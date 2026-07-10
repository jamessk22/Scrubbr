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
