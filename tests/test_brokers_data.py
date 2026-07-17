import json
import re
from pathlib import Path

import pytest

BROKERS_PATH = Path(__file__).resolve().parent.parent / "data" / "brokers.json"

CATEGORIES = {"people-search", "marketing", "risk", "recruitment"}
CONTACT_METHODS = {"email", "form", "both"}
JURISDICTIONS = {"US", "GDPR", "UK-GDPR"}
NETWORKS = {"peopleconnect", "whitepages", "beenverified", "tps-family", "ancestry", "spokeo", "radaris"}
REQUIRED = ("name", "category", "website", "contact_method", "jurisdiction", "difficulty")


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


@pytest.fixture(scope="module")
def brokers():
    data = json.loads(BROKERS_PATH.read_text(encoding="utf-8"))
    return data["brokers"]


def test_required_fields_present_and_nonempty(brokers):
    for b in brokers:
        for field in REQUIRED:
            assert field in b, f"{b.get('name', '?')} missing {field}"
            assert b[field] != "", f"{b.get('name', '?')} has empty {field}"


def test_enums(brokers):
    for b in brokers:
        assert b["category"] in CATEGORIES, f"{b['name']}: bad category {b['category']}"
        assert b["contact_method"] in CONTACT_METHODS, f"{b['name']}: bad contact_method"
        assert b["jurisdiction"] in JURISDICTIONS, f"{b['name']}: bad jurisdiction"


def test_difficulty_in_range(brokers):
    for b in brokers:
        assert isinstance(b["difficulty"], int), f"{b['name']}: difficulty not int"
        assert 1 <= b["difficulty"] <= 3, f"{b['name']}: difficulty out of range"


def test_unique_normalized_names(brokers):
    seen = {}
    for b in brokers:
        n = _norm(b["name"])
        assert n not in seen, f"duplicate normalized name: {b['name']!r} vs {seen[n]!r}"
        seen[n] = b["name"]


def test_scan_config_implies_search_url(brokers):
    for b in brokers:
        if "scan" in b:
            assert b.get("search_url"), f"{b['name']}: has scan config but no search_url"


def test_network_ids_are_known(brokers):
    for b in brokers:
        net = b.get("network", "")
        assert net == "" or net in NETWORKS, f"{b['name']}: unknown network {net!r}"


def test_expected_network_memberships(brokers):
    members = {}
    for b in brokers:
        if b.get("network"):
            members.setdefault(b["network"], set()).add(b["name"])
    assert {"PublicRecordsNow", "AnyWho"} <= members["peopleconnect"]
    assert "TruePeopleSearch.net" in members["tps-family"]
    # groups added for shared ownership need both members to actually propagate
    assert len(members["ancestry"]) >= 2
    assert len(members["spokeo"]) >= 2


def test_email_method_implies_email_present(brokers):
    for b in brokers:
        if b["contact_method"] in ("email", "both"):
            assert b.get("opt_out_email"), (
                f"{b['name']}: contact_method {b['contact_method']} but no opt_out_email"
            )
