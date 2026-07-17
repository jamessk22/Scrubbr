import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "routes.db"

    def get_conn():
        conn = db.connect(db_path)
        db.init_db(conn)
        return conn

    monkeypatch.setattr(main, "get_conn", get_conn)

    conn = get_conn()
    db.upsert_broker(conn, {"name": "Acme Broker", "category": "people-search"})
    broker_id = next(b.id for b in db.all_brokers(conn) if b.name == "Acme Broker")
    profile = db.create_profile(conn, {
        "name": "Me", "full_name": "Jane Public", "aliases": "",
        "emails": "", "phones": "", "addresses": "",
        "date_of_birth": "", "state": "MA",
    })
    db.record_message(conn, {
        "message_id": "<abc/xyz@host>", "request_id": None,
        "classification": "unknown", "subject": "Re: request",
        "from_addr": "broker@x.test", "received_at": "2026-07-10T00:00:00",
        "needs_review": 1,
    })
    conn.commit()
    conn.close()

    c = TestClient(main.app)
    c.broker_id = broker_id
    c.profile_id = profile.id
    return c


def test_sort_rows_invalid_key_unchanged():
    rows = [{"n": 3}, {"n": 1}, {"n": 2}]
    assert main._sort_rows(rows, "bogus", "asc", {"n": lambda r: r["n"]}) is rows


def test_sort_rows_asc_desc():
    rows = [{"n": 3}, {"n": 1}, {"n": 2}]
    keys = {"n": lambda r: r["n"]}
    assert [r["n"] for r in main._sort_rows(rows, "n", "asc", keys)] == [1, 2, 3]
    assert [r["n"] for r in main._sort_rows(rows, "n", "desc", keys)] == [3, 2, 1]


def test_sort_rows_none_keys_last_both_directions():
    rows = [{"n": 2}, {"n": None}, {"n": 1}]
    keys = {"n": lambda r: r["n"]}
    assert [r["n"] for r in main._sort_rows(rows, "n", "asc", keys)] == [1, 2, None]
    assert [r["n"] for r in main._sort_rows(rows, "n", "desc", keys)] == [2, 1, None]


def test_exposure_rank_by_severity_not_alphabetical():
    assert main._EXPOSURE_RANK["found"] < main._EXPOSURE_RANK["not_found"]


def _seed_zeta(client):
    conn = main.get_conn()
    db.upsert_broker(conn, {"name": "Zeta Broker", "category": "people-search"})
    conn.commit()
    conn.close()


def test_dashboard_sort_broker_asc_desc(client):
    _seed_zeta(client)
    resp = client.get("/?show_all=1&sort=broker&dir=desc")
    assert resp.status_code == 200
    assert resp.text.index("Zeta Broker") < resp.text.index("Acme Broker")
    resp = client.get("/?show_all=1&sort=broker&dir=asc")
    assert resp.text.index("Acme Broker") < resp.text.index("Zeta Broker")


def test_brokers_sort_preserves_filter_in_header_link(client):
    resp = client.get("/brokers?category=people-search&sort=broker&dir=asc")
    assert resp.status_code == 200
    assert "category=people-search" in resp.text


def test_dashboard_bogus_sort_defaults(client):
    _seed_zeta(client)
    resp = client.get("/?show_all=1&sort=bogus&dir=sideways")
    assert resp.status_code == 200
    assert resp.text.index("Acme Broker") < resp.text.index("Zeta Broker")


def test_resolve_review_handles_slashed_message_id(client):
    """A raw RFC 5322 Message-ID with a '/' must resolve (bug: it lived in the
    URL path and 404'd)."""
    resp = client.post("/review/resolve", data={
        "message_id": "<abc/xyz@host>", "broker_id": client.broker_id,
        "profile_id": client.profile_id, "status": "confirmed",
    }, follow_redirects=False)
    assert resp.status_code == 303

    conn = main.get_conn()
    row = conn.execute(
        "SELECT needs_review FROM seen_messages WHERE message_id = ?", ("<abc/xyz@host>",)
    ).fetchone()
    conn.close()
    assert row["needs_review"] == 0


