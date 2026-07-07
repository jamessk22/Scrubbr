# Personal Incogni

A local, self-hosted data-broker removal assistant — a subscription-free
reimplementation of [Incogni](https://incogni.com/)'s core workflow that keeps
your personal data on your own machine.

Incogni is a paid service that acts as your *authorized agent* to get your data
deleted from ~400+ data brokers under CCPA/GDPR. This tool does the same job
locally. Because **you** are the data subject (not a third-party agent), your
requests carry full legal weight with no authorization paperwork, and your PII
never leaves your computer.

## What it does (v1)

- **Broker directory** — 65 curated high-priority US brokers (people-search,
  marketing, risk, recruitment) with opt-out URLs/emails, contact method, legal
  basis, and difficulty. Seeded from `data/brokers.json`; easy to extend.
- **Local profile** — your identifiers (name, aliases, emails, phones,
  addresses, DOB), stored in SQLite, used only to fill request templates.
- **Request generation** — per-broker legal deletion + opt-out requests from
  Jinja2 templates, chosen by jurisdiction (CCPA / GDPR / generic multi-state).
- **Guided sending (manual)** — each broker gets an action page: for email
  brokers, a `mailto:` link and copyable text; for form brokers, the opt-out URL
  plus step-by-step instructions and your details to paste. *You* do the actual
  sending in v1.
- **Status tracking** — per-broker pipeline (`not started → sent → confirmed /
  rejected / needs verification`) with history, plus a dashboard with counters
  and a "due for follow-up" list.
- **IMAP reply monitoring** — point it at a mailbox/label; it correlates broker
  replies (via a `[PIR-<id>]` subject tag), classifies them, and auto-advances
  statuses. Ambiguous mail goes to a **review queue**; it never sends, replies,
  or deletes.
- **Recurrence** — sent requests get a follow-up date (60 days people-search,
  90 otherwise) surfaced on the dashboard when due.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m scripts.seed_brokers      # load broker registry
.venv/bin/uvicorn app.main:app --port 8137    # http://127.0.0.1:8137
```

Then open the dashboard, fill in your **Profile**, and work through the brokers.

### Optional: inbox monitoring

Copy `config.example.toml` to `config.toml`, set `[imap] enabled = true`, and
fill in host/username/app-password. Use a **dedicated mailbox or Gmail label**
and an **app-specific password**. Send your removal emails from that address so
replies land where the poller can see them, and keep the `[PIR-N]` subject tag
intact. Click **Check inbox** on the dashboard to poll.

## Tests

```sh
.venv/bin/python -m pytest
```

Covers template rendering, status transitions / follow-up dates, and reply
classification.

## Project layout

```
app/       main.py (routes) · db.py · models.py · templater.py · inbox.py · sender.py
           templates/ (HTML) · static/style.css
data/      brokers.json · request_templates/ (ccpa/gdpr/generic + _identity)
scripts/   seed_brokers.py
tests/
```

## Caveats

- Opt-out URLs and privacy emails change often. Verify a broker's current
  process before relying on it; update `data/brokers.json` and re-seed.
- Some brokers require phone/ID verification or re-list data quickly (flagged by
  the `difficulty` field and notes) — those need manual follow-up.
- This is a personal tool, not legal advice.

## v2 roadmap

The v1 design deliberately leaves seams for automation:

1. **Automatic email sending (SMTP).** `app/sender.py` isolates delivery. Add a
   `SmtpSender` that sends the rendered request via `smtplib` and returns a
   receipt; routes call `sender.deliver(...)` instead of rendering a `mailto:`
   link, and mark the request `sent` automatically. Requires the same
   dedicated-mailbox + app-password setup already used for IMAP.
2. **Web-form automation.** For `contact_method = form` brokers, drive the
   opt-out flow with Playwright behind the same `deliver()` call, dispatched by
   `broker.contact_method`. Expect friction from CAPTCHAs and phone/email
   verification — keep the manual fallback.
3. **Scheduled follow-ups.** A background scheduler that re-sends due requests on
   their cadence instead of surfacing them for a manual click.
4. **Broker list expansion.** Grow toward the full ~400+ using the public
   [Big Ass Data Broker Opt-Out List](https://github.com/yaelwrites/Big-Ass-Data-Broker-Opt-Out-List)
   and the [California DROP](https://cppa.ca.gov/data_brokers/) registry.
```
