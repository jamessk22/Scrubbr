"""FastAPI app: profile, broker directory, per-broker action page, dashboard,
and IMAP-driven review queue. Server-rendered HTML, no front-end framework.
"""
import json
from collections import Counter

from fastapi import BackgroundTasks, FastAPI, Form, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, fetcher, inbox, ratelimit, scan_service, scanner, sender, templater
from .config import ROOT, load_config
from .models import (
    DRIFT_STREAK_THRESHOLD,
    EXPOSURE_ASSUMED,
    EXPOSURE_FOUND,
    EXPOSURE_LIKELY,
    EXPOSURE_NOT_FOUND,
    EXPOSURE_POSSIBLE,
    EXPOSURE_UNKNOWN,
    EXPOSURE_UNREACHABLE,
    STATUS_CONFIRMED,
    STATUS_NEEDS_VERIFICATION,
    STATUS_NOT_STARTED,
    STATUS_REJECTED,
    STATUS_SENT,
    CONTACT_FORM,
    Profile,
    effective_exposure,
    found_networks,
    row_visible,
)

app = FastAPI(title="Scrubbr")
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

EXPOSURE_LABELS = {
    EXPOSURE_UNKNOWN: "Unknown",
    EXPOSURE_FOUND: "Found",
    EXPOSURE_NOT_FOUND: "Not found",
    EXPOSURE_POSSIBLE: "Possible match — review",
    EXPOSURE_UNREACHABLE: "Couldn't check",
    EXPOSURE_ASSUMED: "Assumed exposed",
    EXPOSURE_LIKELY: "Likely exposed",
}
views.env.globals["EXPOSURE_LABELS"] = EXPOSURE_LABELS


def get_conn():
    conn = db.connect()
    db.init_db(conn)
    return conn


def _profile_scope(profile_id: str, profiles: list[Profile]) -> tuple[list[Profile], str]:
    """Resolve a `profile_id` query param ("", "all", or an id) against the
    profile list. Returns (profiles in scope, normalized selector)."""
    if not profiles:
        return [], ""
    if profile_id in ("", "all"):
        return profiles, "all"
    match = [p for p in profiles if str(p.id) == profile_id]
    if match:
        return match, str(match[0].id)
    return profiles, "all"


@app.get("/", response_class=HTMLResponse)
def dashboard(request: HttpRequest, profile_id: str = "", show_all: str = ""):
    conn = get_conn()
    try:
        profiles = db.all_profiles(conn)
        if not profiles:
            return RedirectResponse("/profiles", status_code=303)
        brokers = db.all_brokers(conn)
        scope, selected = _profile_scope(profile_id, profiles)
        exposures = {p.id: db.exposures_for_profile(conn, p.id) for p in scope}
        networks = {p.id: found_networks(brokers, exposures[p.id]) for p in scope}
        all_rows = [
            (b, p, db.get_or_create_request(conn, b.id, p.id),
             effective_exposure(b, exposures[p.id].get(b.id), networks[p.id]))
            for b in brokers for p in scope
        ]
        rows = [row for row in all_rows if row_visible(row[3], row[2].status, bool(show_all))]
        counts = Counter(r.status for _, _, r, _ in rows)
        scope_ids = {p.id for p in scope}
        due = [r for r in db.due_requests(conn) if r.profile_id in scope_ids]
        due_brokers = [(db.get_broker(conn, r.broker_id), db.get_profile(conn, r.profile_id), r) for r in due]
        review = db.review_queue(conn)
        rows.sort(key=lambda x: (x[2].status != STATUS_NEEDS_VERIFICATION, x[0].name.lower(), x[1].name.lower()))
        cfg = load_config()
        return views.TemplateResponse("dashboard.html", {
            "request": request, "rows": rows, "counts": counts,
            "hidden": len(all_rows) - len(rows), "show_all": bool(show_all),
            "total": len(brokers), "due_brokers": due_brokers,
            "review_count": len(review), "profile_ready": all(p.full_name for p in profiles),
            "profiles": profiles, "selected_profile": selected, "multi_profile": len(profiles) > 1,
            "imap_enabled": cfg.get("imap", {}).get("enabled", False),
        })
    finally:
        conn.close()


