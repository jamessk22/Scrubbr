"""Politeness limits for the scan pipeline: strictly sequential execution,
jittered per-domain spacing, and a persisted cooldown after a 429/503 so a
broker that says "back off" stays skipped across scan runs, not just within
one process lifetime.
"""
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import urlparse

from . import db

MIN_CROSS_DOMAIN_DELAY_S = 2.0
DEFAULT_COOLDOWN_HOURS = 6

_lock = threading.Lock()
_last_request: dict[str, float] = {}  # domain -> monotonic timestamp of last fetch
_last_any: float = 0.0


@contextmanager
def scan_slot():
    """Held for the duration of one broker scan. Global concurrency=1: two
    scans (e.g. a manual click racing the bulk background task) never run
    at the same time, so the per-domain delay state below needs no locking
    of its own."""
    with _lock:
        yield


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def wait_for_turn(
    domain: str, min_delay_s: float, max_delay_s: float,
    clock=time.monotonic, sleep=time.sleep,
) -> None:
    """Blocks until it's polite to hit `domain` again: a random min..max
    delay since the last request to this same domain, and at least
    MIN_CROSS_DOMAIN_DELAY_S since the last request to any domain. Call
    immediately before every fetch, inside scan_slot()."""
    global _last_any
    last_domain = _last_request.get(domain)
    if last_domain is not None:
        required = random.uniform(min_delay_s, max_delay_s)
        remaining = required - (clock() - last_domain)
        if remaining > 0:
            sleep(remaining)
    if _last_any:
        remaining = MIN_CROSS_DOMAIN_DELAY_S - (clock() - _last_any)
        if remaining > 0:
            sleep(remaining)
    now = clock()
    _last_request[domain] = now
    _last_any = now


def is_cooled_down(conn: sqlite3.Connection, domain: str, now: datetime | None = None) -> bool:
    """True if `domain` is still in its post-429/503 backoff window."""
    until = db.get_cooldown(conn, domain)
    if not until:
        return False
    return (now or datetime.now()).isoformat() < until


def set_cooldown(
    conn: sqlite3.Connection, domain: str,
    hours: float = DEFAULT_COOLDOWN_HOURS, now: datetime | None = None,
) -> None:
    now = now or datetime.now()
    db.set_cooldown(conn, domain, (now + timedelta(hours=hours)).isoformat())


def is_stale(checked_at: str | None, rescan_after_days: int) -> bool:
    """True if a broker has never been checked, or was checked long enough
    ago to be worth spending a scan on again."""
    if not checked_at:
        return True
    try:
        checked = datetime.fromisoformat(checked_at)
    except ValueError:
        return True
    return datetime.now() - checked >= timedelta(days=rescan_after_days)
