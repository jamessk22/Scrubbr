"""Renders a broker-specific legal request from the profile + a template.

The first template line is the Subject; the rest is the body.

Template selection reflects who can actually invoke which law:
  - EU/UK brokers are governed by GDPR by virtue of being an EU/UK establishment
    (GDPR Art. 3(1)), so those always use the GDPR template regardless of where
    the user lives.
  - US brokers are governed by US state consumer-privacy law, and that right
    follows the *user's* residency, not the broker. Only California residents
    can invoke the CCPA; everyone else uses the generic multi-state template,
    which never asserts residency in a state the user doesn't live in.
"""
from dataclasses import dataclass

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ROOT
from .models import Broker, Profile

_TEMPLATE_DIR = ROOT / "data" / "request_templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=()),  # plain text, no HTML escaping
    trim_blocks=False,
    lstrip_blocks=False,
    keep_trailing_newline=True,
)

# Broker jurisdictions that are governed by GDPR (EU/UK establishment).
_GDPR_JURISDICTIONS = {"GDPR", "UK-GDPR"}


@dataclass
class RenderedRequest:
    subject: str
    body: str
    tag: str


def request_tag(request_id: int, prefix: str = "PIR") -> str:
    """Correlation token embedded in the subject, e.g. 'PIR-42'."""
    return f"{prefix}-{request_id}"


def template_for(broker: Broker, profile: Profile) -> str:
    if broker.jurisdiction in _GDPR_JURISDICTIONS:
        return "gdpr.txt"
    # US broker: the applicable state law is the user's, not the broker's.
    if profile.is_california():
        return "ccpa.txt"
    return "generic.txt"


def render(
    broker: Broker, profile: Profile, request_id: int, tag_prefix: str = "PIR"
) -> RenderedRequest:
    tag = request_tag(request_id, tag_prefix)
    template = _env.get_template(template_for(broker, profile))
    text = template.render(broker=broker, profile=profile, tag=tag)

    lines = text.splitlines()
    subject = ""
    body_start = 0
    for i, line in enumerate(lines):
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip() + "\n"
    return RenderedRequest(subject=subject, body=body, tag=tag)
