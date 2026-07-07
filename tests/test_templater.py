from app import templater
from app.models import Broker


def _broker(jurisdiction="CCPA"):
    return Broker(id=1, name="Spokeo", category="people-search", jurisdiction=jurisdiction)


def test_ccpa_render_includes_tag_citations_and_profile(profile):
    r = templater.render(_broker("CCPA"), profile, 42, tag_prefix="PIR")
    assert r.tag == "PIR-42"
    assert "PIR-42" in r.subject
    assert "1798.105" in r.body          # deletion right
    assert "1798.120" in r.body          # opt-out of sale
    assert "jane@example.com" in r.body  # profile email
    assert "jq@example.com" in r.body    # secondary email
    assert "123 Main St, Springfield CA 90000" in r.body
    assert "Jane Q. Public" in r.body


def test_gdpr_template_selected_by_jurisdiction(profile):
    r = templater.render(_broker("GDPR"), profile, 7)
    assert "Article 17" in r.body
    assert "erasure" in r.body.lower()
    assert "1798" not in r.body


def test_generic_template_for_unknown_jurisdiction(profile):
    r = templater.render(_broker("something-else"), profile, 9)
    assert "delete all personal information" in r.body.lower()


def test_empty_profile_fields_omitted(profile):
    profile.aliases = ""
    profile.phones = ""
    r = templater.render(_broker(), profile, 1)
    assert "Also known as" not in r.body
    assert "Phone number" not in r.body
