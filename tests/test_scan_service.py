import json

import pytest

from app import db, fetcher, ratelimit, scan_service
from app.models import EXPOSURE_FOUND, EXPOSURE_NOT_FOUND, EXPOSURE_POSSIBLE, EXPOSURE_UNREACHABLE


@pytest.fixture(autouse=True)
def _no_rate_limit_sleep(monkeypatch):
    # Real per-domain spacing (tested in test_ratelimit.py) would otherwise
    # sleep 8-20s for every scan() call in this file that reuses a domain.
    monkeypatch.setattr(ratelimit, "wait_for_turn", lambda *a, **kw: None)


@pytest.fixture
def db_profile(conn, profile_id):
    """The `profile` fixture isn't persisted (id=0); scan_and_persist needs a
    real row so its broker_id/profile_id FK writes succeed."""
    return db.get_profile(conn, profile_id)

SCAN_CONFIG = {
    "search_url": "https://example.test/results?name={first}+{last}",
    "result_selector": "div.card",
    "fields": {"name": "div.h4", "age": "span.age", "locations": "div.loc"},
}


def _broker(conn, **over):
    db.upsert_broker(conn, {
        "name": "Example Broker", "category": "people-search",
        "search_url": SCAN_CONFIG["search_url"],
        "scan_config": json.dumps(SCAN_CONFIG),
        **over,
    })
    conn.commit()
    return next(b for b in db.all_brokers(conn) if b.name == over.get("name", "Example Broker"))


def _fetch_returning(html, status=200):
    def fetch(url, result_selector=""):
        return fetcher.FetchResult(final_url=url, html=html, status=status)
    return fetch


def _fetch_raising(exc):
    def fetch(url, result_selector=""):
        raise exc
    return fetch


from datetime import date  # noqa: E402

# Matches the `profile` fixture's DOB (1990-01-15) in conftest.py, computed
# dynamically so this doesn't quietly drift out of sync with wall-clock time.
_PROFILE_AGE = date.today().year - 1990 - ((date.today().month, date.today().day) < (1, 15))

FOUND_HTML = f"""
<div class="card">
  <div class="h4">Jane Public</div>
  <span class="age">{_PROFILE_AGE}</span>
  <div class="loc">Boston, MA</div>
</div>
"""

# Right name and age, wrong location: exactly one comparable signal matches
# (age), which the plan requires to land as `possible`, never `found`.
POSSIBLE_HTML = f"""
<div class="card">
  <div class="h4">Jane Public</div>
  <span class="age">{_PROFILE_AGE}</span>
  <div class="loc">Miami, FL</div>
</div>
"""

NOT_FOUND_HTML = "<p>No records found for your search.</p>"


# --- scan() outcomes -----------------------------------------------------------

def test_scan_found(profile):
    from app.models import Broker
    broker = Broker(id=1, name="Example", category="people-search",
                     search_url=SCAN_CONFIG["search_url"], scan_config=json.dumps(SCAN_CONFIG))
    outcome = scan_service.scan(broker, profile, fetch=_fetch_returning(FOUND_HTML))
    assert outcome.status == EXPOSURE_FOUND
    assert outcome.snapshot is not None
    assert outcome.snapshot["name"] == "Jane Public"


def test_scan_possible(profile):
    from app.models import Broker
    broker = Broker(id=1, name="Example", category="people-search",
                     search_url=SCAN_CONFIG["search_url"], scan_config=json.dumps(SCAN_CONFIG))
    outcome = scan_service.scan(broker, profile, fetch=_fetch_returning(POSSIBLE_HTML))
    assert outcome.status == EXPOSURE_POSSIBLE
    assert outcome.snapshot is not None


def test_scan_not_found(profile):
    from app.models import Broker
    broker = Broker(id=1, name="Example", category="people-search",
                     search_url=SCAN_CONFIG["search_url"], scan_config=json.dumps(SCAN_CONFIG))
    outcome = scan_service.scan(broker, profile, fetch=_fetch_returning(NOT_FOUND_HTML))
    assert outcome.status == EXPOSURE_NOT_FOUND
    # SCAN_CONFIG's template has no {city}/{state}, so absence is only page-1 evidence.
    assert outcome.snapshot == {"page_scope": "unscoped"}


