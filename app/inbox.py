"""IMAP reply monitoring: poll a mailbox, correlate replies to requests, and
classify them to auto-advance status. Never sends, deletes, or replies —
ambiguous mail is queued for manual review.
"""
import email
import hashlib
import imaplib
import re
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header
from email.message import Message

from . import db
from .models import (
    STATUS_CONFIRMED,
    STATUS_NEEDS_VERIFICATION,
    STATUS_REJECTED,
    STATUS_SENT,
)

# A request can only be auto-advanced by a reply while it's actually in
# flight. A `not_started` request was never emailed, and a terminal
# confirmed/rejected request shouldn't be flipped by a stray or spoofed
# reply carrying its tag -- either case goes to the review queue instead.
_ADVANCABLE_STATUSES = {STATUS_SENT, STATUS_NEEDS_VERIFICATION}

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


def _safe_decode(data: bytes, charset: str | None) -> str:
    """Real mail declares charsets Python doesn't know ("unknown-8bit", typos);
    raising on those would leave the message unseen and abort every poll."""
    try:
        return data.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(_safe_decode(text, enc))
        else:
            out.append(text)
    return "".join(out)


def _plain_body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return _safe_decode(payload, part.get_content_charset())
        # fall back to first text/html stripped of tags
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html = _safe_decode(payload, part.get_content_charset())
                    return re.sub(r"<[^>]+>", " ", html)
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        return _safe_decode(payload, msg.get_content_charset())
    return ""


def _fallback_message_id(msg: Message) -> str:
    """Stable synthetic id for mail lacking a Message-ID header. Derived from
    stable headers (From + Date + Subject), not the IMAP sequence number, which
    shifts as the mailbox changes and would cause reprocessing/collisions."""
    basis = "\x00".join(msg.get(h, "") for h in ("From", "Date", "Subject"))
    return "nomsgid-" + hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()


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
        client = imaplib.IMAP4_SSL(
            cfg.host, cfg.port, ssl_context=ssl.create_default_context()
        )
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
            # Headers-only pass first: computing the message id and checking
            # seen_messages doesn't need the full body, and most of the ~200
            # newest messages on a repeat poll are already seen.
            typ, hdr_data = client.fetch(
                num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM DATE SUBJECT)])"
            )
            if typ != "OK" or not hdr_data or not hdr_data[0]:
                continue
            hdr_msg = email.message_from_bytes(hdr_data[0][1])
            message_id = hdr_msg.get("Message-ID") or _fallback_message_id(hdr_msg)
            if db.message_seen(conn, message_id):
                continue

            typ, msg_data = client.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            result.scanned += 1

            subject = from_addr = ""
            req_id = None
            try:
                subject = _decode(msg.get("Subject"))
                from_addr = _decode(msg.get("From"))
                body = _plain_body(msg)
                req_id = _request_id_from_subject(subject, cfg.tag_prefix)

                classification = classify(subject, body)
                needs_review = 0
                req = db.get_request(conn, req_id) if req_id is not None else None
                if req is not None:
                    result.matched += 1
                    new_status = _STATUS_BY_CLASS.get(classification)
                    if new_status and req.status in _ADVANCABLE_STATUSES:
                        db.set_status(
                            conn, req_id, new_status,
                            note=f"Auto from reply: {subject[:80]}",
                        )
                        result.advanced += 1
                    else:
                        needs_review = 1
                else:
                    # Couldn't tie it to a request. A tag for a request that no
                    # longer exists must not be stored: the FK would reject it.
                    needs_review = 1
                    req_id = None
            except Exception as exc:
                # Still record it below: an unrecorded message stays unseen and
                # aborts every later poll at the same spot.
                result.errors.append(f"{message_id}: {exc}")
                classification, needs_review, req_id = "error", 1, None

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
