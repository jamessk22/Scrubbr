"""Structured extraction: broker search-result HTML -> list[CandidateListing].

Selector-based (per-broker CSS selectors from `Broker.scan_config`), with a
generic name-adjacent regex fallback for when a site's markup has drifted out
from under the configured selectors. A page with zero candidates and no
"no results" marker is treated as a parse failure (outcome 4), never silently
downgraded to "not found" -- see the plan doc for the rationale.
"""
import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .models import STATE_ABBR, Broker, CandidateListing

NOT_FOUND = "not_found"
FOUND_CANDIDATES = "candidates"
PARSE_FAILED = "parse_failed"

# Reuse across scanner.py-style "no results" copy that people-search sites use.
_NOT_FOUND_MARKERS = (
    "no results", "no records found", "no record found", "0 results",
    "no matches", "we could not find", "couldn't find any", "did not find",
    "nothing found",
)

_AGE_RE = re.compile(r"\b\d{1,3}\b")  # bounded: "1970s" is a birth decade, not an age
_AGE_DECADE_RE = re.compile(r"\b(\d{1,2}0)\s*s\b", re.I)  # "70s" -> 70..79
_AGE_OPEN_RE = re.compile(r"\b(\d{1,3})\s*\+")  # "65+" -> 65..OLDEST_AGE
_PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")

OLDEST_AGE = 120  # upper bound for open-ended buckets like "65+"

_STATE_NAMES = sorted(STATE_ABBR.keys(), key=len, reverse=True)
_STATE_ABBRS = sorted(set(STATE_ABBR.values()), key=len, reverse=True)
_CITY_STATE_RE = re.compile(
    r"([A-Za-z][A-Za-z .'-]{1,40}?),?\s+(" +
    "|".join(_STATE_NAMES + _STATE_ABBRS) +
    r")\b",
    re.I,
)


def normalize_location(text: str) -> str | None:
    """Free text -> 'City, ST', or None if no recognizable city/state pair.

    Used both for candidate locations scraped off a broker page and for the
    profile's own address lines, so the two sides compare on equal footing.
    """
    if not text:
        return None
    m = _CITY_STATE_RE.search(text)
    if not m:
        return None
    city = m.group(1).strip(" ,")
    city = re.sub(r"\s+", " ", city)
    if not city or city.isdigit():
        return None
    state = m.group(2).lower()
    state = STATE_ABBR.get(state, state.upper())
    return f"{city}, {state}"


@dataclass
class ExtractResult:
    outcome: str  # NOT_FOUND | FOUND_CANDIDATES | PARSE_FAILED
    candidates: list[CandidateListing] = field(default_factory=list)


def parse_scan_config(broker: Broker) -> dict | None:
    if not broker.scan_config:
        return None
    try:
        return json.loads(broker.scan_config)
    except (json.JSONDecodeError, TypeError):
        return None


def _visible_text(soup: BeautifulSoup) -> str:
    """Lowercased human-visible text only. Modern result pages ship a Next.js/
    JSON state blob in a <script> that includes fallback copy like "there are
    no matches for your search"; matching that raw would flag a page full of
    listings as empty (AnyWho does exactly this)."""
    clone = BeautifulSoup(str(soup), "html.parser")
    for tag in clone.select("script, style, noscript, template"):
        tag.decompose()
    return clone.get_text(" ", strip=True).lower()


