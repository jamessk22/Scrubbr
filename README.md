# Scrubbr

A local, self-hosted data-broker removal assistant — a subscription-free
reimplementation of [Incogni](https://incogni.com/)'s core workflow that keeps
your personal data on your own machine.

Incogni is a paid service that acts as your *authorized agent* to get your data
deleted from ~400+ data brokers under CCPA/GDPR. This tool does the same job
locally. Because **you** are the data subject (not a third-party agent), your
requests carry full legal weight with no authorization paperwork, and your PII
never leaves your computer.

## What it does (v1)

- **Broker directory** — 130 curated high-priority brokers (124 US, 6 GDPR/UK-GDPR;
  people-search, marketing, risk, recruitment) with opt-out URLs/emails, contact
  method, legal basis, and difficulty. Seeded from `data/brokers.json`; easy to
  extend. Brokers whose sites have been taken down (currently Radaris, Rehold,
  Verecor) are flagged `defunct`: kept with their history, but excluded from the
  dashboard count, sending, and scanning.
- **Local profiles** — one or more people's identifiers (name, aliases, emails,
  phones, addresses, DOB, state), stored in SQLite, used only to fill request
  templates.
- **Request generation** — per-broker legal deletion + opt-out requests from
  Jinja2 templates. GDPR/UK-GDPR brokers get the GDPR template; US brokers get
  CCPA if the profile lives in California, otherwise a generic multi-state one.
- **Sending** — each broker gets an action page: for email brokers, a `mailto:`
  link and copyable text; for form brokers, the opt-out URL plus step-by-step
  instructions and your details to paste. Email-capable brokers can also be sent
  automatically in bulk from the **Send** page (opt-in, see below); form brokers
  always stay manual.
- **Status tracking** — per-broker pipeline (`not started → sent → confirmed /
  rejected / needs verification`) with history, plus a dashboard with counters
  and a "due for follow-up" list. California profiles also get a pointer to the
  state's DROP one-shot deletion platform.
- **IMAP reply monitoring** — point it at a mailbox/label; it correlates broker
  replies (via a `[PIR-<id>]` subject tag), classifies them, and auto-advances
  statuses. Ambiguous mail goes to a **review queue**; it never sends, replies,
  or deletes.
- **Recurrence** — sent requests get a follow-up date (60 days people-search,
  90 otherwise) surfaced on the dashboard when due.
