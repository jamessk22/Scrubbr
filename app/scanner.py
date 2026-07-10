"""Search-URL construction shared by the manual "open search" link and the
Playwright scan pipeline (scan_service.py). The actual fetch/classify work
lives in fetcher.py/extract.py/matcher.py -- this module only builds URLs
and derives the {first, last, city, state} context from a Profile.
"""
import re
from urllib.parse import quote, quote_plus

from .models import STATE_ABBR as _STATE_ABBR
from .models import Profile

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# Placeholders that scope a result page to a place. A template with none of
# them returns nationwide, paginated results, so "not on page 1" is weak
# evidence of absence rather than a clean bill.
_LOCATION_PLACEHOLDERS = frozenset({"city", "state"})

SCOPE_SCOPED = "scoped"
SCOPE_UNSCOPED = "unscoped"


def search_context(profile: Profile) -> dict | None:
    """{first, last, city, state} for URL templates; None if the name is incomplete."""
    parts = [p for p in profile.full_name.split() if not p.endswith(".")]
    if len(parts) < 2:
        return None
    city = ""
    line = profile.addresses.splitlines()[0] if profile.addresses else ""
    if "," in line:
        # "Street, City, ST ZIP" -> city is the second-to-last comma segment;
        # "Street, City ST ZIP" -> the last segment holds city + state + zip.
        segments = [s.strip() for s in line.split(",")]
        city_seg = segments[-2] if len(segments) >= 3 else segments[-1]
        tokens = [t for t in city_seg.split() if not t.isdigit()]
        if tokens and (tokens[-1].upper() in _STATE_ABBR.values() or tokens[-1].lower() in _STATE_ABBR):
            tokens = tokens[:-1]
        city = " ".join(tokens)
    state = profile.state.strip()
    state = _STATE_ABBR.get(state.lower(), state.upper() if len(state) == 2 else state)
    return {"first": parts[0], "last": parts[-1], "city": city, "state": state}


def missing_placeholders(template: str, ctx: dict) -> list[str]:
    """Placeholders the template needs that the profile can't fill (a profile
    with no parseable city would otherwise build `/person-search/John-Smith/MA/`
    and scrape whatever nationwide page that lands on)."""
    return [name for name in _PLACEHOLDER_RE.findall(template) if not ctx.get(name, "").strip()]


def page_scope(template: str) -> str:
    return SCOPE_SCOPED if _LOCATION_PLACEHOLDERS & set(_PLACEHOLDER_RE.findall(template)) else SCOPE_UNSCOPED


def build_search_url(template: str, ctx: dict) -> str | None:
    """None when the profile can't fill every placeholder the template uses."""
    if missing_placeholders(template, ctx):
        return None
    path, sep, query = template.partition("?")
    path = path.format(**{k: quote(v.replace(" ", "-")) for k, v in ctx.items()})
    if query:
        query = query.format(**{k: quote_plus(v) for k, v in ctx.items()})
    return path + sep + query
