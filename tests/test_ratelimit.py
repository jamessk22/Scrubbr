from datetime import datetime, timedelta

from app import ratelimit


class FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_domain_of():
    assert ratelimit.domain_of("https://www.truepeoplesearch.com/results?x=1") == "www.truepeoplesearch.com"


def test_wait_for_turn_same_domain_enforces_min_delay(monkeypatch):
    ratelimit._last_request.clear()
    monkeypatch.setattr(ratelimit, "_last_any", 0.0)
    clock = FakeClock(start=1000.0)

    ratelimit.wait_for_turn("a.test", 10, 10, clock=clock.now, sleep=clock.sleep)
    first = clock.t
    ratelimit.wait_for_turn("a.test", 10, 10, clock=clock.now, sleep=clock.sleep)
    second = clock.t

    assert second - first >= 10


def test_wait_for_turn_different_domain_only_needs_cross_domain_gap(monkeypatch):
    ratelimit._last_request.clear()
    monkeypatch.setattr(ratelimit, "_last_any", 0.0)
    clock = FakeClock(start=2000.0)

    ratelimit.wait_for_turn("a.test", 10, 10, clock=clock.now, sleep=clock.sleep)
    first = clock.t
    ratelimit.wait_for_turn("b.test", 10, 10, clock=clock.now, sleep=clock.sleep)
    second = clock.t

    # different domain shouldn't wait the full 10s same-domain delay, just the 2s floor
    assert ratelimit.MIN_CROSS_DOMAIN_DELAY_S <= second - first < 10


def test_wait_for_turn_no_wait_if_already_spaced_out(monkeypatch):
    ratelimit._last_request.clear()
    monkeypatch.setattr(ratelimit, "_last_any", 0.0)
    clock = FakeClock(start=3000.0)

    ratelimit.wait_for_turn("a.test", 5, 5, clock=clock.now, sleep=clock.sleep)
    clock.t += 100  # plenty of real elapsed time, not via sleep()
    before = clock.t
    ratelimit.wait_for_turn("a.test", 5, 5, clock=clock.now, sleep=clock.sleep)
    assert clock.t == before  # no additional sleep needed


def test_cooldown_persists_and_expires(conn):
    domain = "blocked.test"
    assert not ratelimit.is_cooled_down(conn, domain)

    ratelimit.set_cooldown(conn, domain, hours=6, now=datetime(2026, 1, 1, 12, 0, 0))
    assert ratelimit.is_cooled_down(conn, domain, now=datetime(2026, 1, 1, 13, 0, 0))
    assert not ratelimit.is_cooled_down(conn, domain, now=datetime(2026, 1, 1, 19, 0, 1))


def test_is_stale_never_checked():
    assert ratelimit.is_stale(None, 14)
    assert ratelimit.is_stale("", 14)


def test_is_stale_recent_check_is_not_stale():
    recent = (datetime.now() - timedelta(days=1)).isoformat()
    assert not ratelimit.is_stale(recent, 14)


def test_is_stale_old_check_is_stale():
    old = (datetime.now() - timedelta(days=15)).isoformat()
    assert ratelimit.is_stale(old, 14)
