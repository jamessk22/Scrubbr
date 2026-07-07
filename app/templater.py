"""Renders a broker-specific legal request from the profile + a template.

The first template line is the Subject; the rest is the body. Template
selection is driven by the broker's jurisdiction field.
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

# Map a broker jurisdiction to its template file.
_JURISDICTION_TEMPLATE = {
    "CCPA": "ccpa.txt",
    "GDPR": "gdpr.txt",
    "UK-GDPR": "gdpr.txt",
    "generic": "generic.txt",
}


@dataclass
class RenderedRequest:
    subject: str
    body: str
    tag: str


def request_tag(request_id: int, prefix: str = "PIR") -> str:
    """Correlation token embedded in the subject, e.g. 'PIR-42'."""
    return f"{prefix}-{request_id}"


def template_for(broker: Broker) -> str:
    return _JURISDICTION_TEMPLATE.get(broker.jurisdiction, "generic.txt")


def render(
    broker: Broker, profile: Profile, request_id: int, tag_prefix: str = "PIR"
) -> RenderedRequest:
    tag = request_tag(request_id, tag_prefix)
    template = _env.get_template(template_for(broker))
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
