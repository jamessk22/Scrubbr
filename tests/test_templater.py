import dataclasses

from app import templater
from app.models import Broker


def _broker(jurisdiction="US"):
    return Broker(id=1, name="Spokeo", category="people-search", jurisdiction=jurisdiction)


def _in_state(profile, state):
    return dataclasses.replace(profile, state=state)


def test_california_resident_gets_ccpa_citations_and_profile(profile):
    ca = _in_state(profile, "California")
    r = templater.render(_broker("US"), ca, 42, tag_prefix="PIR")
    assert r.tag == "PIR-42"
    assert "PIR-42" in r.subject
    assert "California resident" in r.body
    assert "1798.105" in r.body          # deletion right
    assert "1798.120" in r.body          # opt-out of sale
    assert "jane@example.com" in r.body  # profile email
    assert "jq@example.com" in r.body    # secondary email
    assert "Jane Q. Public" in r.body


def test_non_california_resident_never_claims_california(profile):
    # profile fixture is a Massachusetts resident.
    r = templater.render(_broker("US"), profile, 5)
    assert "California resident" not in r.body
    assert "1798.105" not in r.body
    assert "resident of Massachusetts" in r.body
    assert "opt me out of any sale" in r.body  # generic template body


def test_gdpr_broker_uses_gdpr_regardless_of_residency(profile):
    # GDPR follows the broker (EU establishment), not the user's US state.
    r = templater.render(_broker("GDPR"), profile, 7)
    assert "Article 17" in r.body
    assert "erasure" in r.body.lower()
    assert "1798" not in r.body


def test_empty_profile_fields_omitted(profile):
    profile.aliases = ""
    profile.phones = ""
    r = templater.render(_broker(), profile, 1)
    assert "Also known as" not in r.body
    assert "Phone number" not in r.body
