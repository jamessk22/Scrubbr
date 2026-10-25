import threading

import pytest

from app import fetcher
from app.fetcher import looks_like_challenge

CHALLENGE = """
<html><head><title>Just a moment...</title></head>
<body><div id="challenge">Please complete the CAPTCHA to verify you are human.</div></body></html>
"""

# A genuine results page: an invisible reCAPTCHA iframe and the Cloudflare
# analytics beacon appear in the markup of USPhonebook, 411.com and ABC. Scanning
# raw HTML for "captcha"/"cloudflare" would flag all three as bot walls.
RESULTS_WITH_INVISIBLE_WIDGETS = """
<html><head><title>Jane Public in Massachusetts</title></head>
<body>
  <div class="card"><h2>Jane Public</h2><span>Age 36</span></div>
  <iframe src="https://www.google.com/recaptcha/api2/aframe" width="0" height="0" style="display: none"></iframe>
  <script defer src="https://static.cloudflareinsights.com/beacon.min.js"></script>
</body></html>
"""


def test_challenge_page_detected():
    assert looks_like_challenge(CHALLENGE)


def test_results_page_with_hidden_recaptcha_and_cf_beacon_is_not_a_challenge():
    assert not looks_like_challenge(RESULTS_WITH_INVISIBLE_WIDGETS)


def test_marker_in_visible_body_text_still_detected():
    assert looks_like_challenge("<html><body><p>Access denied.</p></body></html>")


class _FakePage:
    def __init__(self, html, status=200, url="https://example.test/results"):
        self._html = html
        self.status = status
        self.url = url
        self.closed = False

    def goto(self, url, timeout=None):
        import types
        return types.SimpleNamespace(status=self.status)

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def wait_for_selector(self, *args, **kwargs):
        pass

    def content(self):
        return self._html

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, html):
        self.html = html
        self.pages = []
        self.closed = False

    def new_page(self):
        page = _FakePage(self.html)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


def test_fetch_reuses_active_browser_session(monkeypatch):
    """When browser_session() has an active context, fetch() must use it
    instead of launching a fresh Chromium context per call -- the whole
    point of the session is to pay browser startup once per bulk scan."""
    fake = _FakeContext("<html><body>Jane Public, Age 40</body></html>")
    monkeypatch.setattr(fetcher, "_active_context", fake)
    monkeypatch.setattr(fetcher, "_active_owner", threading.get_ident())
    try:
        result = fetcher.fetch("https://example.test/search")
    finally:
        monkeypatch.setattr(fetcher, "_active_context", None)

    assert result.html == fake.html
    assert len(fake.pages) == 1
    assert fake.pages[0].closed is True  # per-fetch page is closed...
    assert fake.closed is False  # ...but fetch() must not close a context it doesn't own


def test_fetch_from_another_thread_than_the_session_owner_is_busy(monkeypatch):
    """Playwright's sync API is bound to the thread that started it; reusing a
    bulk scan's context from a request thread raises a greenlet error."""
    monkeypatch.setattr(fetcher, "_active_context", _FakeContext("<html></html>"))
    monkeypatch.setattr(fetcher, "_active_owner", -1)
    with fetcher._browser_lock:  # held by the owning session for its whole run
        with pytest.raises(fetcher.Busy):
            fetcher.fetch("https://example.test/search")


def test_fetch_without_session_is_busy_while_browser_dir_is_held(monkeypatch):
    """A second persistent context on the same .browser/ dir would hit
    Chromium's profile lock, so fetch() refuses instead of launching."""
    with fetcher._browser_lock:
        with pytest.raises(fetcher.Busy):
            fetcher.fetch("https://example.test/search")
