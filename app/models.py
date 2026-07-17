"""Plain dataclasses mirroring the SQLite rows. No ORM."""
from dataclasses import dataclass, field

# US state name -> USPS abbreviation, shared by URL building (scanner.py) and
# location normalization (extract.py/matcher.py) so there's one table to keep current.
STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


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

# Exposure: does this broker actually hold the profile's data?
EXPOSURE_UNKNOWN = "unknown"
EXPOSURE_FOUND = "found"
EXPOSURE_NOT_FOUND = "not_found"
EXPOSURE_POSSIBLE = "possible"  # low-probability match; user confirms or dismisses
EXPOSURE_UNREACHABLE = "unreachable"  # blocked/timeout/parse failure; manual link is the fallback
EXPOSURE_ASSUMED = "assumed"  # unsearchable broker: derived when no verdict is stored, or set by hand
EXPOSURE_LIKELY = "likely"  # derived, never stored: a same-network sibling was found
AUTO_EXPOSURE_STATUSES = {EXPOSURE_FOUND, EXPOSURE_NOT_FOUND, EXPOSURE_POSSIBLE, EXPOSURE_UNREACHABLE}

# Exposure `source` values. "manual" (set by hand on this broker) and "auto" (scan
# result) are inline literals elsewhere; "network" is a verdict propagated from a
# same-network sibling's manual mark.
EXPOSURE_SOURCE_NETWORK = "network"

# Consecutive unreachable scans before a broker's scan config is called out as drifting.
DRIFT_STREAK_THRESHOLD = 2

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
    search_url: str = ""  # GET template with {first}/{last}/{city}/{state}; empty = unsearchable
    scan_config: str = ""  # JSON: {search_url, result_selector, fields{}}; empty = no scan pipeline config
    network: str = ""  # shared-owner/database group; one sibling's `found` implies `likely` for the rest


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
class Exposure:
    id: int
    broker_id: int
    profile_id: int
    status: str = EXPOSURE_UNKNOWN
    source: str = ""  # auto | manual
    checked_at: str | None = None
    evidence: str = ""
    snapshot: str = ""  # JSON: matched listing detail (see matcher.py), for found/possible only
    fail_streak: int = 0  # consecutive `unreachable` scans; feeds the drift monitor


@dataclass
class CandidateListing:
    """A single result-card scraped from a broker's search page (pre-scoring)."""
    name: str = ""
    age: int | None = None
    age_range: tuple[int, int] | None = None  # inclusive [lo, hi] when the broker only buckets age ("70s", "65+")
    locations: list[str] = field(default_factory=list)  # ["City, ST", ...]
    relatives: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    raw_text: str = ""
    source_url: str = ""


def found_networks(brokers: list[Broker], exposures: "dict[int, Exposure]") -> dict[str, str]:
    """{network: name of a broker in it with a stored `found` verdict}.

    Only stored verdicts qualify, so a derived `likely` never seeds another
    `likely`, and a `possible` never promotes its siblings.
    """
    out: dict[str, str] = {}
    for b in brokers:
        exposure = exposures.get(b.id)
        if b.network and b.network not in out and exposure is not None and exposure.status == EXPOSURE_FOUND:
            out[b.network] = b.name
    return out


def effective_exposure(
    broker: Broker, exposure: "Exposure | None", networks: dict[str, str] | None = None
) -> str:
    """Precedence: the broker's own verdict (manual or auto) > network-derived
    `likely` > `unknown`/`assumed`."""
    if exposure is not None:
        return exposure.status
    if networks and broker.network in networks:
        return EXPOSURE_LIKELY
    return EXPOSURE_UNKNOWN if broker.search_url else EXPOSURE_ASSUMED


def row_visible(exposure_status: str, request_status: str, show_all: bool) -> bool:
    """Hide only rows that are provably clear and have no request in flight."""
    if show_all or request_status != STATUS_NOT_STARTED:
        return True
    return exposure_status != EXPOSURE_NOT_FOUND


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
