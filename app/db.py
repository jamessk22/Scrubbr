"""SQLite access layer: schema, migrations, and typed queries."""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import DEFAULT_DB_PATH
from .models import (
    DEFAULT_FOLLOWUP_DAYS,
    EXPOSURE_UNREACHABLE,
    FOLLOWUP_DAYS,
    STATUS_SENT,
    TERMINAL_STATUSES,
    Broker,
    Exposure,
    Profile,
    Request,
)

_REQUESTS_TABLE = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_id INTEGER NOT NULL REFERENCES brokers(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'not_started',
    method TEXT DEFAULT 'email',
    sent_at TEXT,
    next_due TEXT,
    notes TEXT DEFAULT '',
    UNIQUE(broker_id, profile_id)
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS brokers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    website TEXT DEFAULT '',
    opt_out_url TEXT DEFAULT '',
    opt_out_email TEXT DEFAULT '',
    contact_method TEXT DEFAULT 'email',
    jurisdiction TEXT DEFAULT 'CCPA',
    difficulty INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    search_url TEXT DEFAULT '',
    scan_config TEXT DEFAULT '',
    network TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT 'Me',
    full_name TEXT DEFAULT '',
    aliases TEXT DEFAULT '',
    emails TEXT DEFAULT '',
    phones TEXT DEFAULT '',
    addresses TEXT DEFAULT '',
    date_of_birth TEXT DEFAULT '',
    state TEXT DEFAULT ''
);

""" + _REQUESTS_TABLE + """
CREATE TABLE IF NOT EXISTS request_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT DEFAULT ''
);

-- Whether a broker actually holds this profile's data ('assumed' is derived, never stored).
CREATE TABLE IF NOT EXISTS exposures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_id INTEGER NOT NULL REFERENCES brokers(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'unknown',
    source TEXT DEFAULT '',
    checked_at TEXT,
    evidence TEXT DEFAULT '',
    snapshot TEXT DEFAULT '',
    fail_streak INTEGER DEFAULT 0,
    UNIQUE(broker_id, profile_id)
);

-- Raw inbox messages already processed, so polling is idempotent.
CREATE TABLE IF NOT EXISTS seen_messages (
    message_id TEXT PRIMARY KEY,
    request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    classification TEXT,
    subject TEXT,
    from_addr TEXT,
    received_at TEXT,
    needs_review INTEGER DEFAULT 0
);

-- Per-domain backoff after a 429/503, so bulk scans skip cooled-down brokers.
CREATE TABLE IF NOT EXISTS scan_cooldowns (
    domain TEXT PRIMARY KEY,
    until TEXT NOT NULL
);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Rebuilding requests_old below would otherwise cascade-delete request_history
    # rows: SQLite auto-remaps request_history's FK to the renamed table, and
    # DROP TABLE cascades ON DELETE actions when foreign_keys is enabled.
    conn.execute("PRAGMA foreign_keys = OFF")
    _migrate(conn)
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate(conn: sqlite3.Connection) -> None:
    """Rebuilds DBs created before multi-profile support existed.

    Old schema: singleton `profile` table (id=1) and `requests` unique on
    broker_id alone. Both get folded into the new `profiles`/`requests(broker_id,
    profile_id)` shape, preserving row ids so request_history/seen_messages FKs
    stay valid.
    """
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "profile" in tables:
        old = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
        if old is not None:
            data = {k: old[k] for k in old.keys()}
            data.setdefault("state", "")
            conn.execute(
                """INSERT OR IGNORE INTO profiles
                     (id, name, full_name, aliases, emails, phones, addresses, date_of_birth, state)
                   VALUES (1, 'Me', :full_name, :aliases, :emails, :phones, :addresses, :date_of_birth, :state)""",
                data,
            )
        conn.execute("DROP TABLE profile")

    req_cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
    if req_cols and "profile_id" not in req_cols:
        conn.execute("ALTER TABLE requests RENAME TO requests_old")
        conn.executescript(_REQUESTS_TABLE)
        conn.execute(
            """INSERT INTO requests (id, broker_id, profile_id, status, method, sent_at, next_due, notes)
               SELECT id, broker_id, 1, status, method, sent_at, next_due, notes FROM requests_old"""
        )
        conn.execute("DROP TABLE requests_old")

    broker_cols = {r["name"] for r in conn.execute("PRAGMA table_info(brokers)")}
    if broker_cols and "search_url" not in broker_cols:
        conn.execute("ALTER TABLE brokers ADD COLUMN search_url TEXT DEFAULT ''")
    if broker_cols and "scan_config" not in broker_cols:
        conn.execute("ALTER TABLE brokers ADD COLUMN scan_config TEXT DEFAULT ''")
    if broker_cols and "network" not in broker_cols:
        conn.execute("ALTER TABLE brokers ADD COLUMN network TEXT DEFAULT ''")

    exposure_cols = {r["name"] for r in conn.execute("PRAGMA table_info(exposures)")}
    if exposure_cols and "snapshot" not in exposure_cols:
        conn.execute("ALTER TABLE exposures ADD COLUMN snapshot TEXT DEFAULT ''")
    if exposure_cols and "fail_streak" not in exposure_cols:
        conn.execute("ALTER TABLE exposures ADD COLUMN fail_streak INTEGER DEFAULT 0")