def test_scan_unreachable_on_blocked(profile):
    from app.models import Broker
    broker = Broker(id=1, name="Example", category="people-search",
                     search_url=SCAN_CONFIG["search_url"], scan_config=json.dumps(SCAN_CONFIG))
    outcome = scan_service.scan(broker, profile, fetch=_fetch_raising(fetcher.Blocked("captcha", status=429)))
    assert outcome.status == EXPOSURE_UNREACHABLE
    assert outcome.blocked_status == 429


def test_scan_unreachable_on_generic_fetch_error_does_not_raise(profile):
    from app.models import Broker
    broker = Broker(id=1, name="Example", category="people-search",
                     search_url=SCAN_CONFIG["search_url"], scan_config=json.dumps(SCAN_CONFIG))
    outcome = scan_service.scan(broker, profile, fetch=_fetch_raising(RuntimeError("boom")))
    assert outcome.status == EXPOSURE_UNREACHABLE
    assert outcome.detail == "boom"


def test_scan_unreachable_incomplete_profile():
    from app.models import Broker, Profile
    broker = Broker(id=1, name="Example", category="people-search", search_url=SCAN_CONFIG["search_url"])
    outcome = scan_service.scan(broker, Profile(full_name="Cher"))
    assert outcome.status == EXPOSURE_UNREACHABLE


def test_scan_unreachable_no_search_url(profile):
    from app.models import Broker
    broker = Broker(id=1, name="Example", category="people-search")
    outcome = scan_service.scan(broker, profile)
    assert outcome.status == EXPOSURE_UNREACHABLE


# --- scan_and_persist(): persistence + manual guard ------------------------------

def test_scan_and_persist_found_writes_exposure_with_snapshot(conn, db_profile, profile_id):
    broker = _broker(conn)
    outcome = scan_service.scan_and_persist(conn, broker, db_profile, fetch=_fetch_returning(FOUND_HTML))
    assert outcome.status == EXPOSURE_FOUND
    exp = db.get_exposure(conn, broker.id, profile_id)
    assert exp.status == EXPOSURE_FOUND
    assert exp.source == "auto"
    assert json.loads(exp.snapshot)["name"] == "Jane Public"


def test_scan_and_persist_unreachable_stores_reason_as_snapshot(conn, db_profile, profile_id):
    broker = _broker(conn)
    scan_service.scan_and_persist(conn, broker, db_profile, fetch=_fetch_raising(RuntimeError("no browser")))
    exp = db.get_exposure(conn, broker.id, profile_id)
    assert exp.status == EXPOSURE_UNREACHABLE
    assert json.loads(exp.snapshot)["reason"] == "no browser"


def test_scan_and_persist_sets_cooldown_on_429(conn, db_profile, profile_id):
    broker = _broker(conn)
    scan_service.scan_and_persist(conn, broker, db_profile, fetch=_fetch_raising(fetcher.Blocked("rate limited", status=429)))
    assert db.get_cooldown(conn, "example.test") is not None


def test_scan_and_persist_does_not_set_cooldown_on_generic_block(conn, db_profile, profile_id):
    broker = _broker(conn)
    scan_service.scan_and_persist(conn, broker, db_profile, fetch=_fetch_raising(fetcher.Blocked("captcha", status=0)))
    assert db.get_cooldown(conn, "example.test") is None


def test_scan_and_persist_never_overwrites_manual_verdict(conn, db_profile, profile_id):
    broker = _broker(conn)
    db.set_exposure(conn, broker.id, profile_id, EXPOSURE_NOT_FOUND, "manual")

    calls = []

    def fetch(url, result_selector=""):
        calls.append(url)
        return fetcher.FetchResult(final_url=url, html=FOUND_HTML, status=200)

    outcome = scan_service.scan_and_persist(conn, broker, db_profile, fetch=fetch)

    assert calls == []  # never even touched the network
    assert outcome.status == EXPOSURE_NOT_FOUND
    exp = db.get_exposure(conn, broker.id, profile_id)
    assert exp.status == EXPOSURE_NOT_FOUND
    assert exp.source == "manual"


