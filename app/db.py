"""SQLite access layer: schema, migrations, and typed queries."""
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import DEFAULT_DB_PATH
from .models import (
    DEFAULT_FOLLOWUP_DAYS,
    FOLLOWUP_DAYS,
    STATUS_NOT_STARTED,
    STATUS_SENT,
    TERMINAL_STATUSES,
    Broker,
    Profile,
    Request,
)

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
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    full_name TEXT DEFAULT '',
    aliases TEXT DEFAULT '',
    emails TEXT DEFAULT '',
    phones TEXT DEFAULT '',
    addresses TEXT DEFAULT '',
    date_of_birth TEXT DEFAULT '',
    state TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_id INTEGER NOT NULL UNIQUE REFERENCES brokers(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'not_started',
    method TEXT DEFAULT 'email',
    sent_at TEXT,
    next_due TEXT,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS request_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT DEFAULT ''
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
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO profile (id) VALUES (1)")
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for DBs created before a column existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(profile)")}
    if "state" not in cols:
        conn.execute("ALTER TABLE profile ADD COLUMN state TEXT DEFAULT ''")


# --- Brokers ---------------------------------------------------------------

def _broker(row: sqlite3.Row) -> Broker:
    return Broker(**{k: row[k] for k in row.keys()})


def upsert_broker(conn: sqlite3.Connection, b: dict) -> None:
    conn.execute(
        """INSERT INTO brokers
             (name, category, website, opt_out_url, opt_out_email,
              contact_method, jurisdiction, difficulty, notes)
           VALUES (:name, :category, :website, :opt_out_url, :opt_out_email,
              :contact_method, :jurisdiction, :difficulty, :notes)
           ON CONFLICT(name) DO UPDATE SET
              category=excluded.category, website=excluded.website,
              opt_out_url=excluded.opt_out_url, opt_out_email=excluded.opt_out_email,
              contact_method=excluded.contact_method, jurisdiction=excluded.jurisdiction,
              difficulty=excluded.difficulty, notes=excluded.notes""",
        {
            "website": "", "opt_out_url": "", "opt_out_email": "",
            "contact_method": "email", "jurisdiction": "CCPA",
            "difficulty": 1, "notes": "", **b,
        },
    )


def all_brokers(conn: sqlite3.Connection) -> list[Broker]:
    rows = conn.execute("SELECT * FROM brokers ORDER BY name").fetchall()
    return [_broker(r) for r in rows]


def get_broker(conn: sqlite3.Connection, broker_id: int) -> Broker | None:
    row = conn.execute("SELECT * FROM brokers WHERE id = ?", (broker_id,)).fetchone()
    return _broker(row) if row else None


# --- Profile ---------------------------------------------------------------

def get_profile(conn: sqlite3.Connection) -> Profile:
    row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    return Profile(**{k: row[k] for k in row.keys()})


def save_profile(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        """UPDATE profile SET full_name=:full_name, aliases=:aliases,
             emails=:emails, phones=:phones, addresses=:addresses,
             date_of_birth=:date_of_birth, state=:state WHERE id = 1""",
        data,
    )
    conn.commit()


# --- Requests --------------------------------------------------------------

def get_or_create_request(conn: sqlite3.Connection, broker_id: int) -> Request:
    row = conn.execute("SELECT * FROM requests WHERE broker_id = ?", (broker_id,)).fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO requests (broker_id) VALUES (?)", (broker_id,))
        conn.commit()
        return Request(id=cur.lastrowid, broker_id=broker_id)
    return _request(conn, row)


def _request(conn: sqlite3.Connection, row: sqlite3.Row) -> Request:
    hist = conn.execute(
        "SELECT at, status, note FROM request_history WHERE request_id = ? ORDER BY at DESC, id DESC",
        (row["id"],),
    ).fetchall()
    return Request(
        id=row["id"], broker_id=row["broker_id"], status=row["status"],
        method=row["method"], sent_at=row["sent_at"], next_due=row["next_due"],
        notes=row["notes"], history=[dict(h) for h in hist],
    )


def get_request(conn: sqlite3.Connection, request_id: int) -> Request | None:
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    return _request(conn, row) if row else None


def all_requests(conn: sqlite3.Connection) -> dict[int, Request]:
    """Returns {broker_id: Request} for every broker (auto-created lazily)."""
    result: dict[int, Request] = {}
    for b in all_brokers(conn):
        result[b.id] = get_or_create_request(conn, b.id)
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