# --- Brokers ---------------------------------------------------------------

def _broker(row: sqlite3.Row) -> Broker:
    return Broker(**{k: row[k] for k in row.keys()})


def upsert_broker(conn: sqlite3.Connection, b: dict) -> None:
    conn.execute(
        """INSERT INTO brokers
             (name, category, website, opt_out_url, opt_out_email,
              contact_method, jurisdiction, difficulty, notes, search_url, scan_config, network)
           VALUES (:name, :category, :website, :opt_out_url, :opt_out_email,
              :contact_method, :jurisdiction, :difficulty, :notes, :search_url, :scan_config, :network)
           ON CONFLICT(name) DO UPDATE SET
              category=excluded.category, website=excluded.website,
              opt_out_url=excluded.opt_out_url, opt_out_email=excluded.opt_out_email,
              contact_method=excluded.contact_method, jurisdiction=excluded.jurisdiction,
              difficulty=excluded.difficulty, notes=excluded.notes,
              search_url=excluded.search_url, scan_config=excluded.scan_config,
              network=excluded.network""",
        {
            "website": "", "opt_out_url": "", "opt_out_email": "",
            "contact_method": "email", "jurisdiction": "CCPA",
            "difficulty": 1, "notes": "", "search_url": "", "scan_config": "", "network": "", **b,
        },
    )


def all_brokers(conn: sqlite3.Connection) -> list[Broker]:
    rows = conn.execute("SELECT * FROM brokers ORDER BY name").fetchall()
    return [_broker(r) for r in rows]


def get_broker(conn: sqlite3.Connection, broker_id: int) -> Broker | None:
    row = conn.execute("SELECT * FROM brokers WHERE id = ?", (broker_id,)).fetchone()
    return _broker(row) if row else None


# --- Profiles ----------------------------------------------------------------

def _profile(row: sqlite3.Row) -> Profile:
    return Profile(**{k: row[k] for k in row.keys()})


def all_profiles(conn: sqlite3.Connection) -> list[Profile]:
    rows = conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
    return [_profile(r) for r in rows]


def get_profile(conn: sqlite3.Connection, profile_id: int) -> Profile | None:
    row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return _profile(row) if row else None


def create_profile(conn: sqlite3.Connection, data: dict) -> Profile:
    cur = conn.execute(
        """INSERT INTO profiles (name, full_name, aliases, emails, phones, addresses, date_of_birth, state)
           VALUES (:name, :full_name, :aliases, :emails, :phones, :addresses, :date_of_birth, :state)""",
        data,
    )
    conn.commit()
    return get_profile(conn, cur.lastrowid)


def save_profile(conn: sqlite3.Connection, profile_id: int, data: dict) -> None:
    conn.execute(
        """UPDATE profiles SET name=:name, full_name=:full_name, aliases=:aliases,
             emails=:emails, phones=:phones, addresses=:addresses,
             date_of_birth=:date_of_birth, state=:state WHERE id = :id""",
        {**data, "id": profile_id},
    )
    conn.commit()


