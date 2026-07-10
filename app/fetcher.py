"""Playwright fetch layer: the only part of the scan pipeline that touches
the network or a browser. Everything above this (extract/matcher/scan_service)
runs against canned HTML in tests -- this module is the seam, same convention
as sender.py for outbound email.
"""
import contextlib
import os
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .config import ROOT, load_config

BROWSER_DIR = ROOT / ".browser"
DUMP_DIR = ROOT / ".scans"  # gitignored: raw pages contain real listings

# Holds the persistent context opened by browser_session(), if any, so fetch()
# can reuse it instead of paying browser startup per call. ratelimit.py caps
# scan concurrency at 1, so a plain module-level slot (not per-thread) is safe.
_active_context = None

_BLOCKED_STATUS = {403, 429, 503}
_BLOCKED_MARKERS = (
    "captcha", "just a moment", "cloudflare", "access denied",
    "are you a robot", "unusual traffic", "verify you are human",
)


def looks_like_challenge(html: str) -> bool:
    """Challenge markers are only trusted in the title and visible body text.
    Real results pages routinely embed an invisible reCAPTCHA iframe and the
    Cloudflare analytics beacon, so scanning raw markup flags a page full of
    listings as a bot wall (USPhonebook, 411.com and ABC all do this)."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup.select("script, style, noscript, iframe, link, meta"):
        tag.decompose()
    haystack = f"{title} {soup.get_text(' ', strip=True)[:5000]}".lower()
    return any(m in haystack for m in _BLOCKED_MARKERS)


class Blocked(Exception):
    """Raised on a CAPTCHA/challenge page or a 403/429/503 response.
    Callers map this to the `unreachable` outcome."""

    def __init__(self, reason: str, status: int = 0):
        self.reason = reason
        self.status = status
        super().__init__(reason)


@dataclass
class FetchResult:
    final_url: str
    html: str
    status: int


def scan_settings() -> dict:
    cfg = load_config().get("scan", {})
    return {
        "headless": cfg.get("headless", True),
        "nav_timeout_s": cfg.get("nav_timeout_s", 25),
        "min_delay_s": cfg.get("min_delay_s", 8),
        "max_delay_s": cfg.get("max_delay_s", 20),
        "rescan_after_days": cfg.get("rescan_after_days", 14),
        "one_per_network": cfg.get("scan_one_per_network", False),
    }


def _dump_html(url: str, html: str) -> None:
    """SCRUBBR_DUMP_HTML=1 saves every rendered page, so `scripts.verify_scan`
    can write selectors against exactly what the browser saw -- including the
    challenge pages that raise Blocked below."""
    DUMP_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[:100]
    (DUMP_DIR / f"{slug}.html").write_text(html)


@contextlib.contextmanager
def browser_session():
    """Opens one persistent Chromium context and holds it open for the
    duration of the `with` block, so a bulk scan (many fetch() calls in a
    row) pays browser startup once instead of per broker. fetch() picks up
    the active context automatically; callers that don't use this still get
    today's launch-per-call behavior."""
    from playwright.sync_api import sync_playwright

    global _active_context
    settings = scan_settings()
    BROWSER_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(BROWSER_DIR), headless=settings["headless"])
        _active_context = context
        try:
            yield
        finally:
            _active_context = None
            context.close()


def _load(context, url: str, result_selector: str, settings: dict) -> tuple[str, str, int]:
    nav_timeout_ms = settings["nav_timeout_s"] * 1000
    page = context.new_page()
    try:
        response = page.goto(url, timeout=nav_timeout_ms)
        status = response.status if response else 0
        try:
            page.wait_for_load_state("networkidle", timeout=nav_timeout_ms)
        except Exception:
            pass  # some sites never go idle (polling widgets); fall through with whatever loaded
        if result_selector:
            try:
                page.wait_for_selector(result_selector, timeout=5000)
            except Exception:
                pass  # selector may be stale or genuinely absent (no results) -- extract.py decides
        html = page.content()
        final_url = page.url
    finally:
        page.close()
    return html, final_url, status


def fetch(url: str, result_selector: str = "") -> FetchResult:
    """Loads `url` in a persistent Chromium context (cookies/fingerprint
    accumulate across runs like a real user, in a gitignored .browser/ dir),
    waits for the page to settle, and returns its rendered HTML.

    Reuses the context opened by browser_session() if one is active;
    otherwise launches and tears down its own, as before.

    Raises Blocked on a CAPTCHA/challenge page or a 403/429/503 status.
    """
    settings = scan_settings()

    if _active_context is not None:
        html, final_url, status = _load(_active_context, url, result_selector, settings)
    else:
        from playwright.sync_api import sync_playwright  # lazy: only this path needs a browser

        BROWSER_DIR.mkdir(exist_ok=True)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(str(BROWSER_DIR), headless=settings["headless"])
            try:
                html, final_url, status = _load(context, url, result_selector, settings)
            finally:
                context.close()

    if os.getenv("SCRUBBR_DUMP_HTML"):
        _dump_html(final_url, html)

    if status in _BLOCKED_STATUS:
        raise Blocked(f"HTTP {status}", status)
    if looks_like_challenge(html):
        raise Blocked("challenge page detected", status)
    return FetchResult(final_url=final_url, html=html, status=status)
