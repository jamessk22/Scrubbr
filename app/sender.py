"""Delivery seam for outbound requests.

v1 is manual-assist only: we produce a mailto: link and copyable text, and the
user does the actual sending. This module isolates "how a request leaves the
machine" so a v2 SMTP auto-sender can drop in behind the same interface without
touching routes or templates.

    v2 ROADMAP:
      - SmtpSender(config).send(rendered, to_addr) -> sends via smtplib,
        returns a delivery receipt; routes call sender.deliver(...) instead of
        rendering a mailto link.
      - Browser automation (Playwright) for form-only brokers, behind the same
        deliver() call, dispatched by broker.contact_method.
"""
from urllib.parse import quote

from .templater import RenderedRequest


def mailto_link(to_addr: str, rendered: RenderedRequest) -> str:
    """A mailto: URL with subject and body prefilled for the user's mail client."""
    return (
        f"mailto:{to_addr}"
        f"?subject={quote(rendered.subject)}"
        f"&body={quote(rendered.body)}"
    )
