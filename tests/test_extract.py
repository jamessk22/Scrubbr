import json
from pathlib import Path

import pytest

from app.extract import (
    FOUND_CANDIDATES,
    NOT_FOUND,
    OLDEST_AGE,
    PARSE_FAILED,
    extract,
    normalize_location,
    parse_age,
    parse_scan_config,
)
from app.models import Broker

FIXTURES = Path(__file__).parent / "fixtures" / "scan"

BROKER_SLUGS = {
    "Whitepages": "whitepages",
    "Radaris": "radaris",
    "FastPeopleSearch": "fastpeoplesearch",
    "TruePeopleSearch": "truepeoplesearch",
    "That'sThem": "thatsthem",
    "Advanced Background Checks": "advancedbackgroundchecks",
    "USPhonebook": "usphonebook",
}


def _scan_config_for(name: str) -> dict:
    payload = json.loads((Path(__file__).parent.parent / "data" / "brokers.json").read_text())
    return next(b for b in payload["brokers"] if b["name"] == name)["scan"]


def _html(slug: str, kind: str) -> str:
    return (FIXTURES / f"{slug}_{kind}.html").read_text()


@pytest.mark.parametrize("broker_name", list(BROKER_SLUGS))
def test_results_page_yields_matching_candidate(broker_name):
    slug = BROKER_SLUGS[broker_name]
    cfg = _scan_config_for(broker_name)
    result = extract(_html(slug, "results"), "https://example.test", cfg, "Jane Public")
    assert result.outcome == FOUND_CANDIDATES
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert c.name == "Jane Public"
    assert c.age == 36
    assert c.locations == ["Boston, MA"]
    assert c.relatives == ["John Public"]


@pytest.mark.parametrize("broker_name", list(BROKER_SLUGS))
def test_empty_results_page_is_not_found(broker_name):
    slug = BROKER_SLUGS[broker_name]
    cfg = _scan_config_for(broker_name)
    result = extract(_html(slug, "empty"), "https://example.test", cfg, "Jane Public")
    assert result.outcome == NOT_FOUND
    assert result.candidates == []


@pytest.mark.parametrize("broker_name", list(BROKER_SLUGS))
def test_challenge_page_is_parse_failed_not_silent_not_found(broker_name):
    """A CAPTCHA/challenge page has no result markers and no matching cards --
    must surface as a failure (outcome 4), never as a silent not_found."""
    slug = BROKER_SLUGS[broker_name]
    cfg = _scan_config_for(broker_name)
    result = extract(_html(slug, "challenge"), "https://example.test", cfg, "Jane Public")
    assert result.outcome == PARSE_FAILED
    assert result.candidates == []


def test_broken_selectors_on_real_content_is_parse_failed():
    cfg = {"result_selector": "div.does-not-exist", "fields": {"name": "div.h4"}}
    html = "<html><body><div class='card-summary'><div class='h4'>Jane Public</div></div></body></html>"
    result = extract(html, "https://example.test", cfg, "")
    assert result.outcome == PARSE_FAILED


def test_no_scan_config_falls_back_to_generic_name_search():
    html = "<html><body><p>Result: Jane Public, age 36, Boston, MA</p></body></html>"
    result = extract(html, "https://example.test", None, "Jane Public")
    assert result.outcome == FOUND_CANDIDATES
    assert result.candidates[0].age == 36
    assert result.candidates[0].locations == ["Boston, MA"]


def test_not_found_marker_does_not_override_real_candidates():
    """A results page with real listings plus footer copy like "Did not find
    who you were looking for?" must not be a false clean-bill (NOT_FOUND)."""
    cfg = {"result_selector": ".card", "fields": {"name": "h2", "age": ".age"}}
    html = ("<div class=card><h2>John Smith</h2><span class=age>Age 40</span></div>"
            "<footer>Did not find who you were looking for? Try again.</footer>")
    result = extract(html, "https://example.test", cfg, "John Smith")
    assert result.outcome == FOUND_CANDIDATES
    assert len(result.candidates) == 1
    assert result.candidates[0].name == "John Smith"