def test_scan_and_persist_never_overwrites_propagated_verdict(conn, db_profile, profile_id):
    """A verdict propagated from a same-network sibling's manual mark is
    human-originated, so an auto rescan must not silently flip it."""
    broker = _broker(conn)
    db.set_exposure(conn, broker.id, profile_id, EXPOSURE_NOT_FOUND, "network")

    calls = []

    def fetch(url, result_selector=""):
        calls.append(url)
        return fetcher.FetchResult(final_url=url, html=FOUND_HTML, status=200)

    outcome = scan_service.scan_and_persist(conn, broker, db_profile, fetch=fetch)

    assert calls == []
    assert outcome.status == EXPOSURE_NOT_FOUND
    assert db.get_exposure(conn, broker.id, profile_id).source == "network"


def test_scan_and_persist_overwrites_prior_auto_verdict(conn, db_profile, profile_id):
    broker = _broker(conn)
    db.set_exposure(conn, broker.id, profile_id, EXPOSURE_NOT_FOUND, "auto")
    scan_service.scan_and_persist(conn, broker, db_profile, fetch=_fetch_returning(FOUND_HTML))
    exp = db.get_exposure(conn, broker.id, profile_id)
    assert exp.status == EXPOSURE_FOUND
    assert exp.source == "auto"


# --- staleness / cooldown gating for bulk scans ---------------------------------

def test_stale_searchable_broker_ids_includes_never_scanned(conn, profile_id):
    broker = _broker(conn)
    assert broker.id in scan_service.stale_searchable_broker_ids(conn, db.all_brokers(conn), profile_id, 14)


def test_stale_searchable_broker_ids_excludes_recently_scanned(conn, profile, profile_id):
    broker = _broker(conn)
    db.set_exposure(conn, broker.id, profile_id, EXPOSURE_NOT_FOUND, "auto")
    assert broker.id not in scan_service.stale_searchable_broker_ids(conn, db.all_brokers(conn), profile_id, 14)


def test_stale_searchable_broker_ids_excludes_unsearchable_brokers(conn, profile_id):
    _broker(conn, name="No Search", search_url="", scan_config="")
    ids = scan_service.stale_searchable_broker_ids(conn, db.all_brokers(conn), profile_id, 14)
    assert ids == []


def test_is_cooled_down_for_reflects_cooldown_table(conn, profile):
    broker = _broker(conn)
    assert not scan_service.is_cooled_down_for(conn, broker, profile)
    db.set_cooldown(conn, "example.test", "2999-01-01T00:00:00")
    assert scan_service.is_cooled_down_for(conn, broker, profile)


# --- build_url -------------------------------------------------------------------

def test_build_url_prefers_scan_config_search_url(profile):
    from app.models import Broker
    broker = Broker(id=1, name="X", category="people-search",
                     search_url="https://legacy.test/{first}", scan_config=json.dumps(SCAN_CONFIG))
    from app.scanner import search_context
    ctx = search_context(profile)
    url, cfg = scan_service.build_url(broker, ctx)
    assert url.startswith("https://example.test/results")
    assert cfg == SCAN_CONFIG


def test_build_url_falls_back_to_legacy_search_url(profile):
    from app.models import Broker
    broker = Broker(id=1, name="X", category="people-search", search_url="https://legacy.test/{first}-{last}")
    from app.scanner import search_context
    ctx = search_context(profile)
    resolved = scan_service.build_url(broker, ctx)
    assert resolved[0] == "https://legacy.test/Jane-Public"
    assert resolved[1] is None


# --- scan.skip: live URL, but no plain-GET path to results -------------------------

SKIP_CONFIG = {**SCAN_CONFIG, "skip": True}


def _skip_broker(**over):
    from app.models import Broker
    return Broker(id=1, name="Spokeo", category="people-search",
                  search_url=SCAN_CONFIG["search_url"], scan_config=json.dumps(SKIP_CONFIG), **over)


def test_skipped_broker_is_unreachable_without_fetching(profile):
    calls = []

    def fetch(url, result_selector=""):
        calls.append(url)
        return fetcher.FetchResult(final_url=url, html=FOUND_HTML, status=200)

    outcome = scan_service.scan(_skip_broker(), profile, fetch=fetch)
    assert outcome.status == EXPOSURE_UNREACHABLE
    assert outcome.detail == scan_service.MANUAL_ONLY_DETAIL
    assert calls == []


