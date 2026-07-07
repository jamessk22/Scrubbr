"""Plain dataclasses mirroring the SQLite rows. No ORM."""
from dataclasses import dataclass, field


# Contact methods a broker accepts.
CONTACT_EMAIL = "email"
CONTACT_FORM = "form"
CONTACT_BOTH = "both"

# Request status pipeline.
STATUS_NOT_STARTED = "not_started"
STATUS_SENT = "sent"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUS_NEEDS_VERIFICATION = "needs_verification"

TERMINAL_STATUSES = {STATUS_CONFIRMED, STATUS_REJECTED}

# Follow-up cadence (days) by broker category.
FOLLOWUP_DAYS = {
    "people-search": 60,
    "marketing": 90,
    "risk": 90,
    "recruitment": 90,
}
DEFAULT_FOLLOWUP_DAYS = 90


@dataclass
class Broker:
    id: int
    name: str
    category: str
    website: str = ""
    opt_out_url: str = ""
    opt_out_email: str = ""
    contact_method: str = CONTACT_EMAIL
    jurisdiction: str = "CCPA"
    difficulty: int = 1  # 1 easy .. 3 hard (ID upload / phone / notarized)
    notes: str = ""


@dataclass
class Profile:
    id: int = 0
    name: str = "Me"  # short label distinguishing this profile from others, e.g. "Spouse"
    full_name: str = ""
    aliases: str = ""
    emails: str = ""
    phones: str = ""
    addresses: str = ""  # newline-separated, current first
    date_of_birth: str = ""
    state: str = ""  # US state of residence; drives which state-law template applies

    def email_list(self) -> list[str]:
        return [e.strip() for e in self.emails.replace(",", "\n").splitlines() if e.strip()]

    def primary_email(self) -> str:
        emails = self.email_list()
        return emails[0] if emails else ""

    def is_california(self) -> bool:
        return self.state.strip().lower() in ("ca", "california")


@dataclass
class Request:
    id: int
    broker_id: int
    profile_id: int
    status: str = STATUS_NOT_STARTED
    method: str = CONTACT_EMAIL
    sent_at: str | None = None
    next_due: str | None = None
    notes: str = ""
    history: list = field(default_factory=list)
