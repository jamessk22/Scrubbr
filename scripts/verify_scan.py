"""Live-check one broker's scan config, then optionally freeze it as a fixture.

    python -m scripts.verify_scan "PeopleFinders"            # scan + report
    python -m scripts.verify_scan "PeopleFinders" --fixture  # + write a test fixture

Reads the real profile from the gitignored `scan_profile.toml` (falling back to
a profile in the DB), runs one live scan, and prints what `extract.py` pulled
out and how `matcher.py` scored it. `--fixture` trims the page down to the
single matched result card and substitutes a synthetic identity over it, so the
committed fixture carries the broker's real markup and nobody's real data.

This is the loop for adding a broker: run it, read the dumped page in `.scans/`,
write `result_selector`/`fields` into data/brokers.json, re-run with --fixture,
flip `verified: true`, re-seed.
"""
import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup  # noqa: E402

from app import db, extract, fetcher, matcher, scan_service, scanner  # noqa: E402
from app.config import ROOT  # noqa: E402
from app.models import Broker, Profile  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "scan"

# The identity every committed fixture is rewritten to -- matches tests/conftest.py.
SYNTH = {
    "full_name": "Jane Q. Public", "first": "Jane", "last": "Public",
    "email": "jane@example.com", "phone": "555-0100", "relative": "John Public",
    "street": "10 Beacon St", "city": "Boston", "zip": "02108", "age": "36",
}


def load_profile(profile_id: int | None) -> Profile:
    path = ROOT / "scan_profile.toml"
    if path.exists():
        with open(path, "rb") as f:
            return Profile(**tomllib.load(f)["profile"])
    conn = db.connect()
    db.init_db(conn)
    profiles = db.all_profiles(conn)
    conn.close()
    if not profiles:
        sys.exit("No scan_profile.toml and no profiles in the DB.")
    if profile_id is None:
        return profiles[0]
    return next(p for p in profiles if p.id == profile_id)


def load_broker(name: str) -> Broker:
    """From data/brokers.json, not the DB -- the JSON is the source of truth, so
    a config edit is testable without re-seeding."""
    payload = json.loads((ROOT / "data" / "brokers.json").read_text())
    matches = [b for b in payload["brokers"] if name.lower() in b["name"].lower()]
    if len(matches) != 1:
        sys.exit(f"{'No' if not matches else 'Ambiguous'} broker for {name!r}: "
                 f"{[b['name'] for b in matches] or 'try a different substring'}")
    b = matches[0]
    scan = b.get("scan")
    return Broker(
        id=0, name=b["name"], category=b["category"], search_url=b.get("search_url", "") or "",
        scan_config=json.dumps(scan) if scan else "", network=b.get("network") or "",
    )


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _phone_pattern(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())[-10:]
    if len(digits) != 10:
        return ""
    sep = r"[-.\s]?"
    d = list(digits)
    return (rf"(\+?1{sep})?\(?{''.join(d[0:3])}\)?{sep}{''.join(d[3:6])}{sep}{''.join(d[6:10])}")


def _address_parts(profile: Profile) -> list[tuple[str, str]]:
    line = profile.addresses.splitlines()[0] if profile.addresses else ""
    if "," not in line:
        return []
    street, rest = line.split(",", 1)
    tokens = rest.split()
    zip_code = tokens[-1] if tokens and tokens[-1].isdigit() else ""
    city = " ".join(t for t in tokens if not t.isdigit() and len(t) > 2)
    pairs = [(street.strip(), SYNTH["street"])]
    if city:
        pairs.append((city, SYNTH["city"]))
    if zip_code:
        pairs.append((zip_code, SYNTH["zip"]))
    return pairs


def _scrub(html: str, profile: Profile, candidate) -> str:
    """Rewrite every real identifier to the synthetic one. Longest strings first,
    so a full "First Middle Last" is replaced before the bare last name."""
    parts = [p for p in profile.full_name.split() if not p.endswith(".")]
    aliases = [a.strip() for a in profile.aliases.replace(",", "\n").splitlines() if a.strip()]
    pairs: list[tuple[str, str]] = [(profile.full_name, SYNTH["full_name"])]
    if len(parts) >= 2:
        pairs.append((f"{parts[0]} {parts[-1]}", f"{SYNTH['first']} {SYNTH['last']}"))
    pairs += [(a, f"{SYNTH['first']} {SYNTH['last']}") for a in aliases]
    # Bare nicknames ("Jim") survive the full-alias pass when they appear alone.
    pairs += [(tok, SYNTH["last"] if len(parts) >= 2 and tok.lower() == parts[-1].lower() else SYNTH["first"])
              for a in aliases for tok in a.split() if len(tok) >= 3]
    # Third parties on the card (relatives, AKAs) are real people too.
    pairs += [(r, SYNTH["relative"]) for r in (candidate.relatives if candidate else [])]
    pairs += _address_parts(profile)
    pairs += [(e.strip(), SYNTH["email"]) for e in profile.email_list()]
    if len(parts) >= 2:
        pairs += [(parts[-1], SYNTH["last"]), (parts[0], SYNTH["first"])]

    for real, fake in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        if real:
            html = re.sub(re.escape(real), fake, html, flags=re.I)

    for phone in profile.phones.replace(",", "\n").splitlines():
        pattern = _phone_pattern(phone)
        if pattern:
            html = re.sub(pattern, SYNTH["phone"], html)
    if candidate and candidate.age:
        html = re.sub(rf"\b{candidate.age}\b", SYNTH["age"], html)
    return html


