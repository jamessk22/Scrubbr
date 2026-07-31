"""Orchestrates automatic email sending: eligibility -> render -> send ->
persist. Routes call only this module; the smtplib dependency stays behind
sender.py, so everything here is testable by injecting a fake SMTP client.
"""
import random
import time

from . import db, sender, templater
from .models import (
    CONTACT_BOTH,
    CONTACT_EMAIL,
    EXPOSURE_NOT_FOUND,
    STATUS_NOT_STARTED,
    STATUS_SENT,
    Broker,
    effective_exposure,
    found_networks,
)


def eligible_broker_ids(conn, brokers: list[Broker], profile_id: int) -> list[int]:
    """Email/both brokers with an address, still at not_started, and not
    verdicted not_found -- initial sends only, per CLAUDE.md's manual-send-for-
    follow-ups convention."""
    exposures = db.exposures_for_profile(conn, profile_id)
    networks = found_networks(brokers, exposures)
    requests = db.ensure_requests(conn, profile_id)
    return [
        b.id for b in brokers
        if b.contact_method in (CONTACT_EMAIL, CONTACT_BOTH)
        and b.opt_out_email
        and requests[b.id].status == STATUS_NOT_STARTED
        and effective_exposure(b, exposures.get(b.id), networks) != EXPOSURE_NOT_FOUND
    ]


def send_and_persist(conn, broker: Broker, profile, prefix: str, scfg, client) -> dict:
    """`client=None` is a dry run: renders and creates the request but sends
    nothing and leaves status untouched. On send failure, nothing is persisted
    -- the request stays not_started so the next run retries it naturally."""
    req = db.get_or_create_request(conn, broker.id, profile.id)
    rendered = templater.render(broker, profile, req.id, prefix)
    if client is None:
        return {"status": "dry_run", "detail": broker.opt_out_email}
    msg = sender.build_message(scfg, broker.opt_out_email, rendered)
    sender.send(client, msg)
    db.set_status(conn, req.id, STATUS_SENT, note=f"Auto-sent to {broker.opt_out_email}")
    return {"status": "sent", "detail": broker.opt_out_email}


def pace(cfg, sleep=time.sleep) -> None:
    """Jittered pause between messages so a run doesn't look like a burst to
    the relay. Kept separate from ratelimit.py, which is per-domain and shares
    state with the scan pipeline -- a send run must not throttle scans."""
    sleep(random.uniform(cfg.min_delay_s, cfg.max_delay_s))
