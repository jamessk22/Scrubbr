import json

import pytest

from scripts import seed_brokers
from scripts.seed_brokers import load_brokers


def _write(root, filename, names):
    data = root / "data"
    data.mkdir(exist_ok=True)
    payload = {"brokers": [{"name": n, "category": "people-search"} for n in names]}
    (data / filename).write_text(json.dumps(payload))


def test_shared_only_when_no_local_file(tmp_path):
    _write(tmp_path, "brokers.json", ["Spokeo", "Radaris"])
    brokers, local_count = load_brokers(tmp_path)
    assert [b["name"] for b in brokers] == ["Spokeo", "Radaris"]
    assert local_count == 0


def test_local_file_appended(tmp_path):
    _write(tmp_path, "brokers.json", ["Spokeo"])
    _write(tmp_path, "brokers.local.json", ["My Local Site"])
    brokers, local_count = load_brokers(tmp_path)
    assert [b["name"] for b in brokers] == ["Spokeo", "My Local Site"]
    assert local_count == 1


def test_local_name_collision_exits(tmp_path):
    _write(tmp_path, "brokers.json", ["Fast People Search"])
    _write(tmp_path, "brokers.local.json", ["fastpeople-search"])
    with pytest.raises(SystemExit, match="'fastpeople-search'.*'Fast People Search'"):
        load_brokers(tmp_path)


def test_orphaned_names_lists_db_brokers_missing_from_the_loaded_files():
    loaded = [{"name": "Spokeo"}, {"name": "Acxiom"}]
    assert seed_brokers.orphaned_names(["Acxiom", "Old Broker", "Spokeo"], loaded) == ["Old Broker"]
    assert seed_brokers.orphaned_names(["Spokeo"], loaded) == []