@app.get("/brokers", response_class=HTMLResponse)
def broker_list(request: HttpRequest, category: str = "", status: str = "",
                exposure: str = "", profile_id: str = "", show_all: str = ""):
    conn = get_conn()
    try:
        profiles = db.all_profiles(conn)
        if not profiles:
            return RedirectResponse("/profiles", status_code=303)
        brokers = db.all_brokers(conn)
        scope, selected = _profile_scope(profile_id, profiles)
        exposures = {p.id: db.exposures_for_profile(conn, p.id) for p in scope}
        networks = {p.id: found_networks(brokers, exposures[p.id]) for p in scope}
        rows = []
        hidden = 0
        for b in brokers:
            if category and b.category != category:
                continue
            for p in scope:
                r = db.get_or_create_request(conn, b.id, p.id)
                if status and r.status != status:
                    continue
                exp = effective_exposure(b, exposures[p.id].get(b.id), networks[p.id])
                if exposure and exp != exposure:
                    continue
                # An explicit exposure filter overrides the default hiding.
                if not exposure and not row_visible(exp, r.status, bool(show_all)):
                    hidden += 1
                    continue
                rows.append((b, p, r, exp))
        categories = sorted({b.category for b in brokers})
        return views.TemplateResponse("brokers.html", {
            "request": request, "rows": rows, "categories": categories,
            "sel_category": category, "sel_status": status, "sel_exposure": exposure,
            "hidden": hidden, "show_all": bool(show_all),
            "statuses": STATUS_LABELS,
            "profiles": profiles, "selected_profile": selected, "multi_profile": len(profiles) > 1,
        })
    finally:
        conn.close()


@app.get("/broker/{broker_id}", response_class=HTMLResponse)
def broker_detail(request: HttpRequest, broker_id: int, profile_id: str = ""):
    conn = get_conn()
    try:
        broker = db.get_broker(conn, broker_id)
        if broker is None:
            return RedirectResponse("/brokers", status_code=303)
        profiles = db.all_profiles(conn)
        if not profiles:
            return RedirectResponse("/profiles", status_code=303)
        scope, selected = _profile_scope(profile_id, profiles)
        cfg = load_config()
        prefix = cfg.get("app", {}).get("request_tag_prefix", "PIR")
        brokers = db.all_brokers(conn)
        entries = []
        for p in scope:
            req = db.get_or_create_request(conn, broker_id, p.id)
            rendered = templater.render(broker, p, req.id, prefix)
            to_addr = broker.opt_out_email or ""
            mailto = sender.mailto_link(to_addr, rendered) if to_addr else ""
            exposures = db.exposures_for_profile(conn, p.id)
            ctx = scanner.search_context(p)
            search_link = (scanner.build_search_url(broker.search_url, ctx) if broker.search_url and ctx else "") or ""
            entries.append({
                "profile": p, "req": req, "rendered": rendered, "mailto": mailto,
                "profile_ready": bool(p.full_name),
                "exposure": effective_exposure(broker, exposures.get(broker_id), found_networks(brokers, exposures)),
                "search_link": search_link,
            })
        return views.TemplateResponse("broker_detail.html", {
            "request": request, "broker": broker, "entries": entries,
            "is_form": broker.contact_method == CONTACT_FORM,
            "statuses": STATUS_LABELS,
            "profiles": profiles, "selected_profile": selected, "multi_profile": len(profiles) > 1,
        })
    finally:
        conn.close()


@app.post("/broker/{broker_id}/status")
def update_status(broker_id: int, profile_id: int = Form(...), status: str = Form(...), note: str = Form("")):
    conn = get_conn()
    try:
        req = db.get_or_create_request(conn, broker_id, profile_id)
        if status in STATUS_LABELS:
            db.set_status(conn, req.id, status, note=note)
        return RedirectResponse(f"/broker/{broker_id}?profile_id={profile_id}", status_code=303)
    finally:
        conn.close()


# profile_id -> {"running": bool, "total": int, "done": int, "results": {broker_id: {...}}}
_bulk_scans: dict[int, dict] = {}


def _run_bulk_scan(profile_id: int, broker_ids: list[int]) -> None:
    conn = get_conn()
    state = _bulk_scans[profile_id]
    try:
        profile = db.get_profile(conn, profile_id)
        for broker_id in broker_ids:
            try:
                broker = db.get_broker(conn, broker_id)
                if broker is None or profile is None:
                    continue
                if scan_service.is_cooled_down_for(conn, broker, profile):
                    state["results"][broker_id] = {"status": EXPOSURE_UNREACHABLE, "detail": "Cooling down after rate limiting"}
                    continue
                outcome = scan_service.scan_and_persist(conn, broker, profile)
                state["results"][broker_id] = {"status": outcome.status, "detail": outcome.detail}
            except Exception as exc:
                # One broker's failure must not abandon the rest of the run,
                # or strand `running: True` forever (which would permanently
                # disable the "Scan all" button).
                state["results"][broker_id] = {"status": EXPOSURE_UNREACHABLE, "detail": str(exc)}
            finally:
                state["done"] += 1
    finally:
        state["running"] = False
        conn.close()


