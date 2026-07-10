import email
import imaplib

from app import db, inbox
from app.inbox import _fallback_message_id, classify
from app.models import STATUS_CONFIRMED, STATUS_NOT_STARTED, STATUS_SENT


def _msg(headers: str) -> email.message.Message:
    return email.message_from_string(headers + "\n\nbody")


def test_fallback_message_id_is_stable_across_sequence_changes():
    """Mail without a Message-ID must get an id derived from stable headers,
    not the IMAP sequence number (which shifts as the mailbox changes)."""
    a = _msg("From: broker@x.test\nDate: Mon, 1 Jan 2026 10:00:00 -0000\nSubject: Re: request")
    b = _msg("From: broker@x.test\nDate: Mon, 1 Jan 2026 10:00:00 -0000\nSubject: Re: request")
    assert _fallback_message_id(a) == _fallback_message_id(b)


def test_fallback_message_id_differs_for_different_mail():
    a = _msg("From: broker@x.test\nDate: Mon, 1 Jan 2026 10:00:00 -0000\nSubject: Re: request")
    c = _msg("From: other@x.test\nDate: Tue, 2 Jan 2026 11:00:00 -0000\nSubject: Different")
    assert _fallback_message_id(a) != _fallback_message_id(c)


def test_classify_confirmation():
    assert classify("Re: CCPA Request [PIR-3]",
                    "Your information has been deleted from our database.") == "confirmed"
    assert classify("Removal complete", "We have successfully removed your record.") == "confirmed"


def test_classify_verification_demand():
    assert classify("Action required [PIR-3]",
                    "Please verify your identity by uploading a photo of your ID.") == "needs_verification"
    assert classify("Confirm your request",
                    "Click the link below to confirm your removal request.") == "needs_verification"


def test_classify_rejection():
    assert classify("Re: your request",
                    "We were unable to locate any matching records for you.") == "rejected"
    assert classify("Request denied", "Your request was denied.") == "rejected"


def test_classify_unknown_goes_to_review():
    assert classify("Hello", "Thanks for contacting us, an agent will respond soon.") == "unknown"


def test_verification_beats_confirmation_when_ambiguous():
    # Contains both a "deleted" cue and a verification demand -> surface for review, safest.
    text = "Your data will be deleted once you verify your identity."
    assert classify("[PIR-9]", text) == "needs_verification"


# --- poll() -----------------------------------------------------------------

def _raw_message(message_id: str, subject: str, body: str, from_addr: str = "broker@x.test") -> bytes:
    return (
        f"Message-ID: {message_id}\r\n"
        f"From: {from_addr}\r\n"
        f"Date: Mon, 1 Jan 2026 10:00:00 -0000\r\n"
        f"Subject: {subject}\r\n"
        f"Content-Type: text/plain\r\n\r\n{body}"
    ).encode()


class FakeImapClient:
    """Stands in for imaplib.IMAP4_SSL. Tracks which fetch spec (header-only
    vs full RFC822) was used per message id, so tests can assert poll() only
    pulls the full body for messages it hasn't seen before."""

    def __init__(self, messages: dict[bytes, bytes]):
        self.messages = messages
        self.header_fetches: list[bytes] = []
        self.body_fetches: list[bytes] = []

    def login(self, user, password):
        pass

    def select(self, folder, readonly=True):
        pass

    def search(self, charset, criteria):
        ids = sorted(self.messages.keys(), key=int)
        return "OK", [b" ".join(ids)]

    def fetch(self, num, spec):
        raw = self.messages[num]
        if "HEADER.FIELDS" in spec:
            self.header_fetches.append(num)
            msg = email.message_from_bytes(raw)
            header_only = email.message.Message()
            for h in ("Message-ID", "From", "Date", "Subject"):
                if msg.get(h) is not None:
                    header_only[h] = msg.get(h)
            return "OK", [(b"1 (...)", header_only.as_bytes())]
        self.body_fetches.append(num)
        return "OK", [(b"1 (...)", raw)]

    def logout(self):
        pass


def _install_fake_client(monkeypatch, messages: dict[bytes, bytes]) -> FakeImapClient:
    client = FakeImapClient(messages)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port: client)
    return client


def _cfg() -> inbox.ImapConfig:
    return inbox.ImapConfig(host="imap.test", port=993, username="u", password="p")


def _sent_request(conn, profile_id):
    db.upsert_broker(conn, {"name": "Spokeo", "category": "people-search"})
    broker = db.all_brokers(conn)[0]
    req = db.get_or_create_request(conn, broker.id, profile_id)
    db.set_status(conn, req.id, STATUS_SENT)
    return req


def test_poll_advances_in_flight_request(conn, profile_id, monkeypatch):
    req = _sent_request(conn, profile_id)
    raw = _raw_message("<m1@x.test>", f"Re: request [PIR-{req.id}]", "Your data has been deleted.")
    _install_fake_client(monkeypatch, {b"1": raw})

    result = inbox.poll(conn, _cfg())

    assert db.get_request(conn, req.id).status == STATUS_CONFIRMED
    assert result.advanced == 1
    assert result.review == 0


def test_poll_does_not_advance_not_started_request(conn, profile_id, monkeypatch):
    """A `not_started` request was never emailed -- a tagged 'reply' to it is
    suspect and must go to review, not silently advance the status."""
    db.upsert_broker(conn, {"name": "Spokeo", "category": "people-search"})
    broker = db.all_brokers(conn)[0]
    req = db.get_or_create_request(conn, broker.id, profile_id)

    raw = _raw_message("<m2@x.test>", f"Re: request [PIR-{req.id}]", "Your data has been deleted.")
    _install_fake_client(monkeypatch, {b"1": raw})

    result = inbox.poll(conn, _cfg())

    assert db.get_request(conn, req.id).status == STATUS_NOT_STARTED
    assert result.advanced == 0
    assert result.review == 1


def test_poll_does_not_downgrade_terminal_request(conn, profile_id, monkeypatch):
    """A stray or spoofed reply carrying a valid [PIR-<id>] tag must not flip
    an already-terminal (confirmed/rejected) request."""
    req = _sent_request(conn, profile_id)
    db.set_status(conn, req.id, STATUS_CONFIRMED)

    raw = _raw_message(
        "<m3@x.test>", f"Re: request [PIR-{req.id}]",
        "We were unable to locate any matching records for you.",
    )
    _install_fake_client(monkeypatch, {b"1": raw})

    result = inbox.poll(conn, _cfg())

    assert db.get_request(conn, req.id).status == STATUS_CONFIRMED
    assert result.advanced == 0
    assert result.review == 1


def test_poll_skips_full_fetch_for_already_seen_messages(conn, profile_id, monkeypatch):
    req = _sent_request(conn, profile_id)
    raw = _raw_message("<seen@x.test>", f"Re: request [PIR-{req.id}]", "Your data has been deleted.")
    db.record_message(conn, {
        "message_id": "<seen@x.test>", "request_id": req.id,
        "classification": "confirmed", "subject": "Re: request",
        "from_addr": "broker@x.test", "received_at": "2026-01-01T00:00:00",
        "needs_review": 0,
    })

    client = _install_fake_client(monkeypatch, {b"1": raw})
    result = inbox.poll(conn, _cfg())

    assert result.scanned == 0
    assert client.header_fetches == [b"1"]
    assert client.body_fetches == []
