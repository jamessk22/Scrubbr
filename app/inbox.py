"""IMAP reply monitoring: poll a mailbox, correlate replies to requests, and
classify them to auto-advance status. Never sends, deletes, or replies —
ambiguous mail is queued for manual review.
"""
import email
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header
from email.message import Message

from . import db
from .models import (
    STATUS_CONFIRMED,
    STATUS_NEEDS_VERIFICATION,
    STATUS_REJECTED,
)

# classification -> resulting request status
_STATUS_BY_CLASS = {
    "confirmed": STATUS_CONFIRMED,
    "rejected": STATUS_REJECTED,
    "needs_verification": STATUS_NEEDS_VERIFICATION,
}

_CONFIRM_RE = re.compile(
    r"\b(has been (deleted|removed|suppressed)|successfully (removed|deleted|opted out)"
    r"|your (data|information|record).{0,30}(deleted|removed)"
    r"|opt(ed)?[- ]out (is )?complete|removal (is )?complete|request (has been )?completed)\b",
    re.I,
)
_VERIFY_RE = re.compile(
    r"\b(verify your identity|identity verification|confirm your (identity|request)"
    r"|proof of (identity|residence)|upload.{0,20}(id|identification|driver)"
    r"|click.{0,20}(link|button).{0,20}confirm|to complete your request)\b",
    re.I,
)
_REJECT_RE = re.compile(
    r"\b(unable to (locate|find|verify)|no (matching )?record(s)? found"
    r"|we (cannot|can't|are unable to) (process|complete|fulfill)"
    r"|request (was )?(denied|rejected)|not subject to|do not qualify)\b",
    re.I,
)


@dataclass
class ImapConfig:
    host: str
    port: int
    username: str
    password: str
    folder: str = "INBOX"
    tag_prefix: str = "PIR"

    @classmethod
    def from_dict(cls, cfg: dict) -> "ImapConfig":
        imap = cfg.get("imap", {})
        app = cfg.get("app", {})
        return cls(
            host=imap.get("host", ""),
            port=int(imap.get("port", 993)),
            username=imap.get("username", ""),
            password=imap.get("password", ""),
            folder=imap.get("folder", "INBOX"),
            tag_prefix=app.get("request_tag_prefix", "PIR"),
        )


def classify(subject: str, body: str) -> str:
    """Return one of: confirmed, rejected, needs_verification, unknown.

    Verification demands are checked before confirmation because a "click to
    confirm" mail can contain both cues, and the safe action is to surface it.
    """
    text = f"{subject}\n{body}"
    if _VERIFY_RE.search(text):
        return "needs_verification"
    if _REJECT_RE.search(text):
        return "rejected"
    if _CONFIRM_RE.search(text):
        return "confirmed"
    return "unknown"


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _plain_body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # fall back to first text/html stripped of tags
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    return re.sub(r"<[^>]+>", " ", html)
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return ""


def _request_id_from_subject(subject: str, prefix: str) -> int | None:
    m = re.search(rf"{re.escape(prefix)}-(\d+)", subject)
    return int(m.group(1)) if m else None


@dataclass
class PollResult:
    scanned: int = 0
    matched: int = 0
    advanced: int = 0
    review: int = 0
    errors: list[str] | None = None


def poll(conn, cfg: ImapConfig, limit: int = 200) -> PollResult:
    """Fetch recent mail, classify, and update request statuses.

    Idempotent via seen_messages: a Message-ID is processed at most once.
    """
    result = PollResult(errors=[])
    try:
        client = imaplib.IMAP4_SSL(cfg.host, cfg.port)
        client.login(cfg.username, cfg.password)
        client.select(cfg.folder, readonly=True)
    except Exception as e:  # network/auth failures shouldn't crash the app
        result.errors.append(f"IMAP connection failed: {e}")
        return result

    try:
        typ, data = client.search(None, "ALL")
        if typ != "OK":
            result.errors.append("IMAP search failed")
            return result
        ids = data[0].split()[-limit:]
        for num in reversed(ids):
            typ, msg_data = client.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            message_id = msg.get("Message-ID", f"nomsgid-{num.decode()}")
            if db.message_seen(conn, message_id):
                continue
            result.scanned += 1

            subject = _decode(msg.get("Subject"))
            from_addr = _decode(msg.get("From"))
            body = _plain_body(msg)
            req_id = _request_id_from_subject(subject, cfg.tag_prefix)

            classification = classify(subject, body)
            needs_review = 0
            if req_id is not None and db.get_request(conn, req_id) is not None:
                result.matched += 1
                new_status = _STATUS_BY_CLASS.get(classification)
                if new_status:
                    db.set_status(
                        conn, req_id, new_status,
                        note=f"Auto from reply: {subject[:80]}",
                    )
                    result.advanced += 1
                else:
                    needs_review = 1
            else:
                # couldn't tie it to a request, or classification unknown
                needs_review = 1
                req_id = req_id if req_id else None

            if needs_review:
                result.review += 1

            db.record_message(conn, {
                "message_id": message_id,
                "request_id": req_id,
                "classification": classification,
                "subject": subject[:200],
                "from_addr": from_addr[:200],
                "received_at": datetime.now().isoformat(timespec="seconds"),
                "needs_review": needs_review,
            })
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return result