@app.get("/scan", response_class=HTMLResponse)
def scan_page(request: HttpRequest, profile_id: str = ""):
    conn = get_conn()
    try:
        profiles = db.all_profiles(conn)
        if not profiles:
            return RedirectResponse("/profiles", status_code=303)
        match = [p for p in profiles if str(p.id) == profile_id]
        profile = match[0] if match else profiles[0]
        brokers = db.all_brokers(conn)
        exposures = db.exposures_for_profile(conn, profile.id)
        networks = found_networks(brokers, exposures)
        ctx = scanner.search_context(profile)
        rescan_after_days = fetcher.scan_settings()["rescan_after_days"]
        searchable = []
        for b in brokers:
            if not b.search_url:
                continue
            exp = exposures.get(b.id)
            snapshot = json.loads(exp.snapshot) if exp and exp.snapshot else None
            searchable.append({
                "broker": b,
                "status": effective_exposure(b, exp, networks),
                "link": (scanner.build_search_url(b.search_url, ctx) if ctx else "") or "",
                "snapshot": snapshot,
                "reason": snapshot.get("reason") if snapshot and exp and exp.status == EXPOSURE_UNREACHABLE else "",
                "source": exp.source if exp else "",
                "stale": ratelimit.is_stale(exp.checked_at if exp else None, rescan_after_days),
                "manual_only": scan_service.is_skipped(b),
                "likely_via": networks.get(b.network, ""),
                "weak": bool(snapshot) and snapshot.get("page_scope") == scanner.SCOPE_UNSCOPED,
            })
        assumed = [b for b in brokers if not b.search_url]
        drifting = [
            (b, streak, reason) for b, streak, reason in db.drifting_brokers(conn, DRIFT_STREAK_THRESHOLD)
            if not scan_service.is_skipped(b)
        ]
        return views.TemplateResponse("scan.html", {
            "request": request, "profile": profile, "profiles": profiles,
            "multi_profile": len(profiles) > 1,
            "searchable": searchable, "assumed": assumed, "ctx_ready": ctx is not None,
            "bulk": _bulk_scans.get(profile.id), "drifting": drifting,
        })
    finally:
        conn.close()


@app.post("/scan/{broker_id}/auto")
def scan_auto(request: HttpRequest, broker_id: int, profile_id: int = Form(...)):
    conn = get_conn()
    try:
        broker = db.get_broker(conn, broker_id)
        profile = db.get_profile(conn, profile_id)
        if broker is None or profile is None or not scan_service.is_auto_scannable(broker):
            outcome = scan_service.ScanOutcome(EXPOSURE_UNREACHABLE, detail="Broker is not auto-scannable")
        else:
            outcome = scan_service.scan_and_persist(conn, broker, profile)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({
                "outcome": outcome.status, "url": outcome.url, "detail": outcome.detail,
                "snapshot": outcome.snapshot,
            })
        return RedirectResponse(f"/scan?profile_id={profile_id}", status_code=303)
    finally:
        conn.close()


@app.post("/scan/all")
def scan_all(background_tasks: BackgroundTasks, profile_id: int = Form(...)):
    conn = get_conn()
    try:
        profile = db.get_profile(conn, profile_id)
        if profile is None:
            return RedirectResponse("/scan", status_code=303)
        settings = fetcher.scan_settings()
        broker_ids = scan_service.stale_searchable_broker_ids(
            conn, db.all_brokers(conn), profile_id,
            settings["rescan_after_days"], settings["one_per_network"],
        )
        existing = _bulk_scans.get(profile_id)
        if not existing or not existing.get("running"):
            _bulk_scans[profile_id] = {"running": True, "total": len(broker_ids), "done": 0, "results": {}}
            background_tasks.add_task(_run_bulk_scan, profile_id, broker_ids)
        return RedirectResponse(f"/scan?profile_id={profile_id}", status_code=303)
    finally:
        conn.close()


