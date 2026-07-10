"""Score a broker's scraped CandidateListing against the local Profile.

Deterministic, local-only (no LLM, no network). The rule this file exists to
enforce: a same-name stranger with a coincidentally similar age is never a
`found` match on its own -- at least two independently-comparable signals
must agree, and one of them can't just be "the name we already gated on".
"""
from dataclasses import dataclass, field
from datetime import date

from .extract import normalize_location
from .models import STATE_ABBR, CandidateListing, Profile

MATCH_FOUND = "found"
MATCH_POSSIBLE = "possible"
MATCH_DISCARD = "discard"

# score = matched signals / comparable signals. Both thresholds must hold for
# `found`: score alone would let a candidate with exactly one comparable
# signal (matched) count as 100% -- requiring matched >= 2 rules that out.
SCORE_THRESHOLD = 0.6
MIN_MATCHED_FOR_FOUND = 2

# Small built-in nickname map; profile.aliases covers anything not listed here.
_NICKNAME_GROUPS: list[set[str]] = [
    {"james", "jim", "jimmy", "jamie"},
    {"robert", "bob", "bobby", "rob"},
    {"william", "bill", "billy", "will"},
    {"richard", "rick", "dick", "rich"},
    {"elizabeth", "liz", "beth", "betty", "eliza"},
    {"margaret", "maggie", "meg", "peggy"},
    {"michael", "mike", "mickey"},
    {"katherine", "catherine", "kate", "katie", "kathy", "cathy"},
    {"christopher", "chris"},
    {"joseph", "joe", "joey"},
    {"daniel", "dan", "danny"},
    {"thomas", "tom", "tommy"},
    {"charles", "charlie", "chuck"},
    {"jennifer", "jen", "jenny"},
    {"john", "jack", "johnny"},
    {"patricia", "pat", "patty", "trish"},
    {"susan", "sue", "susie"},
]


@dataclass
class MatchResult:
    verdict: str
    score: float
    matched: int
    comparable: int
    signals: dict[str, bool | None] = field(default_factory=dict)


def _norm_last(name: str) -> str:
    return name.strip().lower().replace("-", "").replace("'", "")


def _same_first_name(a: str, b: str, aliases: set[str]) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    if not a or not b:
        return False
    if a == b or a in aliases or b in aliases:
        return True
    return any(a in group and b in group for group in _NICKNAME_GROUPS)


def _profile_aliases(profile: Profile) -> set[str]:
    return {a.strip().lower() for a in profile.aliases.replace(",", "\n").splitlines() if a.strip()}


def _profile_primary_name(profile: Profile) -> str:
    parts = [p for p in profile.full_name.split() if not p.endswith(".")]
    return f"{parts[0]} {parts[-1]}".strip().lower() if len(parts) >= 2 else ""


def name_gate(candidate_name: str, profile: Profile) -> bool:
    """Last name exact, first name exact/nickname/alias. Discards non-matches
    before any scoring happens -- same-name-different-person is handled by
    signal scoring, not by loosening this gate."""
    parts = [p for p in profile.full_name.split() if not p.endswith(".")]
    if len(parts) < 2:
        return False
    cand_parts = candidate_name.split()
    if len(cand_parts) < 2:
        return False
    if _norm_last(cand_parts[-1]) != _norm_last(parts[-1]):
        return False
    return _same_first_name(parts[0], cand_parts[0], _profile_aliases(profile))


def _age_from_dob(dob: str) -> int | None:
    try:
        y, m, d = (int(x) for x in dob.split("-"))
        born = date(y, m, d)
    except (ValueError, AttributeError):
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _profile_locations(profile: Profile) -> list[str]:
    out = []
    for line in profile.addresses.splitlines():
        loc = normalize_location(line)
        if loc and loc not in out:
            out.append(loc)
    return out


def _profile_state_abbr(profile: Profile) -> str:
    state = profile.state.strip()
    if not state:
        return ""
    return STATE_ABBR.get(state.lower(), state.upper() if len(state) == 2 else "")


