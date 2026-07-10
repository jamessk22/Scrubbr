"""Orchestrates broker/profile scans end to end: rate-limit -> fetch ->
extract -> match -> outcome -> persist. Routes call only this module (and
db.py for reads); the Playwright dependency stays behind fetcher.py, so
everything here is testable by injecting a fake `fetch` callable against an
in-memory db connection.
"""
import json
from dataclasses import dataclass
from datetime import datetime

from . import db, extract, fetcher, matcher, ratelimit, scanner
from .models import (
    EXPOSURE_FOUND,
    EXPOSURE_NOT_FOUND,
    EXPOSURE_POSSIBLE,
    EXPOSURE_UNREACHABLE,
    Broker,
    Profile,
)


MANUAL_ONLY_DETAIL = "Manual check only — see scan notes"


@dataclass
class ScanOutcome:
    status: str  # found | not_found | possible | unreachable
    detail: str = ""
    snapshot: dict | None = None
    url: str = ""
    blocked_status: int = 0  # HTTP status that triggered Blocked, if any (429/503 -> cooldown)


def is_skipped(broker: Broker) -> bool:
    """`scan.skip`: the search page is live but can't be driven by a plain GET
    (a JS wizard, an empty client-rendered shell, or a bot wall). The manual
    link still works -- a human can do what we can't."""
    return bool((extract.parse_scan_config(broker) or {}).get("skip"))


def is_auto_scannable(broker: Broker) -> bool:
    return bool(broker.search_url) and not is_skipped(broker)


def _url_template(broker: Broker, scan_config: dict | None) -> str:
    return (scan_config or {}).get("search_url") or broker.search_url


def build_url(broker: Broker, ctx: dict) -> tuple[str, dict | None] | None:
    """(url, scan_config) for this broker given a profile's search context.
    None if the broker has no search URL configured; ("", scan_config) if it
    has one the profile can't fill (e.g. a {city} template, no known city)."""
    scan_config = extract.parse_scan_config(broker)
    template = _url_template(broker, scan_config)
    if not template:
        return None
    url = scanner.build_search_url(template, ctx)
    if url is None:
        fallback = (scan_config or {}).get("search_url_no_city", "")
        url = scanner.build_search_url(fallback, ctx) if fallback else None
    return url or "", scan_config


def scan(broker: Broker, profile: Profile, fetch=fetcher.fetch) -> ScanOutcome:
    if is_skipped(broker):
        return ScanOutcome(EXPOSURE_UNREACHABLE, detail=MANUAL_ONLY_DETAIL)

    ctx = scanner.search_context(profile)
    if ctx is None:
        return ScanOutcome(EXPOSURE_UNREACHABLE, detail="Profile full name is incomplete")

    resolved = build_url(broker, ctx)
    if resolved is None:
        return ScanOutcome(EXPOSURE_UNREACHABLE, detail="Broker has no configured search URL")
    url, scan_config = resolved
    if not url:
        missing = scanner.missing_placeholders(_url_template(broker, scan_config), ctx)
        return ScanOutcome(EXPOSURE_UNREACHABLE, detail=f"Profile is missing a {' and '.join(missing)}")

    scope = scanner.page_scope(_url_template(broker, scan_config))
    result_selector = (scan_config or {}).get("result_selector", "")
    settings = fetcher.scan_settings()

    with ratelimit.scan_slot():
        ratelimit.wait_for_turn(ratelimit.domain_of(url), settings["min_delay_s"], settings["max_delay_s"])
        try:
            fetched = fetch(url, result_selector)
        except fetcher.Blocked as exc:
            return ScanOutcome(EXPOSURE_UNREACHABLE, detail=exc.reason, url=url, blocked_status=exc.status)
        except Exception as exc:
            # Browser launch failures, navigation timeouts, etc: a single bad
            # fetch must not crash the whole bulk-scan background task.
            return ScanOutcome(EXPOSURE_UNREACHABLE, detail=str(exc).strip().splitlines()[0], url=url)

    query_name = f"{ctx['first']} {ctx['last']}"
    extracted = extract.extract(fetched.html, fetched.final_url, scan_config, query_name)

    if extracted.outcome == extract.PARSE_FAILED:
        return ScanOutcome(EXPOSURE_UNREACHABLE, detail="Couldn't parse the results page", url=fetched.final_url)

    match = matcher.best_match(extracted.candidates, profile)
    if match is None:
        # An unscoped template searches nationwide and paginates, so absence
        # from page 1 is weak evidence -- the UI says so rather than implying
        # a clean bill of health.
        return ScanOutcome(EXPOSURE_NOT_FOUND, snapshot={"page_scope": scope}, url=fetched.final_url)

    candidate, result = match
    fetched_at = datetime.now().isoformat(timespec="seconds")
    snapshot = matcher.to_snapshot(candidate, result, fetched_at) | {"page_scope": scope}
    status = EXPOSURE_FOUND if result.verdict == matcher.MATCH_FOUND else EXPOSURE_POSSIBLE
    return ScanOutcome(status, snapshot=snapshot, url=fetched.final_url)


def _persist(conn, broker_id: int, profile_id: int, outcome: ScanOutcome) -> None:
    snapshot = outcome.snapshot
    if snapshot is None and outcome.status == EXPOSURE_UNREACHABLE and outcome.detail:
        snapshot = {"reason": outcome.detail}
    db.set_exposure(
        conn, broker_id, profile_id, outcome.status, "auto",
        evidence=outcome.url, snapshot=json.dumps(snapshot) if snapshot else "",
    )
    if outcome.blocked_status in (429, 503) and outcome.url:
        ratelimit.set_cooldown(conn, ratelimit.domain_of(outcome.url))


def scan_and_persist(conn, broker: Broker, profile: Profile, fetch=fetcher.fetch) -> ScanOutcome:
    """Runs scan() and persists the result, unless a prior *manual* verdict
    for this broker/profile exists -- a manual mark always wins over an auto
    (re-)scan, so it's checked before touching the network at all."""
    existing = db.get_exposure(conn, broker.id, profile.id)
    if existing is not None and existing.source == "manual":
        return ScanOutcome(existing.status, detail="Manual verdict kept", url=existing.evidence)
    outcome = scan(broker, profile, fetch=fetch)
    _persist(conn, broker.id, profile.id, outcome)
    return outcome


def is_cooled_down_for(conn, broker: Broker, profile: Profile) -> bool:
    ctx = scanner.search_context(profile)
    resolved = build_url(broker, ctx) if ctx else None
    if resolved is None or not resolved[0]:
        return False
    return ratelimit.is_cooled_down(conn, ratelimit.domain_of(resolved[0]))


def stale_searchable_broker_ids(
    conn, brokers: list[Broker], profile_id: int, rescan_after_days: int,
    one_per_network: bool = False,
) -> list[int]:
    """Auto-scannable brokers whose exposure for this profile is missing or old
    enough to be worth spending another scan on. With `one_per_network`, only
    the first sibling of each network is scanned -- the rest inherit a derived
    `likely` (see models.found_networks), which is the whole point of grouping
    them."""
    exposures = db.exposures_for_profile(conn, profile_id)
    seen_networks: set[str] = set()
    ids = []
    for b in brokers:
        if not is_auto_scannable(b):
            continue
        if not ratelimit.is_stale(exposures[b.id].checked_at if b.id in exposures else None, rescan_after_days):
            continue
        if one_per_network and b.network:
            if b.network in seen_networks:
                continue
            seen_networks.add(b.network)
        ids.append(b.id)
    return ids
