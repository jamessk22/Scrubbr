from app.matcher import (
    MATCH_DISCARD,
    MATCH_FOUND,
    MATCH_POSSIBLE,
    best_match,
    compute_signals,
    name_gate,
    score_signals,
    to_snapshot,
)
from app.models import CandidateListing, Profile


def _profile(**over):
    base = dict(
        full_name="Jane Q. Public", aliases="Jane Public",
        emails="jane@example.com", phones="555-0100",
        addresses="10 Beacon St, Boston MA 02108\n5 Elm St, Albany NY 12207",
        date_of_birth="1990-01-15", state="Massachusetts",
    )
    base.update(over)
    return Profile(**base)


# --- name gate ---------------------------------------------------------------

def test_name_gate_exact_match():
    assert name_gate("Jane Public", _profile())


def test_name_gate_wrong_last_name_fails():
    assert not name_gate("Jane Smith", _profile())


def test_name_gate_nickname_passes():
    assert name_gate("Jim Public", _profile(full_name="James Public", aliases=""))


def test_name_gate_alias_first_name_passes():
    assert name_gate("Janie Public", _profile(aliases="Janie"))


def test_name_gate_incomplete_candidate_name_fails():
    assert not name_gate("Jane", _profile())


def test_name_gate_incomplete_profile_name_fails():
    assert not name_gate("Jane Public", Profile(full_name="Cher"))


def test_name_gate_suffix_on_profile_side_ignored():
    assert name_gate("Jane Public", _profile(full_name="Jane Public Jr", aliases=""))


def test_name_gate_suffix_on_candidate_side_ignored():
    assert name_gate("Jane Public Jr.", _profile())


def test_name_gate_suffix_on_both_sides_ignored():
    assert name_gate("Jane Public III", _profile(full_name="Jane Public Sr", aliases=""))


# --- signals -------------------------------------------------------------------

def test_signal_age_within_tolerance():
    profile = _profile()  # DOB 1990-01-15
    from datetime import date
    expected_age = date.today().year - 1990 - ((date.today().month, date.today().day) < (1, 15))
    c = CandidateListing(name="Jane Public", age=expected_age + 1)
    assert compute_signals(c, profile)["age"] is True


def test_signal_missing_data_is_none_not_false():
    profile = _profile(date_of_birth="")
    c = CandidateListing(name="Jane Public", age=None)
    assert compute_signals(c, profile)["age"] is None


def test_signal_location_matches_past_address():
    profile = _profile()
    c = CandidateListing(name="Jane Public", locations=["Albany, NY"])
    signals = compute_signals(c, profile)
    assert signals["location"] is True
    # state is a *weaker* signal keyed off profile.state (current residence),
    # so a stale-address match doesn't imply it.
    assert signals["state"] is False


def test_signal_phone_digits_only_compare():
    profile = _profile(phones="(555) 010-0000")
    c = CandidateListing(name="Jane Public", phones=["555-010-0000"])
    assert compute_signals(c, profile)["phone"] is True


def test_alias_signal_excludes_bare_primary_name():
    """A profile alias that's just the primary first+last adds no info beyond
    the name gate -- must not count as a corroborating signal."""
    profile = _profile(aliases="Jane Public")
    c = CandidateListing(name="Jane Public")
    assert compute_signals(c, profile)["alias"] is None


def test_alias_signal_matches_real_alternate_name():
    profile = _profile(aliases="Jane Smith")
    c = CandidateListing(name="Jane Smith")
    assert compute_signals(c, profile)["alias"] is True


# --- scoring / verdicts --------------------------------------------------------

def test_right_name_and_age_wrong_everything_else_is_possible_never_found():
    """Explicit regression required by the plan: age alone must never reach found."""
    profile = _profile()
    from datetime import date
    age = date.today().year - 1990
    c = CandidateListing(name="Jane Public", age=age, locations=["Miami, FL"], relatives=["Someone Else"])
    match = best_match([c], profile)
    assert match is not None
    assert match[1].verdict == MATCH_POSSIBLE


def test_same_name_stranger_with_no_comparable_signals_is_discarded():
    profile = _profile(aliases="Jane Public")  # bare-name alias, excluded from signal
    c = CandidateListing(name="Jane Public")
    assert best_match([c], profile) is None


