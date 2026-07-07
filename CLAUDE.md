# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.[[README]]

Scrubbr is a local, subscription-free reimplementation of Incogni's data-broker
removal workflow. It generates legal deletion/opt-out requests from your profile, tracks
per-broker status, and monitors an IMAP mailbox to auto-advance statuses from broker replies.
Everything runs on your machine; PII never leaves it. See `README.md` for product framing and
the **v2 roadmap** (SMTP auto-send, Playwright form automation, scheduler, broker-list growth).

## Commands

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m scripts.seed_brokers        # (re)load data/brokers.json into SQLite
.venv/bin/uvicorn app.main:app --port 8137      # dev server, http://127.0.0.1:8137
.venv/bin/python -m pytest                       # full suite
.venv/bin/python -m pytest tests/test_inbox.py::test_classify_confirmation   # single test
```

The SQLite DB (`incogni.db`) and `config.toml` are gitignored and created on demand — `init_db`
runs on every request via `get_conn()`, and `seed_brokers` is idempotent (upsert by broker name).
Deleting `incogni.db` fully resets state; re-seed afterward.

## Architecture

Python + FastAPI + SQLite (`sqlite3`, no ORM) + Jinja2. Server-rendered HTML, no front-end
framework. `app/main.py` holds every route; the rest of `app/` is a thin layered core:

- **`db.py`** — schema + all queries. Tables: `brokers`, `profile` (single row, id=1),
  `requests` (one per broker, **auto-created lazily** by `get_or_create_request`),
  `request_history`, `seen_messages` (makes IMAP polling idempotent). `set_status` is the only
  status mutator — it writes history and, on `sent`, stamps `next_due` from the broker's category.
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
- **`sender.py`** — delivery seam. v1 only builds `mailto:` links (manual send). This is the
  designated insertion point for v2 SMTP/Playwright auto-send; keep routes calling through it.

## Conventions specific to this repo

- **v1 is manual-send by design.** The app presents requests and tracks status; the user does the
  actual emailing/form-filling. Don't add auto-sending without it being an explicit v2 task.
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