def test_no_scan_config_and_no_generic_hit_is_parse_failed():
    html = "<html><body><p>Totally unrelated page content.</p></body></html>"
    result = extract(html, "https://example.test", None, "Jane Public")
    assert result.outcome == PARSE_FAILED


# --- broker-specific quirks captured from live pages 2026-07-09 ------------------

def test_abc_age_nested_in_name_heading_is_split_off():
    """ABC renders "<h2>Jane Public<span>Age 36</span></h2>"; a naive name read
    would gate on "36" as the last name. See extract._name_text."""
    cfg = _scan_config_for("Advanced Background Checks")
    c = extract(_html("advancedbackgroundchecks", "results"), "https://x", cfg, "Jane Public").candidates[0]
    assert c.name == "Jane Public"
    assert c.age == 36


def test_usphonebook_uses_schema_org_microdata():
    cfg = _scan_config_for("USPhonebook")
    c = extract(_html("usphonebook", "results"), "https://x", cfg, "Jane Public").candidates[0]
    assert (c.name, c.age, c.locations, c.relatives) == ("Jane Public", 36, ["Boston, MA"], ["John Public"])


def test_anywho_json_blob_no_matches_is_not_a_false_not_found():
    """AnyWho ships a Next.js state blob containing "There are no matches for
    your search" even on a page full of results -- the visible-text check must
    ignore it."""
    cfg = _scan_config_for("AnyWho")
    result = extract(_html("anywho", "results"), "https://x", cfg, "Jane Public")
    assert result.outcome == FOUND_CANDIDATES
    c = result.candidates[0]
    assert c.name == "Jane Public"
    assert c.age == 36
    assert c.locations == ["Boston, MA"]


# --- normalize_location --------------------------------------------------------

def test_normalize_location_from_street_address():
    assert normalize_location("10 Beacon St, Boston MA 02108") == "Boston, MA"


def test_normalize_location_from_full_state_name():
    assert normalize_location("Boston, Massachusetts") == "Boston, MA"


def test_normalize_location_no_match_returns_none():
    assert normalize_location("just some words") is None


def test_normalize_location_empty_returns_none():
    assert normalize_location("") is None


# --- parse_scan_config ----------------------------------------------------------

def test_parse_scan_config_valid_json():
    b = Broker(id=1, name="X", category="people-search", scan_config='{"result_selector": "div.card"}')
    assert parse_scan_config(b) == {"result_selector": "div.card"}


def test_parse_scan_config_empty_is_none():
    b = Broker(id=1, name="X", category="people-search", scan_config="")
    assert parse_scan_config(b) is None


def test_parse_scan_config_malformed_is_none():
    b = Broker(id=1, name="X", category="people-search", scan_config="{not json")
    assert parse_scan_config(b) is None


# --- parse_age ------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Age 36", (36, None)),
    ("36", (36, None)),
    ("Age 56 (1970 or 1969)", (56, None)),  # SearchPeopleFree: birth year is noise after the age
    ("70s", (None, (70, 79))),              # 411.com decade bucket
    ("Age 70s", (None, (70, 79))),
    ("65+", (None, (65, OLDEST_AGE))),
    ("Age 65 +", (None, (65, OLDEST_AGE))),
    ("", (None, None)),
    ("Age unknown", (None, None)),
    ("Born 1970s", (None, None)),           # a decade of birth is not a decade of age
])
def test_parse_age(text, expected):
    assert parse_age(text) == expected


def test_extract_carries_age_range_onto_candidate():
    html = '<div class="card"><div class="n">Jane Public</div><span class="a">70s</span></div>'
    cfg = {"result_selector": "div.card", "fields": {"name": "div.n", "age": "span.a"}}
    c = extract(html, "https://x", cfg, "Jane Public").candidates[0]
    assert c.age is None
    assert c.age_range == (70, 79)
