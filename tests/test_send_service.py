import smtplib
from datetime import date, timedelta

import pytest

from app import db, send_service, sender
from app.models import EXPOSURE_FOUND, EXPOSURE_NOT_FOUND, FOLLOWUP_DAYS


class FakeSmtp:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)


class RaisingSmtp:
    def send_message(self, msg):
        raise smtplib.SMTPRecipientsRefused({})


@pytest.fixture
def db_profile(conn, profile_id):
    return db.get_profile(conn, profile_id)


def _broker(conn, **over):
    db.upsert_broker(conn, {
        "name": "Example Broker", "category": "people-search",
        "opt_out_email": "privacy@example.test", "contact_method": "both",
        **over,
    })
    conn.commit()
    return next(b for b in db.all_brokers(conn) if b.name == over.get("name", "Example Broker"))


SCFG = sender.SmtpConfig(host="h", port=587, username="u", password="p", from_addr="u@example.test")


def test_eligible_includes_email_and_both_excludes_form(conn, profile_id):
    email_b = _broker(conn, name="Email Broker", contact_method="email")
    both_b = _broker(conn, name="Both Broker", contact_method="both")
    form_b = _broker(conn, name="Form Broker", contact_method="form", opt_out_email="")

    ids = send_service.eligible_broker_ids(conn, db.all_brokers(conn), profile_id)
    assert set(ids) == {email_b.id, both_b.id}
    assert form_b.id not in ids


def test_eligible_excludes_broker_without_opt_out_email(conn, profile_id):
    b = _broker(conn, opt_out_email="")
    ids = send_service.eligible_broker_ids(conn, db.all_brokers(conn), profile_id)
    assert b.id not in ids


def test_eligible_only_not_started(conn, profile_id):
    sent_b = _broker(conn, name="Sent Broker")
    confirmed_b = _broker(conn, name="Confirmed Broker")
    not_started_b = _broker(conn, name="New Broker")

    req = db.get_or_create_request(conn, sent_b.id, profile_id)
    db.set_status(conn, req.id, "sent")
    req = db.get_or_create_request(conn, confirmed_b.id, profile_id)
    db.set_status(conn, req.id, "confirmed")

    ids = send_service.eligible_broker_ids(conn, db.all_brokers(conn), profile_id)
    assert ids == [not_started_b.id]


def test_eligible_skips_not_found_exposure(conn, profile_id):
    b = _broker(conn)
    db.set_exposure(conn, b.id, profile_id, EXPOSURE_NOT_FOUND, "manual")
    ids = send_service.eligible_broker_ids(conn, db.all_brokers(conn), profile_id)
    assert b.id not in ids


def test_eligible_includes_network_derived_likely(conn, profile_id):
    anchor = _broker(conn, name="Anchor Broker", network="shared-net")
    sibling = _broker(conn, name="Sibling Broker", network="shared-net")
    db.set_exposure(conn, anchor.id, profile_id, EXPOSURE_FOUND, "manual")

    ids = send_service.eligible_broker_ids(conn, db.all_brokers(conn), profile_id)
    assert sibling.id in ids


def test_send_and_persist_marks_sent_with_history_and_next_due(conn, db_profile):
    broker = _broker(conn)
    client = FakeSmtp()
    result = send_service.send_and_persist(conn, broker, db_profile, "PIR", SCFG, client)

    assert result["status"] == "sent"
    assert len(client.sent) == 1

    req = db.get_or_create_request(conn, broker.id, db_profile.id)
    assert req.status == "sent"
    assert req.sent_at is not None
    expected_due = (date.today() + timedelta(days=FOLLOWUP_DAYS["people-search"])).isoformat()
    assert req.next_due == expected_due
    assert "Auto-sent to privacy@example.test" in req.history[0]["note"]


def test_send_failure_leaves_request_not_started_and_writes_no_history(conn, db_profile):
    broker = _broker(conn)
    client = RaisingSmtp()
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        send_service.send_and_persist(conn, broker, db_profile, "PIR", SCFG, client)

    req = db.get_or_create_request(conn, broker.id, db_profile.id)
    assert req.status == "not_started"
    assert req.history == []


def test_dry_run_sends_nothing_and_persists_no_status(conn, db_profile):
    broker = _broker(conn)
    result = send_service.send_and_persist(conn, broker, db_profile, "PIR", SCFG, None)

    assert result["status"] == "dry_run"
    req = db.get_or_create_request(conn, broker.id, db_profile.id)
    assert req.status == "not_started"