def extract(html: str, source_url: str, scan_config: dict | None, query_name: str = "") -> ExtractResult:
    candidates = _extract_selectors(html, source_url, scan_config) if scan_config else []
    if not candidates:
        candidates = _extract_generic(html, source_url, query_name)

    if candidates:
        # Real listings win over footer copy like "Did not find who you were
        # looking for?"; only consult the not-found markers when nothing parsed.
        return ExtractResult(FOUND_CANDIDATES, candidates)

    soup = BeautifulSoup(html, "html.parser")
    if any(m in _visible_text(soup) for m in _NOT_FOUND_MARKERS):
        return ExtractResult(NOT_FOUND)
    return ExtractResult(PARSE_FAILED)


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _name_text(card, name_sel: str, age_sel: str) -> str:
    """Name text with a nested age element excluded. Sites like ABC and 411.com
    render "<h2>Jane Public<span>Age 36</span></h2>", so a naive read yields
    "Jane Public Age 36" and the name gate's last-name check lands on "36"."""
    name_el = card.select_one(name_sel) if name_sel else None
    if name_el is None:
        return ""
    age_el = card.select_one(age_sel) if age_sel else None
    if age_el is not None and age_el in name_el.descendants:
        return " ".join(name_el.get_text(" ", strip=True).split()).replace(
            _text(age_el), "").strip()
    return _text(name_el)


def _extract_selectors(html: str, source_url: str, scan_config: dict) -> list[CandidateListing]:
    result_sel = scan_config.get("result_selector", "")
    if not result_sel:
        return []
    fields = scan_config.get("fields", {})
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for card in soup.select(result_sel):
        name = _name_text(card, fields.get("name", ""), fields.get("age", ""))
        if not name:
            continue
        raw_text = _text(card)
        age, age_range = parse_age(_text(card.select_one(fields["age"])) if fields.get("age") else "")
        candidates.append(CandidateListing(
            name=name,
            age=age,
            age_range=age_range,
            locations=_parse_locations(card, fields.get("locations", "")),
            relatives=_parse_names(card, fields.get("relatives", "")),
            phones=_dedupe(_PHONE_RE.findall(raw_text)),
            raw_text=raw_text,
            source_url=source_url,
        ))
    return candidates


def _extract_generic(html: str, source_url: str, query_name: str) -> list[CandidateListing]:
    """Best-effort fallback when configured selectors find nothing: look for
    the searched name in the page text and pull age/location out of the
    surrounding words. Low precision by design -- matcher.py's scoring is
    what keeps false positives out, not this.
    """
    query_name = query_name.strip()
    if not query_name:
        return []
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    candidates = []
    for m in re.finditer(re.escape(query_name), text, re.I):
        start = max(0, m.start() - 40)
        window = text[start: m.end() + 200]
        after_name = text[m.end(): m.end() + 200]
        age_m = re.search(r"age\s*:?\s*(\d{1,3})|(\d{1,3})\s*years?\s*old", window, re.I)
        age = int(next(g for g in age_m.groups() if g)) if age_m else None
        loc = normalize_location(after_name)
        candidates.append(CandidateListing(
            name=query_name, age=age,
            locations=[loc] if loc else [],
            phones=_dedupe(_PHONE_RE.findall(window)),
            raw_text=window, source_url=source_url,
        ))
    return candidates


def parse_age(text: str) -> tuple[int | None, tuple[int, int] | None]:
    """(exact_age, age_range). Brokers report age three ways: exact ("Age 56",
    and "Age 56 (1970 or 1969)" where the birth year is noise after the age),
    decade buckets ("70s"), and open-ended ones ("65+"). Only the first is
    comparable with `abs(diff) <= 1`, so the other two come back as ranges.
    """
    if not text:
        return None, None
    decade = _AGE_DECADE_RE.search(text)
    if decade:
        lo = int(decade.group(1))
        return None, (lo, lo + 9)
    open_ended = _AGE_OPEN_RE.search(text)
    if open_ended:
        return None, (int(open_ended.group(1)), OLDEST_AGE)
    exact = _AGE_RE.search(text)
    return (int(exact.group()), None) if exact else (None, None)


def _parse_locations(card, sel: str) -> list[str]:
    if not sel:
        return []
    out = []
    for el in card.select(sel):
        loc = normalize_location(_text(el))
        if loc and loc not in out:
            out.append(loc)
    return out


def _parse_names(card, sel: str) -> list[str]:
    if not sel:
        return []
    out = []
    for el in card.select(sel):
        name = _text(el)
        if name and name not in out:
            out.append(name)
    return out


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