def test_resolve_review_bogus_broker_id_redirects_without_crash(client):
    """A nonexistent broker id from the free-text field must not 500 (FK
    IntegrityError) nor mark the item reviewed."""
    resp = client.post("/review/resolve", data={
        "message_id": "<abc/xyz@host>", "broker_id": 999999,
        "profile_id": client.profile_id, "status": "confirmed",
    }, follow_redirects=False)
    assert resp.status_code == 303

    conn = main.get_conn()
    row = conn.execute(
        "SELECT needs_review FROM seen_messages WHERE message_id = ?", ("<abc/xyz@host>",)
    ).fetchone()
    conn.close()
    assert row["needs_review"] == 1


def test_scan_mark_honors_relative_back_param(client):
    resp = client.post(f"/scan/{client.broker_id}/mark", data={
        "profile_id": client.profile_id, "status": "found", "back": "/brokers?category=marketing",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/brokers?category=marketing"


def test_scan_mark_rejects_offsite_back_param(client):
    """`back` must not be honored as an open redirect -- a protocol-relative
    '//' target or an absolute URL must fall back to the default redirect."""
    resp = client.post(f"/scan/{client.broker_id}/mark", data={
        "profile_id": client.profile_id, "status": "found", "back": "//evil.example/phish",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/scan?profile_id={client.profile_id}"

    resp = client.post(f"/scan/{client.broker_id}/mark", data={
        "profile_id": client.profile_id, "status": "found", "back": "https://evil.example/phish",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/scan?profile_id={client.profile_id}"


def _seed_network(client, names):
    conn = main.get_conn()
    for name in names:
        db.upsert_broker(conn, {"name": name, "category": "people-search", "network": "peopleconnect"})
    conn.commit()
    ids = {b.name: b.id for b in db.all_brokers(conn) if b.name in names}
    conn.close()
    return ids


def test_scan_mark_propagates_across_network(client):
    ids = _seed_network(client, ["Intelius", "TruthFinder", "US Search"])
    client.post(f"/scan/{ids['Intelius']}/mark", data={
        "profile_id": client.profile_id, "status": "found",
    }, follow_redirects=False)

    conn = main.get_conn()
    exps = db.exposures_for_profile(conn, client.profile_id)
    conn.close()
    assert exps[ids["Intelius"]].status == "found" and exps[ids["Intelius"]].source == "manual"
    for name in ("TruthFinder", "US Search"):
        assert exps[ids[name]].status == "found" and exps[ids[name]].source == "network"


def test_scan_mark_unknown_clears_propagated_but_keeps_manual(client):
    ids = _seed_network(client, ["Intelius", "TruthFinder", "US Search"])
    client.post(f"/scan/{ids['Intelius']}/mark", data={
        "profile_id": client.profile_id, "status": "found",
    }, follow_redirects=False)
    # A hand-set verdict on one sibling must survive the network clear.
    client.post(f"/scan/{ids['US Search']}/mark", data={
        "profile_id": client.profile_id, "status": "not_found",
    }, follow_redirects=False)
    client.post(f"/scan/{ids['Intelius']}/mark", data={
        "profile_id": client.profile_id, "status": "unknown",
    }, follow_redirects=False)

    conn = main.get_conn()
    exps = db.exposures_for_profile(conn, client.profile_id)
    conn.close()
    assert ids["Intelius"] not in exps      # anchor cleared
    assert ids["TruthFinder"] not in exps   # propagated sibling cleared
    assert exps[ids["US Search"]].source == "manual"  # own verdict kept


def test_scan_status_returns_empty_state_for_unknown_profile(client):
    resp = client.get("/scan/status", params={"profile_id": 999999})
    assert resp.status_code == 200
    assert resp.json() == {"running": False, "total": 0, "done": 0, "results": {}}


def test_scan_status_returns_snapshot_of_running_scan(client, monkeypatch):
    """/scan/status must serialize a stable copy even while the background
    scan thread is still writing into the same profile's results dict."""
    monkeypatch.setattr(main, "_bulk_scans", {
        client.profile_id: {"running": True, "total": 2, "done": 1, "results": {client.broker_id: {"status": "found", "detail": ""}}},
    })
    resp = client.get("/scan/status", params={"profile_id": client.profile_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is True
    assert body["results"] == {str(client.broker_id): {"status": "found", "detail": ""}}
