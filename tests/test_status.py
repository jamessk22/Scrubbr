from datetime import date, timedelta

from app import db
from app.models import (
    STATUS_CONFIRMED,
    STATUS_SENT,
)

BROKER = {"name": "Spokeo", "category": "people-search", "jurisdiction": "CCPA"}
PRIVATE_BROKER = {"name": "Acxiom", "category": "marketing", "jurisdiction": "CCPA"}


def _add(conn, b):
    db.upsert_broker(conn, b)
    return db.all_brokers(conn)


def test_status_transition_records_history(conn, profile_id):
    db.upsert_broker(conn, BROKER)
    broker = db.all_brokers(conn)[0]
    req = db.get_or_create_request(conn, broker.id, profile_id)
    db.set_status(conn, req.id, STATUS_SENT, note="first send")
    db.set_status(conn, req.id, STATUS_CONFIRMED, note="broker confirmed")
    fresh = db.get_request(conn, req.id)
    assert fresh.status == STATUS_CONFIRMED
    assert len(fresh.history) == 2
    assert fresh.history[0]["status"] == STATUS_CONFIRMED  # newest first


def test_sent_sets_followup_60_days_for_people_search(conn, profile_id):
    db.upsert_broker(conn, BROKER)
    broker = db.all_brokers(conn)[0]
    req = db.get_or_create_request(conn, broker.id, profile_id)
    db.set_status(conn, req.id, STATUS_SENT)
    fresh = db.get_request(conn, req.id)
    expected = (date.today() + timedelta(days=60)).isoformat()
    assert fresh.next_due == expected


def test_sent_sets_followup_90_days_for_marketing(conn, profile_id):
    db.upsert_broker(conn, PRIVATE_BROKER)
    broker = db.all_brokers(conn)[0]
    req = db.get_or_create_request(conn, broker.id, profile_id)
    db.set_status(conn, req.id, STATUS_SENT)
    fresh = db.get_request(conn, req.id)
    expected = (date.today() + timedelta(days=90)).isoformat()
    assert fresh.next_due == expected


def test_due_requests_excludes_terminal_and_future(conn, profile_id):
    db.upsert_broker(conn, BROKER)
    broker = db.all_brokers(conn)[0]
    req = db.get_or_create_request(conn, broker.id, profile_id)
    db.set_status(conn, req.id, STATUS_SENT)
    # freshly sent -> due in the future, not listed
    assert db.due_requests(conn) == []
    # force next_due into the past -> now listed
    conn.execute(
        "UPDATE requests SET next_due = ? WHERE id = ?",
        ((date.today() - timedelta(days=1)).isoformat(), req.id),
    )
    conn.commit()
    assert len(db.due_requests(conn)) == 1
    # once confirmed (terminal), it drops off the due list
    db.set_status(conn, req.id, STATUS_CONFIRMED)
    conn.execute(
        "UPDATE requests SET next_due = ? WHERE id = ?",
        ((date.today() - timedelta(days=1)).isoformat(), req.id),
    )
    conn.commit()
    assert db.due_requests(conn) == []
