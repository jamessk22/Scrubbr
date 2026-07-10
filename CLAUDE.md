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
- **`sender.py`** — delivery seam. v1 only builds `mailto:` links (manual send). This is the
  designated insertion point for v2 SMTP/Playwright auto-send; keep routes calling through it.
- **Exposure scan pipeline** (`scanner.py` → `fetcher.py` → `extract.py` → `matcher.py` →
  `scan_service.py`/`ratelimit.py`) — checks whether a broker actually lists the profile, as
  opposed to `templater.py`'s "send them a removal request regardless." **`scanner.py`** only
  builds search URLs (`search_context`/`build_search_url`) — the manual "open search" link and
  the pipeline's fetch URL both go through it. `build_search_url` returns **None** if the profile
  can't fill a placeholder the template needs (e.g. a `{city}` template, no known city); `scan_service`
  maps that to `unreachable` "profile missing city" rather than fetching a garbage nationwide page.
  `page_scope` marks templates with no `{city}`/`{state}` as `unscoped`, which the snapshot records so
  a page-1 `not_found` on a paginated nationwide broker is shown as weak evidence. **`fetcher.py`** is
  the Playwright seam (persistent browser context in gitignored `.browser/`); it's the only module that
  touches the network, so everything above it is tested against canned HTML. `looks_like_challenge`
  checks **visible text only** (title + body, scripts stripped) — a results page routinely embeds an
  invisible reCAPTCHA iframe and the Cloudflare beacon, which would otherwise read as a bot wall.
  `SCRUBBR_DUMP_HTML=1` saves every rendered page to gitignored `.scans/` for `scripts.verify_scan`.
  **`extract.py`** turns result-page HTML into `CandidateListing`s via per-broker CSS selectors
  (`Broker.scan_config`, sourced from `data/brokers.json`'s `scan` key), with a generic name-adjacent
  regex fallback; a page with no candidates and no "no results" marker is `PARSE_FAILED`, never a silent
  not-found. The "no results" marker check also runs on **visible text only** (result pages ship JSON
  state blobs with fallback "no matches" copy). `parse_age` yields an exact age *or* an `age_range` for
  bucketed formats ("70s" → [70,79], "65+" → [65,120]); `_name_text` strips a nested age element out of
  the name (sites render `<h2>Name<span>Age 55</span></h2>`). **`matcher.py`** scores a candidate against
  the `Profile` (name gate, then age/location/state/alias/phone signals) — `found` needs ≥2 matched
  signals *and* ≥60% score, so a same-name/same-age stranger caps out at `possible`, never `found`. The
  age signal tests `±1` for an exact age and containment for an `age_range`. **`scan_service.scan_and_persist`**
  is what routes call: it checks for a prior *manual* verdict (never overwritten by an auto scan) before
  touching the network, then persists via `db.set_exposure` including a JSON `snapshot` of the matched
  listing. `scan.skip: true` (a live search page behind a JS wizard / empty shell / bot wall) is treated
  as non-scannable — `unreachable`, manual link only, without ever fetching (`is_skipped`/`is_auto_scannable`).
  **`ratelimit.py`** enforces global concurrency=1 and jittered per-domain spacing, and persists a
  cooldown after a 429/503 in the `scan_cooldowns` table. Exposure states: `unknown` (searchable,
  never scanned) → `found` / `not_found` / `possible` (low-confidence, user confirms/dismisses) /
  `unreachable` (blocked/timeout/parse failure, manual link is the fallback); `assumed` (no public
  search) and `likely` (a same-`network` sibling was `found`) are both **derived, never stored**.
  `models.effective_exposure` precedence: own verdict (manual or auto) > network-derived `likely` >
  `unknown`/`assumed`; `found_networks` only seeds `likely` from a **stored** `found`, so a `likely`
  never cascades and a `possible` never promotes siblings. **Network inference** (`Broker.network`,
  from `brokers.json`): `peopleconnect` (7), `whitepages` (2), `beenverified` (2), `tps-family` (5,
  heuristic). `scan_one_per_network` config flag makes a bulk sweep scan one sibling per network and
  let the rest inherit `likely`. **Drift monitor** (`exposures.fail_streak`, bumped on each consecutive
  `unreachable`, reset otherwise): `db.drifting_brokers` surfaces a "configs needing attention" list on
  the scan page (excluding `scan.skip` brokers, which are unreachable by design).
  **Only brokers with a `search_url` in `data/brokers.json` are scan-eligible at all**, and only those
  without `scan.skip` are *auto*-scannable — currently 8 verified auto-scannable configs (the five
  selector-based ones Whitepages/Radaris/FastPeopleSearch/TruePeopleSearch/That'sThem plus the three
  verified live 2026-07-09: Advanced Background Checks, USPhonebook, AnyWho). `scan.skip: true` marks a
  broker whose live search can't be driven by a plain GET from a residential IP (Spokeo/Nuwber wizards,
  411.com's client-rendered "NaN people found" shell, and several Cloudflare-walled sites —
  PeopleFinders/SearchPeopleFree/FamilyTreeNow/VoterRecords/Clustrmaps); `verified: false` means the
  selectors haven't been confirmed against a live page yet. A broker having a real public search in the
  wild (e.g. CheckPeople) doesn't make it scannable in-app — it stays in the "assumed"/"not publicly
  searchable" bucket until someone adds its `search_url`/`scan` config and verifies it. A `search_url`
  (and its `verified: false` `scan` stub) may be **sourced from web research at broker-addition time** —
  a plausible GET pattern with `notes: "URL pattern from web research …; not verified against a live
  rendered page"` — not only from a live scan. Such entries are auto-scannable via extract.py's generic
  fallback but stay untrusted until confirmed; `verify_scan` is still the only path to `verified: true` /
  `url_confirmed`. Growing this list
  is manual, broker-by-broker work: `python -m scripts.verify_scan "<broker>" [--fixture]` runs a live
  scan (reading the real profile from gitignored `scan_profile.toml`), prints extraction + match, and
  `--fixture` writes a PII-scrubbed fixture. Committed fixtures are **synthetic** ("Jane Public"), never
  raw dumps — raw pages carry the profile's and third parties' real data. Requires `playwright install
  chromium` once (see README); each live scan takes ~10-25s (browser launch + rate-limit delay).

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
- **A new broker's `search_url` may come from web research**, recorded with a `scan` stub of
  `{verified: false, search_url, notes: "URL pattern from web research <date>; not verified against a
  live rendered page"}`. Never set `verified: true` or invent CSS selectors at addition time — live
  confirmation via `scripts.verify_scan` is the only path to a trusted config. If a broker's search is
  wizard/POST-only or not URL-addressable, omit `search_url` and say why in `notes`.
