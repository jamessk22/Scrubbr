# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.[[README]]

Scrubbr is a local, subscription-free reimplementation of Incogni's data-broker
removal workflow. It generates legal deletion/opt-out requests from your profile, tracks
per-broker status, and monitors an IMAP mailbox to auto-advance statuses from broker replies.
Everything runs on your machine; PII never leaves it. See `README.md` for product framing and
the **v2 roadmap** (Playwright form automation, scheduler, broker-list growth).

## Commands

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m scripts.seed_brokers        # (re)load data/brokers.json into SQLite
.venv/bin/uvicorn app.main:app --port 8137      # dev server, http://127.0.0.1:8137
.venv/bin/python -m pytest                       # full suite
.venv/bin/python -m pytest tests/test_inbox.py::test_classify_confirmation   # single test
```

The SQLite DB (`scrubbr.db`) and `config.toml` are gitignored and created on demand — `init_db`
runs on every request via `get_conn()`, and `seed_brokers` is idempotent (upsert by broker name).
Deleting `scrubbr.db` fully resets state; re-seed afterward. (`db.connect()` auto-renames a
pre-existing `incogni.db` to `scrubbr.db` on first connect, for anyone upgrading from before
the product rename.)

## Architecture

Python + FastAPI + SQLite (`sqlite3`, no ORM) + Jinja2. Server-rendered HTML, no front-end
framework. `app/main.py` holds every route; the rest of `app/` is a thin layered core:

- **`db.py`** — schema + all queries. Tables: `brokers`, `profiles` (multi-profile — one row
  per tracked person), `requests` (unique on `(broker_id, profile_id)`, **auto-created lazily**
  by `get_or_create_request`), `request_history`, `exposures` (also unique on `(broker_id,
  profile_id)` — the scan pipeline's per-profile verdict), `seen_messages` (makes IMAP polling
  idempotent). `set_status` is the only status mutator — it writes history and, on `sent`,
  stamps `next_due` from the broker's category.
- **`models.py`** — dataclasses + the status/contact-method/cadence constants. The status pipeline
  is `not_started → sent → confirmed | rejected | needs_verification`; `TERMINAL_STATUSES` and
  `FOLLOWUP_DAYS` (60 people-search / 90 else) live here.
- **`templater.py`** — renders a broker's request. **Template choice reflects who can
  invoke which law**: GDPR/UK-GDPR brokers always use the GDPR template (the broker is an
  EU/UK establishment); US brokers use a state-law template chosen from the *user's*
  residency (`profile.state`) — California → `ccpa.txt`, everyone else → `generic.txt`,
  which never asserts residency in a state the user doesn't live in. First template line
  is the `Subject:`, rest is body.
- **`inbox.py`** — IMAP poll + `classify()`. Replies are tied to a request via a `[PIR-<id>]`
  subject tag (`request_tag`); classification advances status, and anything ambiguous or
  unmatched is flagged `needs_review` for the review queue. **Never sends/replies/deletes.**
  Verification demands are checked before confirmations on purpose (safest when a reply has both).
- **`sender.py`** — delivery seam. Builds `mailto:` links (manual fallback for form brokers) and,
  for email-capable brokers, sends via SMTP (`SmtpConfig`, `build_message`, `open_smtp`, `send`).
  This is the designated insertion point for v2 Playwright form auto-send; keep routes calling
  through it.
- **`send_service.py`** — orchestrates automatic sending: `eligible_broker_ids` (email/both,
  `opt_out_email` set, request still `not_started`, exposure not `not_found`) and
  `send_and_persist`, which only calls `db.set_status(..., STATUS_SENT)` after SMTP accepts the
  message — a failure leaves the request `not_started` so the next run retries it, and writes no
  history row. **`sender.build_message` never sets `Reply-To`**: the From address must be the same
  mailbox `[imap]` polls, or a broker's reply never reaches `inbox.poll()` and the request is stuck
  at `sent` forever.
- **Exposure scan pipeline** (`scanner.py` → `fetcher.py` → `extract.py` → `matcher.py` →
  `scan_service.py`/`ratelimit.py`) — checks whether a broker actually lists the profile, as
  opposed to `templater.py`'s "send them a removal request regardless." **`scanner.py`** only
  builds search URLs; `build_search_url` returns **None** if the profile can't fill a placeholder
  the template needs (e.g. `{city}`), which `scan_service` maps to `unreachable` rather than
  fetching a garbage nationwide page. **`fetcher.py`** is the only module that touches the network
  (Playwright, persistent context in gitignored `.browser/`) — everything above it is tested
  against canned HTML. `looks_like_challenge` and the "no results" marker check in **`extract.py`**
  both check **visible text only** (scripts stripped), since result pages routinely embed invisible
  reCAPTCHA/Cloudflare beacons and JSON state blobs with fallback "no matches" copy that would
  otherwise misread as a bot wall or false not-found. `extract.py` turns HTML into `CandidateListing`s
  via per-broker CSS selectors (`Broker.scan_config` from `data/brokers.json`'s `scan` key) with a
  generic regex fallback; no candidates + no "no results" marker is `PARSE_FAILED`, never a silent
  not-found. **`matcher.py`** scores a candidate against the `Profile` — `found` needs ≥2 matched
  signals *and* ≥60% score, so a same-name/same-age stranger caps out at `possible`, never `found`.
  **`scan_service.scan_and_persist`** is what routes call: it checks for a prior *manual* verdict
  (never overwritten by an auto scan) before touching the network. `scan.skip: true` (JS wizard /
  empty shell / bot wall) is treated as non-scannable — `unreachable`, manual link only, without
  ever fetching. **`ratelimit.py`** enforces global concurrency=1 and jittered per-domain spacing,
  with a persisted cooldown after a 429/503. Exposure states: `unknown` (never scanned) → `found` /
  `not_found` / `possible` (low-confidence, user confirms/dismisses) / `unreachable` (blocked/timeout/
  parse failure); `assumed` (no public search) is **derived when nothing is stored**, but is also one of
  the verdicts a user can set by hand (`main.EXPOSURE_CHOICES`, written with `source: manual`); `likely`
  (a same-`network` sibling was `found`) is **derived, never stored** — `likely` never cascades and
  `possible` never promotes siblings
  (`models.effective_exposure` precedence: own verdict > network-derived `likely` > `unknown`/`assumed`).
  Every badge in the UI is a dropdown of pill-styled options (`templates/_badges.html`): exposure posts to
  `/scan/{id}/mark` (picking `unknown` calls `db.clear_exposure`, deleting the row so the derived state
  returns), status posts to `/broker/{id}/status`. Both carry a `back` field so the badge redirects to the
  page it was clicked from — validate it through `_safe_redirect_target`, never trust it raw.
  **Drift monitor**: `exposures.fail_streak` bumps on consecutive `unreachable`; `db.drifting_brokers`
  surfaces a "needs attention" list (excluding `scan.skip` brokers).
  **Only brokers with a `search_url` in `data/brokers.json` are scan-eligible**, and only those without
  `scan.skip` are *auto*-scannable. `verified: false` means the selectors/URL haven't been confirmed
  against a live page — a `search_url` may be sourced from web research at broker-addition time (never
  invent selectors), and `scripts.verify_scan` is the only path to `verified: true`. A broker having a
  real public search in the wild doesn't make it scannable in-app until someone adds that config and
  verifies it. Growing this list is manual: `python -m scripts.verify_scan "<broker>" [--fixture]` runs
  a live scan against the profile in gitignored `scan_profile.toml`; `--fixture` writes a PII-scrubbed
  **synthetic** fixture (never a raw dump — raw pages carry real third-party data). Requires
  `playwright install chromium` once.

## Conventions specific to this repo

- **Auto-send is opt-in and initial-send only.** Gated on `[smtp].enabled`, driven from the `/send`
  page's Preview/Send buttons (never fires on a schedule). Only targets requests at `not_started`
  for `email`/`both` brokers whose effective exposure isn't `not_found`. Follow-up re-sends
  (`next_due`) and `form` brokers stay a manual click. Don't widen this scope without it being an
  explicit task.
- **`data/brokers.json` is the source of truth** for the broker registry — edit it and re-seed
  rather than writing brokers directly to the DB. A broker's `jurisdiction` marks its regime
  (`US` vs `GDPR`/`UK-GDPR`) and `category` sets its follow-up cadence, so those fields are
  load-bearing, not cosmetic. For US brokers the exact state-law template comes from the user's
  `profile.state`, not the broker.
- **The `[PIR-<id>]` subject tag is the correlation key** between an outbound request and its
  inbound reply. Any change to how subjects are generated must stay in sync with the regex in
  `inbox.py`.
- **Config falls back gracefully**: `config.py` loads `config.toml` if present, else
  `config.example.toml`, so the app runs with IMAP disabled out of the box.
- Broker opt-out URLs/emails drift constantly and are unverified — treat them as needing
  confirmation, not as ground truth.
- **A new broker's `search_url` may come from web research**, recorded with a `scan` stub of
  `{verified: false, search_url, notes: "URL pattern from web research <date>; not verified against a
  live rendered page"}`. Never set `verified: true` or invent CSS selectors at addition time — live
  confirmation via `scripts.verify_scan` is the only path to a trusted config. If a broker's search is
  wizard/POST-only or not URL-addressable, omit `search_url` and say why in `notes`.