def write_fixture(html: str, broker: Broker, profile: Profile, candidate) -> Path:
    """Keep only the matched result card. Everything else on the page is other
    people's listings, tracking payloads, and embedded JSON-LD -- none of which
    belongs in a committed fixture."""
    cfg = extract.parse_scan_config(broker) or {}
    selector = cfg.get("result_selector", "")
    if not selector:
        sys.exit("Set result_selector in data/brokers.json first (read the page in .scans/).")

    name_sel = cfg.get("fields", {}).get("name", "")

    def card_name(card) -> str:
        el = card.select_one(name_sel) if name_sel else None
        return el.get_text(" ", strip=True) if el else ""

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(selector)
    card = next((c for c in cards if matcher.name_gate(card_name(c), profile)), None)
    if card is None:
        sys.exit(f"No card matching {profile.full_name} in {len(cards)} results -- nothing safe to freeze.")
    for tag in card.select("script, style, noscript, svg, img, iframe"):
        tag.decompose()

    body = _scrub(f"<html><body>\n<h1>Search results</h1>\n{card}\n</body></html>\n", profile, candidate)
    real_tokens = re.findall(
        r"[A-Za-z0-9]{4,}", f"{profile.full_name} {profile.aliases} {profile.addresses} {profile.phones}")
    leftovers = {w for w in real_tokens if re.search(rf"\b{re.escape(w)}\b", body, re.I)}
    if leftovers:
        sys.exit(f"Refusing to write: real identifiers survived scrubbing: {sorted(leftovers)}")

    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / f"{_slug(broker.name)}_results.html"
    path.write_text(body)
    for kind, stub in (
        ("empty", "<html><body>\n<h1>Search results</h1>\n<p>Sorry, no results found for your search.</p>\n</body></html>\n"),
        ("challenge", '<html><head><title>Just a moment...</title></head><body>\n'
                      '<div id="challenge">Please complete the CAPTCHA to verify you are human before continuing.</div>\n</body></html>\n'),
    ):
        stub_path = FIXTURES / f"{_slug(broker.name)}_{kind}.html"
        if not stub_path.exists():
            stub_path.write_text(stub)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("broker", help="broker name or unique substring")
    ap.add_argument("--profile", type=int, default=None, help="DB profile id (ignored if scan_profile.toml exists)")
    ap.add_argument("--fixture", action="store_true", help="write a scrubbed tests/fixtures/scan/ fixture")
    args = ap.parse_args()

    profile = load_profile(args.profile)
    broker = load_broker(args.broker)
    print(f"{broker.name}  (network={broker.network or '-'})")

    if scan_service.is_skipped(broker):
        print("  scan.skip is set: manual link only, not auto-scanned.")
    ctx = scanner.search_context(profile)
    if ctx is None:
        sys.exit("Profile needs a first and last name.")
    resolved = scan_service.build_url(broker, ctx)
    if resolved is None or not resolved[0]:
        sys.exit("No usable search URL for this profile (missing search_url, or an unfillable placeholder).")
    url, cfg = resolved
    print(f"  url    {url}")
    print(f"  scope  {scanner.page_scope((cfg or {}).get('search_url') or broker.search_url)}")

    os.environ["SCRUBBR_DUMP_HTML"] = "1"
    try:
        fetched = fetcher.fetch(url, (cfg or {}).get("result_selector", ""))
    except fetcher.Blocked as exc:
        sys.exit(f"  BLOCKED: {exc.reason} (raw page dumped to {fetcher.DUMP_DIR})")
    print(f"  http   {fetched.status}, {len(fetched.html)} bytes -> {fetcher.DUMP_DIR}")

    result = extract.extract(fetched.html, fetched.final_url, cfg, f"{ctx['first']} {ctx['last']}")
    print(f"  parse  {result.outcome}, {len(result.candidates)} candidates")
    for c in result.candidates[:5]:
        age = c.age if c.age is not None else (f"{c.age_range[0]}-{c.age_range[1]}" if c.age_range else "?")
        print(f"    - {c.name!r} age={age} locs={c.locations} phones={c.phones} rels={c.relatives[:3]}")

    match = matcher.best_match(result.candidates, profile)
    if match is None:
        print("  match  none (no candidate survived the name gate + scoring)")
        return
    candidate, score = match
    print(f"  match  {score.verdict} score={score.score:.2f} "
          f"({score.matched}/{score.comparable} signals) {score.signals}")

    if args.fixture:
        print(f"  wrote  {write_fixture(fetched.html, broker, profile, candidate)}")


if __name__ == "__main__":
    main()
