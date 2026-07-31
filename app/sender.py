"""Delivery seam for outbound requests.

mailto_link() is the manual-assist fallback for form-only brokers: it produces
a mailto: link and copyable text, and the user does the actual sending. The
SmtpConfig/build_message/open_smtp/send functions below are the automatic path
for email-capable brokers, used by send_service.py.

    v2 ROADMAP:
      - Browser automation (Playwright) for form-only brokers.
"""
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import quote

from .templater import RenderedRequest


def mailto_link(to_addr: str, rendered: RenderedRequest) -> str:
    """A mailto: URL with subject and body prefilled for the user's mail client."""
    return (
        f"mailto:{to_addr}"
        f"?subject={quote(rendered.subject)}"
        f"&body={quote(rendered.body)}"
    )


@dataclass
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    from_addr: str
    use_ssl: bool = False
    min_delay_s: float = 5.0
    max_delay_s: float = 15.0

    @classmethod
    def from_dict(cls, cfg: dict) -> "SmtpConfig":
        smtp = cfg.get("smtp", {})
        username = smtp.get("username", "")
        return cls(
            host=smtp.get("host", ""),
            port=int(smtp.get("port", 587)),
            username=username,
            password=smtp.get("password", ""),
            from_addr=smtp.get("from_addr") or username,
            use_ssl=smtp.get("security", "starttls") == "ssl",
            min_delay_s=float(smtp.get("min_delay_s", 5.0)),
            max_delay_s=float(smtp.get("max_delay_s", 15.0)),
        )


def build_message(cfg: SmtpConfig, to_addr: str, rendered: RenderedRequest) -> EmailMessage:
    """No Reply-To: the From address must be the mailbox [imap] polls, so a
    broker's reply lands where inbox.poll() can classify it and auto-advance
    the request. Anything else silently breaks that correlation."""
    msg = EmailMessage()
    msg["From"] = cfg.from_addr
    msg["To"] = to_addr
    msg["Subject"] = rendered.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(rendered.body)
    return msg


def open_smtp(cfg: SmtpConfig):
    """An authenticated SMTP client. Caller is responsible for client.quit()."""
    if cfg.use_ssl:
        client = smtplib.SMTP_SSL(cfg.host, cfg.port)
    else:
        client = smtplib.SMTP(cfg.host, cfg.port)
        client.starttls()
    client.login(cfg.username, cfg.password)
    return client


def send(client, msg: EmailMessage) -> None:
    client.send_message(msg)