- **Exposure scanning** — a local Playwright browser session checks a
  broker's public search page for your profile and scores any listing
  against it (name, age, location, alias, phone), never on a bare
  name-appears-on-page match. Four outcomes: **found** / **not found** /
  **possible match** (low-confidence, you confirm or dismiss) / **couldn't
  check** (blocked/timeout, falls back to a manual search link). Go to the
  **Scan** page and click **Scan** (single broker) or **Scan all**; each
  scan launches a real browser and is rate-limited, so expect ~10-25s per
  broker, more for a bulk run. Brokers in the same `network` (shared owner or
  database) inherit a derived **likely exposed** verdict when one sibling is found.
  **Coverage is partial.** 19 brokers have a search URL (`scan` key in
  `data/brokers.json`). 10 of them are auto-scanned, but only 3 (USPhonebook,
  Advanced Background Checks, AnyWho) have selectors verified against a live
  page; the rest use unverified selectors or the generic fallback. The other
  8 are manual-link only (JS wizards / bot walls). Every other broker shows up
  under "Not publicly searchable" until someone adds and verifies a `scan`
  config for it (`python -m scripts.verify_scan`).

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium         # one-time, for exposure scanning
.venv/bin/python -m scripts.seed_brokers      # load broker registry
.venv/bin/uvicorn app.main:app --port 8137    # http://127.0.0.1:8137
```

Then open the dashboard, add a **Profile**, and work through the brokers. Open it
via `127.0.0.1` or `localhost`; other hostnames are rejected.

### Optional: inbox monitoring

Copy `config.example.toml` to `config.toml`, set `[imap] enabled = true`, and
fill in host/username/app-password. Use a **dedicated mailbox or Gmail label**
and an **app-specific password**. Send your removal emails from that address so
replies land where the poller can see them, and keep the `[PIR-N]` subject tag
intact. Click **Check inbox** on the dashboard to poll.

### Optional: automatic sending

Set `[smtp] enabled = true` in `config.toml` with the **same mailbox** as
`[imap]` — broker replies must land in the mailbox being polled, so auto-sent
requests use no `Reply-To` and rely on the From address being that mailbox.
On the **Send** page, **Preview** renders and stages every eligible
not-yet-sent email/both broker without sending anything; **Send for real**
opens one SMTP connection and works through the (editable) selection with a
jittered pause between messages. Only brokers still `not_started` and not
verdicted `not_found` are eligible; follow-up re-sends and form-only brokers
always stay manual. A full run of the ~73 email-capable brokers takes several
minutes and is well under Gmail's ~500 recipients/day limit.

### Optional: custom local brokers

To track sites that aren't in the shared registry, create
`data/brokers.local.json` (gitignored) as `{"brokers": [...]}`, with entries in the
same schema as `data/brokers.json`, then re-seed. A local name that duplicates an
existing broker makes the seed fail. Removing an entry later leaves its DB row in
place, same as removing one from `brokers.json`.

### Security notes

IMAP and SMTP verify TLS certificates, so self-signed mail servers won't connect.
The app accepts only `127.0.0.1`/`localhost` as Host and rejects cross-origin
POSTs, which stops other websites from submitting forms to it (CSRF and DNS rebinding).

## Tests

```sh
.venv/bin/python -m pytest
```

Covers template rendering, status transitions / follow-up dates, reply
classification, SMTP sending, routes (including the Host/Origin checks), broker
data and seeding, and the scan pipeline (match scoring, HTML extraction, rate
limiting, outcome persistence) — all against fakes and saved HTML fixtures, no
live network calls.

## Project layout

```
app/       main.py (routes) · config.py · db.py · models.py · templater.py · inbox.py
           sender.py · send_service.py
           scanner.py · fetcher.py · extract.py · matcher.py · ratelimit.py · scan_service.py
           templates/ (HTML) · static/ (style.css, fonts)
data/      brokers.json · brokers.local.json (optional, gitignored)
           request_templates/ (ccpa/gdpr/generic + _identity)
scripts/   seed_brokers.py · verify_scan.py
tests/     fixtures/ (synthetic scan pages)
mockups/   ui-preview.html
```

## Caveats

- Opt-out URLs and privacy emails change often. Verify a broker's current
  process before relying on it; update `data/brokers.json` and re-seed.
- Some brokers require phone/ID verification or re-list data quickly (flagged by
  the `difficulty` field and notes) — those need manual follow-up.
- **Scanning uses the first address line only.** `scanner.search_context()` takes
  the city from it (`"street, city, ST zip"` and `"street, city ST zip"` both
  work). A profile with no parseable city can't scan brokers whose search URL
  needs `{city}`; those show as couldn't-check rather than searching nationwide.
- This is a personal tool, not legal advice.

## v2 roadmap

Automatic email sending shipped (see above). Remaining seams:

1. **Web-form automation.** For `contact_method = form` brokers, drive the
   opt-out flow with Playwright behind a shared `deliver()` call, dispatched by
   `broker.contact_method`. Expect friction from CAPTCHAs and phone/email
   verification — keep the manual fallback.
2. **Scheduled follow-ups.** A background scheduler that re-sends due requests on
   their cadence instead of surfacing them for a manual click.
3. **Broker list expansion.** Grow toward the full ~400+ using the public
   [Big Ass Data Broker Opt-Out List](https://github.com/yaelwrites/Big-Ass-Data-Broker-Opt-Out-List)
   and the [California DROP](https://cppa.ca.gov/data_brokers/) registry.
```