def delete_profile(conn: sqlite3.Connection, profile_id: int) -> None:
    conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    conn.commit()


# --- Requests --------------------------------------------------------------

def get_or_create_request(conn: sqlite3.Connection, broker_id: int, profile_id: int) -> Request:
    row = conn.execute(
        "SELECT * FROM requests WHERE broker_id = ? AND profile_id = ?", (broker_id, profile_id)
    ).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO requests (broker_id, profile_id) VALUES (?, ?)", (broker_id, profile_id)
        )
        conn.commit()
        return Request(id=cur.lastrowid, broker_id=broker_id, profile_id=profile_id)
    return _request(conn, row)


def ensure_requests(conn: sqlite3.Connection, profile_id: int) -> dict[int, Request]:
    """Bulk equivalent of get_or_create_request for every broker, scoped to one
    profile: one INSERT OR IGNORE + one SELECT instead of a query (and possibly
    an insert+commit) per broker. For list views only -- returned Requests have
    empty history, since list templates don't show it."""
    conn.execute(
        "INSERT OR IGNORE INTO requests (broker_id, profile_id) SELECT id, ? FROM brokers",
        (profile_id,),
    )
    conn.commit()
    rows = conn.execute("SELECT * FROM requests WHERE profile_id = ?", (profile_id,)).fetchall()
    return {
        row["broker_id"]: Request(
            id=row["id"], broker_id=row["broker_id"], profile_id=row["profile_id"],
            status=row["status"], method=row["method"], sent_at=row["sent_at"],
            next_due=row["next_due"], notes=row["notes"],
        )
        for row in rows
    }


def _request(conn: sqlite3.Connection, row: sqlite3.Row) -> Request:
    hist = conn.execute(
        "SELECT at, status, note FROM request_history WHERE request_id = ? ORDER BY at DESC, id DESC",
        (row["id"],),
    ).fetchall()
    return Request(
        id=row["id"], broker_id=row["broker_id"], profile_id=row["profile_id"], status=row["status"],
        method=row["method"], sent_at=row["sent_at"], next_due=row["next_due"],
        notes=row["notes"], history=[dict(h) for h in hist],
    )


def get_request(conn: sqlite3.Connection, request_id: int) -> Request | None:
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    return _request(conn, row) if row else None


def all_requests(conn: sqlite3.Connection, profile_id: int) -> dict[int, Request]:
    """Returns {broker_id: Request} for every broker, scoped to one profile (auto-created lazily)."""
    result: dict[int, Request] = {}
    for b in all_brokers(conn):
        result[b.id] = get_or_create_request(conn, b.id, profile_id)
    return result