@app.get("/scan/status")
def scan_status(profile_id: int):
    state = _bulk_scans.get(profile_id)
    if state is None:
        return JSONResponse({"running": False, "total": 0, "done": 0, "results": {}})
    return JSONResponse(state)


@app.post("/scan/{broker_id}/mark")
def scan_mark(broker_id: int, profile_id: int = Form(...), status: str = Form(...), back: str = Form("")):
    conn = get_conn()
    try:
        if status in (EXPOSURE_FOUND, EXPOSURE_NOT_FOUND):
            db.set_exposure(conn, broker_id, profile_id, status, "manual")
        return RedirectResponse(back or f"/scan?profile_id={profile_id}", status_code=303)
    finally:
        conn.close()


@app.get("/profiles", response_class=HTMLResponse)
def profiles_page(request: HttpRequest):
    conn = get_conn()
    try:
        profiles = db.all_profiles(conn)
        return views.TemplateResponse("profiles.html", {
            "request": request, "profiles": profiles,
        })
    finally:
        conn.close()


@app.get("/profiles/new", response_class=HTMLResponse)
def new_profile_page(request: HttpRequest):
    return views.TemplateResponse("profile_form.html", {
        "request": request, "profile": Profile(), "is_new": True,
    })


@app.post("/profiles")
def create_profile(
    name: str = Form(...), full_name: str = Form(""), aliases: str = Form(""),
    emails: str = Form(""), phones: str = Form(""), addresses: str = Form(""),
    date_of_birth: str = Form(""), state: str = Form(""),
):
    conn = get_conn()
    try:
        db.create_profile(conn, {
            "name": name.strip() or "Me", "full_name": full_name.strip(),
            "aliases": aliases.strip(), "emails": emails.strip(),
            "phones": phones.strip(), "addresses": addresses.strip(),
            "date_of_birth": date_of_birth.strip(), "state": state.strip(),
        })
        return RedirectResponse("/profiles", status_code=303)
    finally:
        conn.close()


@app.get("/profiles/{profile_id}/edit", response_class=HTMLResponse)
def edit_profile_page(request: HttpRequest, profile_id: int):
    conn = get_conn()
    try:
        profile = db.get_profile(conn, profile_id)
        if profile is None:
            return RedirectResponse("/profiles", status_code=303)
        return views.TemplateResponse("profile_form.html", {
            "request": request, "profile": profile, "is_new": False,
        })
    finally:
        conn.close()


@app.post("/profiles/{profile_id}")
def update_profile(
    profile_id: int, name: str = Form(...), full_name: str = Form(""), aliases: str = Form(""),
    emails: str = Form(""), phones: str = Form(""), addresses: str = Form(""),
    date_of_birth: str = Form(""), state: str = Form(""),
):
    conn = get_conn()
    try:
        db.save_profile(conn, profile_id, {
            "name": name.strip() or "Me", "full_name": full_name.strip(),
            "aliases": aliases.strip(), "emails": emails.strip(),
            "phones": phones.strip(), "addresses": addresses.strip(),
            "date_of_birth": date_of_birth.strip(), "state": state.strip(),
        })
        return RedirectResponse("/profiles", status_code=303)
    finally:
        conn.close()


@app.post("/profiles/{profile_id}/delete")
def delete_profile(profile_id: int):
    conn = get_conn()
    try:
        db.delete_profile(conn, profile_id)
        return RedirectResponse("/profiles", status_code=303)
    finally:
        conn.close()


@app.get("/review", response_class=HTMLResponse)
def review_page(request: HttpRequest):
    conn = get_conn()
    try:
        items = db.review_queue(conn)
        profiles = db.all_profiles(conn)
        enriched = []
        for m in items:
            broker = None
            profile = None
            if m["request_id"]:
                req = db.get_request(conn, m["request_id"])
                if req:
                    broker = db.get_broker(conn, req.broker_id)
                    profile = db.get_profile(conn, req.profile_id)
            enriched.append({**m, "broker": broker, "profile": profile})
        return views.TemplateResponse("review.html", {
            "request": request, "items": enriched, "statuses": STATUS_LABELS,
            "profiles": profiles,
        })
    finally:
        conn.close()


@app.post("/review/{message_rowid}/resolve")
def resolve_review(message_rowid: str, broker_id: int = Form(...), profile_id: int = Form(...), status: str = Form(...)):
    """Manually classify a review-queue message and apply status to its broker."""
    conn = get_conn()
    try:
        req = db.get_or_create_request(conn, broker_id, profile_id)
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
