from app.models import Profile
from app.scanner import (
    SCOPE_SCOPED,
    SCOPE_UNSCOPED,
    build_search_url,
    missing_placeholders,
    page_scope,
    search_context,
)


def test_search_context(profile):
    assert search_context(profile) == {
        "first": "Jane", "last": "Public", "city": "Boston", "state": "MA",
    }


def test_search_context_incomplete_name():
    assert search_context(Profile(full_name="Cher")) is None
    assert search_context(Profile(full_name="")) is None


def test_build_search_url_path_style():
    url = build_search_url(
        "https://example.test/name/{first}-{last}/{city}-{state}",
        {"first": "Jane", "last": "Public", "city": "New York", "state": "NY"},
    )
    assert url == "https://example.test/name/Jane-Public/New-York-NY"


def test_build_search_url_query_style():
    url = build_search_url(
        "https://example.test/results?name={first}+{last}&citystatezip={city}+{state}",
        {"first": "Jane", "last": "Public", "city": "New York", "state": "NY"},
    )
    assert url == "https://example.test/results?name=Jane+Public&citystatezip=New+York+NY"


def test_search_context_without_city():
    p = Profile(full_name="Jane Public", addresses="", state="Massachusetts")
    assert search_context(p) == {"first": "Jane", "last": "Public", "city": "", "state": "MA"}


def test_build_search_url_none_when_placeholder_unfilled():
    ctx = {"first": "Jane", "last": "Public", "city": "", "state": "MA"}
    assert build_search_url("https://example.test/person/{first}-{last}/{state}/{city}", ctx) is None
    # a template that never asks for the missing value still builds
    assert build_search_url("https://example.test/{first}-{last}", ctx) == "https://example.test/Jane-Public"


def test_missing_placeholders_lists_only_unfilled_names():
    ctx = {"first": "Jane", "last": "Public", "city": "", "state": ""}
    assert missing_placeholders("https://x/{first}/{city}/{state}", ctx) == ["city", "state"]
    assert missing_placeholders("https://x/{first}-{last}", ctx) == []


def test_page_scope():
    assert page_scope("https://x/name/{first}-{last}/{city}-{state}") == SCOPE_SCOPED
    assert page_scope("https://x/person-search/{first}-{last}/{state}/{city}") == SCOPE_SCOPED
    assert page_scope("https://x/people/{first}+{last}") == SCOPE_UNSCOPED