def set_status(
    conn: sqlite3.Connection, request_id: int, status: str, note: str = ""
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    req = get_request(conn, request_id)
    if req is None:
        return
    fields = {"status": status}
    if status == STATUS_SENT:
        broker = get_broker(conn, req.broker_id)
        cadence = FOLLOWUP_DAYS.get(broker.category, DEFAULT_FOLLOWUP_DAYS) if broker else DEFAULT_FOLLOWUP_DAYS
        fields["sent_at"] = now
        fields["next_due"] = (date.today() + timedelta(days=cadence)).isoformat()
    conn.execute(
        f"UPDATE requests SET {', '.join(f'{k}=:{k}' for k in fields)} WHERE id = :id",
        {**fields, "id": request_id},
    )
    conn.execute(
        "INSERT INTO request_history (request_id, at, status, note) VALUES (?, ?, ?, ?)",
        (request_id, now, status, note),
    )
    conn.commit()


def due_requests(conn: sqlite3.Connection) -> list[Request]:
    """Sent, non-terminal requests whose next_due date has passed."""
    today = date.today().isoformat()
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    rows = conn.execute(
        f"""SELECT * FROM requests
            WHERE next_due IS NOT NULL AND next_due <= ?
              AND status NOT IN ({placeholders})""",
        (today, *TERMINAL_STATUSES),
    ).fetchall()
    return [_request(conn, r) for r in rows]


# --- Exposures ---------------------------------------------------------------

def set_exposure(
    conn: sqlite3.Connection, broker_id: int, profile_id: int,
    status: str, source: str, evidence: str = "", snapshot: str = "",
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    failed = status == EXPOSURE_UNREACHABLE
    conn.execute(
        """INSERT INTO exposures (broker_id, profile_id, status, source, checked_at, evidence, snapshot, fail_streak)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(broker_id, profile_id) DO UPDATE SET
              status=excluded.status, source=excluded.source,
              checked_at=excluded.checked_at, evidence=excluded.evidence,
              snapshot=excluded.snapshot,
              fail_streak=CASE WHEN excluded.fail_streak = 0 THEN 0 ELSE exposures.fail_streak + 1 END""",
        (broker_id, profile_id, status, source, now, evidence, snapshot, int(failed)),
    )
    conn.commit()


def drifting_brokers(conn: sqlite3.Connection, min_streak: int) -> list[tuple[Broker, int, str]]:
    """(broker, streak, last_reason) for brokers whose scans keep coming back
    `unreachable` for some profile -- selector rot and dead domains surface here
    as a chore queue instead of quietly degrading into "not found"."""
    rows = conn.execute(
        """SELECT b.*, MAX(e.fail_streak) AS streak,
                  (SELECT snapshot FROM exposures
                    WHERE broker_id = b.id ORDER BY fail_streak DESC LIMIT 1) AS last_snapshot
             FROM brokers b JOIN exposures e ON e.broker_id = b.id
            WHERE e.fail_streak >= ?
            GROUP BY b.id ORDER BY streak DESC, b.name""",
        (min_streak,),
    ).fetchall()
    out = []
    for r in rows:
        broker = _broker({k: r[k] for k in r.keys() if k not in ("streak", "last_snapshot")})
        try:
            reason = json.loads(r["last_snapshot"] or "{}").get("reason", "")
        except json.JSONDecodeError:
            reason = ""
        out.append((broker, r["streak"], reason))
    return out


def get_exposure(conn: sqlite3.Connection, broker_id: int, profile_id: int) -> Exposure | None:
    row = conn.execute(
        "SELECT * FROM exposures WHERE broker_id = ? AND profile_id = ?", (broker_id, profile_id)
    ).fetchone()
    return Exposure(**{k: row[k] for k in row.keys()}) if row else None


def exposures_for_profile(conn: sqlite3.Connection, profile_id: int) -> dict[int, Exposure]:
    rows = conn.execute("SELECT * FROM exposures WHERE profile_id = ?", (profile_id,)).fetchall()
    return {r["broker_id"]: Exposure(**{k: r[k] for k in r.keys()}) for r in rows}


# --- Scan cooldowns ----------------------------------------------------------

def set_cooldown(conn: sqlite3.Connection, domain: str, until_iso: str) -> None:
    conn.execute(
        """INSERT INTO scan_cooldowns (domain, until) VALUES (?, ?)
           ON CONFLICT(domain) DO UPDATE SET until=excluded.until""",
        (domain, until_iso),
    )
    conn.commit()


def get_cooldown(conn: sqlite3.Connection, domain: str) -> str | None:
    row = conn.execute("SELECT until FROM scan_cooldowns WHERE domain = ?", (domain,)).fetchone()
    return row["until"] if row else None


# --- Inbox bookkeeping -----------------------------------------------------

def message_seen(conn: sqlite3.Connection, message_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seen_messages WHERE message_id = ?", (message_id,)
    ).fetchone() is not None


def record_message(conn: sqlite3.Connection, msg: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO seen_messages
             (message_id, request_id, classification, subject, from_addr,
              received_at, needs_review)
           VALUES (:message_id, :request_id, :classification, :subject,
              :from_addr, :received_at, :needs_review)""",
        {"request_id": None, "classification": "", "subject": "",
         "from_addr": "", "received_at": "", "needs_review": 0, **msg},
    )
    conn.commit()


def review_queue(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM seen_messages WHERE needs_review = 1 ORDER BY received_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]
