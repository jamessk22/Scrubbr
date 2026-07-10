from app import db
from app.models import (
    DRIFT_STREAK_THRESHOLD,
    EXPOSURE_ASSUMED,
    EXPOSURE_FOUND,
    EXPOSURE_LIKELY,
    EXPOSURE_NOT_FOUND,
    EXPOSURE_POSSIBLE,
    EXPOSURE_UNKNOWN,
    EXPOSURE_UNREACHABLE,
    STATUS_NOT_STARTED,
    STATUS_SENT,
    Broker,
    Exposure,
    effective_exposure,
    found_networks,
    row_visible,
)


def _seed_broker(conn, name="TestBroker", **extra):
    db.upsert_broker(conn, {"name": name, "category": "people-search", **extra})
    conn.commit()
    return next(b for b in db.all_brokers(conn) if b.name == name)


def test_set_exposure_upserts_single_row(conn, profile_id):
    b = _seed_broker(conn)
    db.set_exposure(conn, b.id, profile_id, EXPOSURE_FOUND, "auto", evidence="https://x/search")
    db.set_exposure(conn, b.id, profile_id, EXPOSURE_NOT_FOUND, "manual")
    rows = conn.execute("SELECT * FROM exposures").fetchall()
    assert len(rows) == 1
    exp = db.exposures_for_profile(conn, profile_id)[b.id]
    assert exp.status == EXPOSURE_NOT_FOUND
    assert exp.source == "manual"
    assert exp.checked_at


def test_exposures_for_profile_scoped(conn, profile_id):
    b = _seed_broker(conn)
    other = db.create_profile(conn, {
        "name": "Spouse", "full_name": "", "aliases": "", "emails": "",
        "phones": "", "addresses": "", "date_of_birth": "", "state": "",
    })
    db.set_exposure(conn, b.id, profile_id, EXPOSURE_FOUND, "auto")
    assert b.id in db.exposures_for_profile(conn, profile_id)
    assert db.exposures_for_profile(conn, other.id) == {}


def test_effective_exposure_derivation():
    searchable = Broker(id=1, name="A", category="people-search", search_url="https://x/{first}")
    unsearchable = Broker(id=2, name="B", category="marketing")
    assert effective_exposure(searchable, None) == EXPOSURE_UNKNOWN
    assert effective_exposure(unsearchable, None) == EXPOSURE_ASSUMED
    stored = Exposure(id=1, broker_id=1, profile_id=1, status=EXPOSURE_FOUND)
    assert effective_exposure(searchable, stored) == EXPOSURE_FOUND
    assert effective_exposure(unsearchable, stored) == EXPOSURE_FOUND


def test_search_url_migration_and_upsert(tmp_path):
    conn = db.connect(tmp_path / "old.db")
    conn.execute(
        """CREATE TABLE brokers (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               name TEXT NOT NULL UNIQUE,
               category TEXT NOT NULL,
               website TEXT DEFAULT '', opt_out_url TEXT DEFAULT '',
               opt_out_email TEXT DEFAULT '', contact_method TEXT DEFAULT 'email',
               jurisdiction TEXT DEFAULT 'CCPA', difficulty INTEGER DEFAULT 1,
               notes TEXT DEFAULT '')"""
    )
    conn.execute("INSERT INTO brokers (name, category) VALUES ('Old', 'people-search')")
    conn.commit()
    db.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(brokers)")}
    assert "search_url" in cols
    assert next(b for b in db.all_brokers(conn) if b.name == "Old").search_url == ""

    db.upsert_broker(conn, {"name": "Old", "category": "people-search",
                            "search_url": "https://x/{first}-{last}"})
    conn.commit()
    assert next(b for b in db.all_brokers(conn) if b.name == "Old").search_url == "https://x/{first}-{last}"
    conn.close()


def test_row_visible():
    # hidden only when: not show_all, no request in flight, provably not found
    assert not row_visible(EXPOSURE_NOT_FOUND, STATUS_NOT_STARTED, show_all=False)
    assert row_visible(EXPOSURE_NOT_FOUND, STATUS_NOT_STARTED, show_all=True)
    assert row_visible(EXPOSURE_NOT_FOUND, STATUS_SENT, show_all=False)
    for status in (EXPOSURE_FOUND, EXPOSURE_UNKNOWN, EXPOSURE_ASSUMED):
        assert row_visible(status, STATUS_NOT_STARTED, show_all=False)


# --- network inference: derived `likely` -------------------------------------------