def test_is_auto_scannable():
    from app.models import Broker
    assert scan_service.is_auto_scannable(
        Broker(id=1, name="X", category="people-search", search_url=SCAN_CONFIG["search_url"]))
    assert not scan_service.is_auto_scannable(_skip_broker())
    assert not scan_service.is_auto_scannable(Broker(id=2, name="Y", category="marketing"))


def test_stale_searchable_broker_ids_excludes_skipped(conn, profile_id):
    _broker(conn, name="Skippy", scan_config=json.dumps(SKIP_CONFIG))
    ids = scan_service.stale_searchable_broker_ids(conn, db.all_brokers(conn), profile_id, 14)
    assert ids == []


# --- one-per-network bulk dividend -------------------------------------------------

def test_stale_searchable_broker_ids_one_per_network(conn, profile_id):
    a = _broker(conn, name="Intelius", network="peopleconnect")
    b = _broker(conn, name="TruthFinder", network="peopleconnect")
    solo = _broker(conn, name="Radaris", network="")

    all_ids = scan_service.stale_searchable_broker_ids(conn, db.all_brokers(conn), profile_id, 14)
    assert {a.id, b.id, solo.id} <= set(all_ids)

    trimmed = scan_service.stale_searchable_broker_ids(
        conn, db.all_brokers(conn), profile_id, 14, one_per_network=True)
    assert solo.id in trimmed
    assert len({a.id, b.id} & set(trimmed)) == 1  # exactly one sibling scanned


# --- city-less profiles ------------------------------------------------------------

CITY_CONFIG = {**SCAN_CONFIG, "search_url": "https://example.test/p/{first}-{last}/{state}/{city}"}


def test_city_less_profile_is_unreachable_not_a_garbage_fetch(conn):
    from app.models import Broker, Profile
    broker = Broker(id=1, name="411", category="people-search",
                    search_url=CITY_CONFIG["search_url"], scan_config=json.dumps(CITY_CONFIG))
    cityless = Profile(full_name="Jane Public", addresses="", state="Massachusetts")
    calls = []

    def fetch(url, result_selector=""):
        calls.append(url)
        return fetcher.FetchResult(final_url=url, html=FOUND_HTML, status=200)

    outcome = scan_service.scan(broker, cityless, fetch=fetch)
    assert outcome.status == EXPOSURE_UNREACHABLE
    assert outcome.detail == "Profile is missing a city"
    assert calls == []


def test_search_url_no_city_fallback_is_used(profile):
    from app.models import Broker, Profile
    cfg = {**CITY_CONFIG, "search_url_no_city": "https://example.test/p/{first}-{last}/{state}"}
    broker = Broker(id=1, name="411", category="people-search",
                    search_url=cfg["search_url"], scan_config=json.dumps(cfg))
    cityless = Profile(full_name="Jane Public", addresses="", state="Massachusetts",
                       date_of_birth="1990-01-15")
    outcome = scan_service.scan(broker, cityless, fetch=_fetch_returning(FOUND_HTML))
    assert outcome.url == "https://example.test/p/Jane-Public/MA"


# --- page scope ---------------------------------------------------------------------

def test_scoped_template_marks_snapshot_scoped(profile):
    from app.models import Broker
    broker = Broker(id=1, name="X", category="people-search",
                    search_url=CITY_CONFIG["search_url"], scan_config=json.dumps(CITY_CONFIG))
    outcome = scan_service.scan(broker, profile, fetch=_fetch_returning(FOUND_HTML))
    assert outcome.snapshot["page_scope"] == "scoped"


def test_unscoped_not_found_records_weak_evidence(profile):
    """AnyWho paginates 72k nationwide results with no state filter, so a page-1
    miss is weak evidence -- the snapshot has to say so."""
    from app.models import Broker
    broker = Broker(id=1, name="AnyWho", category="people-search",
                    search_url=SCAN_CONFIG["search_url"], scan_config=json.dumps(SCAN_CONFIG))
    outcome = scan_service.scan(broker, profile, fetch=_fetch_returning(NOT_FOUND_HTML))
    assert outcome.status == EXPOSURE_NOT_FOUND
    assert outcome.snapshot == {"page_scope": "unscoped"}