def test_strong_match_is_found():
    profile = _profile()
    from datetime import date
    age = date.today().year - 1990
    c = CandidateListing(name="Jane Public", age=age, locations=["Boston, MA"], phones=["5550100"])
    match = best_match([c], profile)
    assert match is not None
    assert match[1].verdict == MATCH_FOUND


def test_best_match_picks_highest_ranked_candidate():
    profile = _profile()
    weak = CandidateListing(name="Jane Public", locations=["Miami, FL"])
    from datetime import date
    age = date.today().year - 1990
    strong = CandidateListing(name="Jane Public", age=age, locations=["Boston, MA"], phones=["5550100"])
    winner = best_match([weak, strong], profile)
    assert winner[0] is strong


def test_no_candidates_survive_gate_returns_none():
    profile = _profile()
    c = CandidateListing(name="John Smith", age=99)
    assert best_match([c], profile) is None


def test_score_signals_thresholds():
    assert score_signals({"a": True, "b": True}).verdict == MATCH_FOUND
    assert score_signals({"a": True, "b": False, "c": False}).verdict == MATCH_POSSIBLE
    assert score_signals({"a": None, "b": None}).verdict == MATCH_DISCARD
    assert score_signals({"a": True}).verdict == MATCH_POSSIBLE  # matched>=1 but matched<2 -> never found


def test_to_snapshot_shape():
    c = CandidateListing(name="Jane Public", age=36, locations=["Boston, MA"], relatives=["R"], source_url="https://x")
    result = score_signals({"age": True, "location": True})
    snap = to_snapshot(c, result, fetched_at="2026-07-09T00:00:00")
    assert snap == {
        "name": "Jane Public", "age": 36, "age_range": None,
        "locations": ["Boston, MA"], "relatives": ["R"],
        "signals": {"age": True, "location": True}, "score": 1.0,
        "source_url": "https://x", "fetched_at": "2026-07-09T00:00:00",
    }


# --- range-aware age matching ----------------------------------------------------

def _age_signal(**candidate_kwargs):
    c = CandidateListing(name="Jane Public", **candidate_kwargs)
    return compute_signals(c, _profile())["age"]


def test_age_signal_exact_within_one_year():
    from datetime import date
    age = date.today().year - 1990 - ((date.today().month, date.today().day) < (1, 15))
    assert _age_signal(age=age) is True
    assert _age_signal(age=age + 1) is True
    assert _age_signal(age=age + 2) is False


def test_age_signal_range_tests_containment():
    from datetime import date
    age = date.today().year - 1990 - ((date.today().month, date.today().day) < (1, 15))
    assert _age_signal(age_range=(age - 5, age + 5)) is True
    assert _age_signal(age_range=(age + 1, age + 9)) is False


def test_age_signal_exact_wins_over_range():
    assert _age_signal(age=99, age_range=(1, 120)) is False


def test_age_signal_uncomparable_without_age_or_dob():
    assert _age_signal() is None
    c = CandidateListing(name="Jane Public", age=36)
    assert compute_signals(c, _profile(date_of_birth=""))["age"] is None


def test_age_signal_tolerates_mm_dd_yyyy_dob():
    """DOB format is documented as ISO but shouldn't silently kill the age
    signal for the common MM/DD/YYYY alternate."""
    from datetime import date
    age = date.today().year - 1990 - ((date.today().month, date.today().day) < (1, 15))
    profile = _profile(date_of_birth="01/15/1990")
    c = CandidateListing(name="Jane Public", age=age)
    assert compute_signals(c, profile)["age"] is True


def test_age_signal_unrecognized_dob_format_is_none():
    profile = _profile(date_of_birth="Jan 15 1990")
    c = CandidateListing(name="Jane Public", age=36)
    assert compute_signals(c, profile)["age"] is None


def test_bucketed_age_can_earn_a_found_verdict():
    """411.com only reports decade buckets; without range matching a true match
    there could never collect its second signal and would cap at `possible`."""
    from datetime import date
    age = date.today().year - 1990 - ((date.today().month, date.today().day) < (1, 15))
    lo = age - (age % 10)
    c = CandidateListing(name="Jane Public", age_range=(lo, lo + 9), locations=["Boston, MA"])
    assert score_signals(compute_signals(c, _profile())).verdict == MATCH_FOUND