def test_found_networks_only_from_stored_found():
    a = Broker(id=1, name="Intelius", category="people-search", network="peopleconnect")
    b = Broker(id=2, name="TruthFinder", category="people-search", network="peopleconnect")
    exposures = {1: Exposure(id=1, broker_id=1, profile_id=1, status=EXPOSURE_FOUND)}
    assert found_networks([a, b], exposures) == {"peopleconnect": "Intelius"}

    # a `possible` sibling never promotes the network (the tps-family grouping
    # is a heuristic; advisory only)
    exposures = {1: Exposure(id=1, broker_id=1, profile_id=1, status=EXPOSURE_POSSIBLE)}
    assert found_networks([a, b], exposures) == {}


def test_effective_exposure_derives_likely_from_sibling():
    sibling = Broker(id=2, name="TruthFinder", category="people-search", network="peopleconnect")
    networks = {"peopleconnect": "Intelius"}
    assert effective_exposure(sibling, None, networks) == EXPOSURE_LIKELY

    # no network, or a network with no `found` sibling, falls back as before
    solo = Broker(id=3, name="Radaris", category="people-search", search_url="https://x/{first}")
    assert effective_exposure(solo, None, networks) == EXPOSURE_UNKNOWN
    orphan = Broker(id=4, name="Ownerly", category="people-search", network="beenverified")
    assert effective_exposure(orphan, None, networks) == EXPOSURE_ASSUMED


def test_own_verdict_outranks_network_derivation():
    sibling = Broker(id=2, name="TruthFinder", category="people-search", network="peopleconnect")
    networks = {"peopleconnect": "Intelius"}
    own = Exposure(id=9, broker_id=2, profile_id=1, status=EXPOSURE_NOT_FOUND, source="manual")
    assert effective_exposure(sibling, own, networks) == EXPOSURE_NOT_FOUND


def test_likely_never_seeds_another_likely():
    """`likely` is derived at render time and never stored, so it can't cascade."""
    a = Broker(id=1, name="A", category="people-search", network="n1")
    b = Broker(id=2, name="B", category="people-search", network="n1")
    assert found_networks([a, b], {}) == {}


def test_network_column_migrates_and_round_trips(conn):
    db.upsert_broker(conn, {"name": "Intelius", "category": "people-search", "network": "peopleconnect"})
    conn.commit()
    assert next(b for b in db.all_brokers(conn) if b.name == "Intelius").network == "peopleconnect"


# --- drift monitor ------------------------------------------------------------------

def test_fail_streak_counts_consecutive_unreachables_and_resets(conn, profile_id):
    b = _seed_broker(conn)
    for _ in range(3):
        db.set_exposure(conn, b.id, profile_id, EXPOSURE_UNREACHABLE, "auto", snapshot='{"reason": "captcha"}')
    assert db.get_exposure(conn, b.id, profile_id).fail_streak == 3

    db.set_exposure(conn, b.id, profile_id, EXPOSURE_NOT_FOUND, "auto")
    assert db.get_exposure(conn, b.id, profile_id).fail_streak == 0


def test_drifting_brokers_lists_repeat_failures_with_reason(conn, profile_id):
    steady = _seed_broker(conn, name="Steady")
    rotten = _seed_broker(conn, name="Rotten")
    db.set_exposure(conn, steady.id, profile_id, EXPOSURE_UNREACHABLE, "auto")
    for _ in range(2):
        db.set_exposure(conn, rotten.id, profile_id, EXPOSURE_UNREACHABLE, "auto",
                        snapshot='{"reason": "Couldn\'t parse the results page"}')

    drift = db.drifting_brokers(conn, DRIFT_STREAK_THRESHOLD)
    assert [b.name for b, _, _ in drift] == ["Rotten"]  # one failure isn't drift yet
    _, streak, reason = drift[0]
    assert streak == 2
    assert reason == "Couldn't parse the results page"


def test_drifting_brokers_reason_matches_the_max_streak_profile(conn, profile_id):
    """In a multi-profile DB, the reported reason must come from whichever
    profile's exposure row actually has the MAX(fail_streak), not just
    whichever row the correlated subquery happened to pick."""
    broker = _seed_broker(conn)
    other_id = db.create_profile(conn, {
        "name": "Other", "full_name": "John Q. Other", "aliases": "", "emails": "",
        "phones": "", "addresses": "", "date_of_birth": "", "state": "",
    }).id

    db.set_exposure(conn, broker.id, profile_id, EXPOSURE_UNREACHABLE, "auto",
                     snapshot='{"reason": "captcha"}')
    for _ in range(2):
        db.set_exposure(conn, broker.id, other_id, EXPOSURE_UNREACHABLE, "auto",
                         snapshot='{"reason": "selector rot"}')

    drift = db.drifting_brokers(conn, DRIFT_STREAK_THRESHOLD)
    assert len(drift) == 1
    _, streak, reason = drift[0]
    assert streak == 2
    assert reason == "selector rot"