def _profile_phones(profile: Profile) -> set[str]:
    return {
        "".join(ch for ch in p if ch.isdigit())
        for p in profile.phones.replace(",", "\n").splitlines() if p.strip()
    }


def _age_signal(candidate: CandidateListing, profile_age: int | None) -> bool | None:
    """Exact ages compare within a year (brokers round, and a birthday may have
    passed since they last refreshed). A bucketed age ("70s", "65+") only tells
    us containment, so it's tested as such -- without this, a true match on a
    bucketing broker like 411.com could never earn its age signal."""
    if profile_age is None:
        return None
    if candidate.age is not None:
        return abs(candidate.age - profile_age) <= 1
    if candidate.age_range is not None:
        lo, hi = candidate.age_range
        return lo <= profile_age <= hi
    return None


def compute_signals(candidate: CandidateListing, profile: Profile) -> dict[str, bool | None]:
    signals: dict[str, bool | None] = {}

    signals["age"] = _age_signal(candidate, _age_from_dob(profile.date_of_birth))

    profile_locs = _profile_locations(profile)
    signals["location"] = None if not candidate.locations or not profile_locs else any(
        loc in profile_locs for loc in candidate.locations
    )

    profile_state = _profile_state_abbr(profile)
    cand_states = {loc.rsplit(", ", 1)[-1] for loc in candidate.locations if ", " in loc}
    signals["state"] = None if not cand_states or not profile_state else profile_state in cand_states

    # Exclude the profile's own bare "First Last" from the alias set: matching
    # it adds no information beyond what the name gate already required, and
    # would otherwise turn a same-name-stranger with zero real signals into
    # a spurious "possible" instead of a discard.
    aliases = _profile_aliases(profile) - {_profile_primary_name(profile)}
    signals["alias"] = None if not aliases else candidate.name.strip().lower() in aliases

    profile_phones = _profile_phones(profile)
    cand_phones = {"".join(ch for ch in p if ch.isdigit()) for p in candidate.phones}
    signals["phone"] = None if not cand_phones or not profile_phones else bool(cand_phones & profile_phones)

    return signals


def score_signals(signals: dict[str, bool | None]) -> MatchResult:
    comparable = [v for v in signals.values() if v is not None]
    matched = sum(1 for v in comparable if v)
    score = matched / len(comparable) if comparable else 0.0
    if score >= SCORE_THRESHOLD and matched >= MIN_MATCHED_FOR_FOUND:
        verdict = MATCH_FOUND
    elif matched >= 1:
        verdict = MATCH_POSSIBLE
    else:
        verdict = MATCH_DISCARD
    return MatchResult(verdict=verdict, score=score, matched=matched, comparable=len(comparable), signals=signals)


_VERDICT_RANK = {MATCH_DISCARD: 0, MATCH_POSSIBLE: 1, MATCH_FOUND: 2}


def best_match(candidates: list[CandidateListing], profile: Profile) -> tuple[CandidateListing, MatchResult] | None:
    """Best surviving (candidate, result) after the name gate + scoring, or
    None if every candidate was discarded (same-name stranger, or no
    candidates at all)."""
    best: tuple[CandidateListing, MatchResult] | None = None
    for candidate in candidates:
        if not name_gate(candidate.name, profile):
            continue
        result = score_signals(compute_signals(candidate, profile))
        if result.verdict == MATCH_DISCARD:
            continue
        if best is None or (
            (_VERDICT_RANK[result.verdict], result.score) > (_VERDICT_RANK[best[1].verdict], best[1].score)
        ):
            best = (candidate, result)
    return best


def to_snapshot(candidate: CandidateListing, result: MatchResult, fetched_at: str) -> dict:
    return {
        "name": candidate.name,
        "age": candidate.age,
        "age_range": list(candidate.age_range) if candidate.age_range else None,
        "locations": candidate.locations,
        "relatives": candidate.relatives,
        "signals": result.signals,
        "score": round(result.score, 2),
        "source_url": candidate.source_url,
        "fetched_at": fetched_at,
    }
