"""FastAPI app: profile, broker directory, per-broker action page, dashboard,
and IMAP-driven review queue. Server-rendered HTML, no front-end framework.
"""
from collections import Counter

from fastapi import FastAPI, Form, Request as HttpRequest
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, inbox, sender, templater
from .config import ROOT, load_config
from .models import (
    STATUS_CONFIRMED,
    STATUS_NEEDS_VERIFICATION,
    STATUS_NOT_STARTED,
    STATUS_REJECTED,
    STATUS_SENT,
    CONTACT_FORM,
)

app = FastAPI(title="Personal Incogni")
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
views = Jinja2Templates(directory=str(ROOT / "app" / "templates"))

STATUS_LABELS = {
    STATUS_NOT_STARTED: "Not started",
    STATUS_SENT: "Sent",
    STATUS_CONFIRMED: "Confirmed",
    STATUS_REJECTED: "Rejected",
    STATUS_NEEDS_VERIFICATION: "Needs verification",
}
views.env.globals["STATUS_LABELS"] = STATUS_LABELS


def get_conn():
    conn = db.connect()
    db.init_db(conn)
    return conn


@app.get("/", response_class=HTMLResponse)
def dashboard(request: HttpRequest):
    conn = get_conn()
    try:
        brokers = db.all_brokers(conn)
        requests = db.all_requests(conn)
        profile = db.get_profile(conn)
        counts = Counter(requests[b.id].status for b in brokers)
        due = db.due_requests(conn)
        due_brokers = [(db.get_broker(conn, r.broker_id), r) for r in due]
        review = db.review_queue(conn)
        rows = sorted(
            ((b, requests[b.id]) for b in brokers),
            key=lambda x: (x[1].status != STATUS_NEEDS_VERIFICATION, x[0].name.lower()),
        )
        cfg = load_config()
        return views.TemplateResponse("dashboard.html", {
            "request": request, "rows": rows, "counts": counts,
            "total": len(brokers), "due_brokers": due_brokers,
            "review_count": len(review), "profile_ready": bool(profile.full_name),
            "imap_enabled": cfg.get("imap", {}).get("enabled", False),
        })
    finally:
        conn.close()


@app.get("/brokers", response_class=HTMLResponse)
def broker_list(request: HttpRequest, category: str = "", status: str = ""):
    conn = get_conn()
    try:
        brokers = db.all_brokers(conn)
        requests = db.all_requests(conn)
        rows = []
        for b in brokers:
            r = requests[b.id]
            if category and b.category != category:
                continue
            if status and r.status != status:
                continue
            rows.append((b, r))
        categories = sorted({b.category for b in brokers})
        return views.TemplateResponse("brokers.html", {
            "request": request, "rows": rows, "categories": categories,
            "sel_category": category, "sel_status": status,
            "statuses": STATUS_LABELS,
        })
    finally:
        conn.close()


@app.get("/broker/{broker_id}", response_class=HTMLResponse)
def broker_detail(request: HttpRequest, broker_id: int):
    conn = get_conn()
    try:
        broker = db.get_broker(conn, broker_id)
        if broker is None:
            return RedirectResponse("/brokers", status_code=303)
        profile = db.get_profile(conn)
        req = db.get_or_create_request(conn, broker_id)
        cfg = load_config()
        prefix = cfg.get("app", {}).get("request_tag_prefix", "PIR")
        rendered = templater.render(broker, profile, req.id, prefix)
        to_addr = broker.opt_out_email or ""
        mailto = sender.mailto_link(to_addr, rendered) if to_addr else ""
        return views.TemplateResponse("broker_detail.html", {
            "request": request, "broker": broker, "req": req,
            "rendered": rendered, "mailto": mailto, "profile": profile,
            "profile_ready": bool(profile.full_name),
            "is_form": broker.contact_method == CONTACT_FORM,
            "statuses": STATUS_LABELS,
        })
    finally:
        conn.close()


@app.post("/broker/{broker_id}/status")
def update_status(broker_id: int, status: str = Form(...), note: str = Form("")):
    conn = get_conn()
    try:
        req = db.get_or_create_request(conn, broker_id)
        if status in STATUS_LABELS:
            db.set_status(conn, req.id, status, note=note)
        return RedirectResponse(f"/broker/{broker_id}", status_code=303)
    finally:
        conn.close()


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: HttpRequest):
    conn = get_conn()
    try:
        profile = db.get_profile(conn)
        return views.TemplateResponse("profile.html", {
            "request": request, "profile": profile,
        })
    finally:
        conn.close()


@app.post("/profile")
def save_profile(
    full_name: str = Form(""), aliases: str = Form(""), emails: str = Form(""),
    phones: str = Form(""), addresses: str = Form(""), date_of_birth: str = Form(""),
):
    conn = get_conn()
    try:
        db.save_profile(conn, {
            "full_name": full_name.strip(), "aliases": aliases.strip(),
            "emails": emails.strip(), "phones": phones.strip(),
            "addresses": addresses.strip(), "date_of_birth": date_of_birth.strip(),
        })
        return RedirectResponse("/", status_code=303)
    finally:
        conn.close()


@app.get("/review", response_class=HTMLResponse)
def review_page(request: HttpRequest):
    conn = get_conn()
    try:
        items = db.review_queue(conn)
        enriched = []
        for m in items:
            broker = None
            if m["request_id"]:
                req = db.get_request(conn, m["request_id"])
                if req:
                    broker = db.get_broker(conn, req.broker_id)
            enriched.append({**m, "broker": broker})
        return views.TemplateResponse("review.html", {
            "request": request, "items": enriched, "statuses": STATUS_LABELS,
        })
    finally:
        conn.close()


@app.post("/review/{message_rowid}/resolve")
def resolve_review(message_rowid: str, broker_id: int = Form(...), status: str = Form(...)):
    """Manually classify a review-queue message and apply status to its broker."""
    conn = get_conn()
    try:
        req = db.get_or_create_request(conn, broker_id)
        if status in STATUS_LABELS:
            db.set_status(conn, req.id, status, note="Manual review resolution")
        conn.execute(
            "UPDATE seen_messages SET needs_review = 0 WHERE message_id = ?",
            (message_rowid,),
        )
        conn.commit()
        return RedirectResponse("/review", status_code=303)
    finally:
        conn.close()


@app.post("/inbox/poll")
def poll_inbox():
    conn = get_conn()
    try:
        cfg = load_config()
        if not cfg.get("imap", {}).get("enabled", False):
            return RedirectResponse("/?imap=disabled", status_code=303)
        icfg = inbox.ImapConfig.from_dict(cfg)
        result = inbox.poll(conn, icfg)
        msg = f"scanned={result.scanned}&advanced={result.advanced}&review={result.review}"
        if result.errors:
            msg += "&error=1"
        return RedirectResponse(f"/?{msg}", status_code=303)
    finally:
        conn.close()
