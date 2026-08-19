"""
Facility Switch Gate — Phase 0

Purpose: become the ONLY way anyone moves a sale order's items between the
dummy and main facilities in Uniware. Today it enforces NOTHING — it just
logs every request and passes it straight through to Uniware. That's
deliberate: get everyone using this instead of the Uniware UI first, prove
it's reliable, THEN add rules inside check_rules() without changing how the
team works at all.

Auth: each user logs in with their OWN Uniware username + password on the
login page. We swap those for a token, keep only the TOKEN in their session,
and discard the password. Nothing about Uniware comes from environment
variables — the tenant URL is hardcoded in uniware_client.py, and facilities
are hardcoded in FACILITIES below.

Run locally:
    pip install -r requirements.txt
    python app.py
    # open http://localhost:5000  -> log in with your Uniware credentials

SECURITY: on localhost this is fine over http. If you deploy this anywhere
the team reaches over a network, it MUST be served over HTTPS — otherwise
usernames/passwords travel in plaintext. See the deployment note at bottom.
"""
import os
import io
import csv
import json
import time
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor

# Optional: only used if you point BigQuery at a service-account key via
# GOOGLE_APPLICATION_CREDENTIALS in a local .env. Uniware uses no env at all.
from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask, render_template, request, session, redirect, url_for, Response
)

from uniware_client import (
    login as uniware_login,
    refresh as uniware_refresh,
    switch_facility,
    set_sale_order_priority,
    get_sale_order_items,
    get_sale_order_line_items,
    get_sale_order_full,
    get_inventory_snapshot,
    search_sale_orders,
    UniwareConfigError,
    UniwareAuthError,
)
from bq_logger import log_switch_event

app = Flask(__name__)
# Session signing key. On a shared/multi-worker deployment this MUST be a
# fixed value shared across workers (set FLASK_SECRET_KEY in the environment),
# otherwise logins break as requests hit different workers. Falls back to a
# random per-process key for a quick single-user local run.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # MUST be true in any real (HTTPS) deployment so the session cookie is
    # never sent over plain HTTP. Set SESSION_COOKIE_SECURE=true in the env.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true",
)

# The destination facilities offered in the form. Single source of truth —
# hardcoded on purpose. To add a facility later, add a row here. Only codes
# listed here are accepted from the form, so the gate can never be pointed at
# some arbitrary facility.
#   direction "commit"  = moving stock INTO this facility blocks it   (Available -> Blocked)
#   direction "release" = moving stock INTO this facility frees it up (Blocked -> Available)
FACILITIES = [
    {"code": "zoukst",          "label": "Main (zoukst) — commit inventory (Available → Blocked)",           "direction": "commit"},
    {"code": "saleorderswitch", "label": "Dummy (saleorderswitch) — release inventory (Blocked → Available)", "direction": "release"},
]

# Refresh the token this many seconds before it actually expires.
_EXPIRY_SKEW_SECONDS = 120


# The facility that holds real stock — used for the live inventory lookup.
MAIN_FACILITY = next((f["code"] for f in FACILITIES if f["direction"] == "commit"), "zoukst")

# Short-lived cache of order line items, so reloads don't re-fetch every order
# every time. Items/statuses barely change minute-to-minute. Live INVENTORY is
# never cached (fetched fresh each load).
_ITEMS_TTL = 180
_items_cache = {}
_items_lock = threading.Lock()

# The channel dropdown shows ONLY channels that actually have orders. We
# discover them by scanning recent orders across both facilities, persist the
# result (survives restarts), and refresh it in the background.
_channels_seen = set()
_channels_lock = threading.Lock()
_CHANNELS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.json")
_HARVEST_TTL = 1800
_last_harvest = 0.0


def _load_channels():
    try:
        with open(_CHANNELS_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            with _channels_lock:
                _channels_seen.update(x for x in data if x)
    except Exception:
        pass


def _save_channels():
    try:
        with _channels_lock:
            data = sorted(_channels_seen)
        tmp = _CHANNELS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _CHANNELS_FILE)
    except Exception:
        pass


def _harvest_channels(token, pages=5):
    """Scan recent orders in both facilities and record channels that have
    orders. pages*100 orders per facility."""
    added = False
    for fac in [f["code"] for f in FACILITIES]:
        start = 0
        for _ in range(pages):
            try:
                res = search_sale_orders(token, facility_code=fac,
                                         display_start=start, display_length=100)
            except Exception:
                break
            els = res.get("elements", [])
            with _channels_lock:
                for el in els:
                    ch = el.get("channel")
                    if ch and ch not in _channels_seen:
                        _channels_seen.add(ch)
                        added = True
            if len(els) < 100:
                break
            start += 100
    if added:
        _save_channels()


def _maybe_harvest(token):
    """Seed the channel list on first use (quick) and refresh periodically."""
    global _last_harvest
    now = time.time()
    with _channels_lock:
        empty = not _channels_seen
    if empty:
        _harvest_channels(token, pages=1)  # quick synchronous seed
    if now - _last_harvest >= _HARVEST_TTL:
        _last_harvest = now
        threading.Thread(target=lambda: _harvest_channels(token, pages=2), daemon=True).start()


_load_channels()


# ---------------------------------------------------------------------------
# Access control — per-module roles (admin / editor / viewer)
# ---------------------------------------------------------------------------
MODULES = [
    {"key": "facility", "label": "Switch Facility"},
    {"key": "priority", "label": "Order Priority"},
]
_DEFAULT_ROLE = "viewer"
# Always-admins (bootstrap): can't be locked out even if the file is edited.
BOOTSTRAP_ADMINS = {"eswar.mediboina@zouk.co.in"}

_ACCESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "access.json")
_access = {}  # {username_lower: {"admin": bool, "facility": role, "priority": role}}
_access_lock = threading.Lock()


def _norm_user(u):
    return (u or "").strip().lower()


def _load_access():
    global _access
    try:
        with open(_ACCESS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            _access = {_norm_user(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        pass


def _save_access():
    try:
        with _access_lock:
            data = dict(_access)
        tmp = _ACCESS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _ACCESS_FILE)
    except Exception:
        pass


def get_access(username):
    """Effective access: {'admin': bool, 'roles': {module_key: 'editor'|'viewer'}}."""
    u = _norm_user(username)
    with _access_lock:
        entry = dict(_access.get(u, {}))
    admin = bool(entry.get("admin")) or (u in {_norm_user(a) for a in BOOTSTRAP_ADMINS})
    roles = {}
    for m in MODULES:
        roles[m["key"]] = "editor" if admin else (entry.get(m["key"]) or _DEFAULT_ROLE)
    return {"admin": admin, "roles": roles}


def can_act(username, module_key):
    """True if the user may send actions to Uniware for this module."""
    return get_access(username)["roles"].get(module_key) == "editor"


def is_admin(username):
    return get_access(username)["admin"]


_load_access()

# Cache the fully-built page per (facility + filters) so flipping between tabs
# is instant. ?refresh=1 rebuilds live; a switch clears it (see below).
_PAGE_TTL = 120
_page_cache = {}
_page_lock = threading.Lock()


def _cached_line_items(order_code, token, facility_code):
    now = time.time()
    with _items_lock:
        hit = _items_cache.get(order_code)
        if hit and now - hit[0] < _ITEMS_TTL:
            return hit[1]
    items = get_sale_order_line_items(order_code, token, facility_code=facility_code)
    with _items_lock:
        _items_cache[order_code] = (now, items)
    return items


def available_facilities():
    """Destination facilities the form is allowed to switch orders to."""
    return FACILITIES


def _store_token(data: dict, username: str = None):
    """Save token state into the session. Never stores the password."""
    session["access_token"] = data["access_token"]
    if data.get("refresh_token"):
        session["refresh_token"] = data["refresh_token"]
    expires_in = data.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        session["expires_at"] = time.time() + float(expires_in) - _EXPIRY_SKEW_SECONDS
    else:
        session["expires_at"] = time.time() + 300
    if username:
        session["username"] = username


def _current_token():
    """
    Return a valid access token for the logged-in user, refreshing if it's
    about to expire. Returns None if the user isn't logged in / can't refresh
    (caller should redirect to login).
    """
    if "access_token" not in session:
        return None
    if time.time() >= session.get("expires_at", 0):
        rt = session.get("refresh_token")
        if not rt:
            session.clear()
            return None
        try:
            data = uniware_refresh(rt)
            _store_token(data)
        except UniwareAuthError:
            # Refresh failed (expired/revoked) — force a fresh login.
            session.clear()
            return None
    return session.get("access_token")


def check_rules(channel: str, item_codes: list, requested_by: str):
    """
    PHASE 0: no rules. Every request is allowed.

    Phase 1+ will add logic here, e.g.:
        if would_breach_d2c_floor(channel, item_codes):
            return ("BLOCK", "Would breach D2C protected floor for SKU X")

    Return contract: (result, reason)
        result: "ALLOWED_NO_RULES" (phase 0) | "ALLOW" | "BLOCK"
        reason: None, or a short human-readable string when blocked/flagged
    """
    return ("ALLOWED_NO_RULES", None)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")  # used once, never stored
        try:
            data = uniware_login(username, password)
            _store_token(data, username=username)
            return redirect(url_for("index"))
        except UniwareAuthError as e:
            return render_template("login.html", error=str(e))
        except Exception as e:
            print(f"[login] could not reach Uniware: {type(e).__name__}: {e}")
            return render_template("login.html", error=f"Could not reach Uniware: {e}")
        finally:
            # Drop the password reference as soon as we're done with it.
            password = None
    if _current_token():
        return redirect(url_for("index"))
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _iso_start(d):
    return f"{d}T00:00:00.000Z" if d else None


def _iso_end(d):
    return f"{d}T23:59:59.000Z" if d else None


def _build_orders_view(token, active_facility, is_queue, filters):
    """Search + enrich orders for one tab. Returns a cacheable view dict."""
    orders = []
    search_error = None
    auth_error = False
    if filters["order_code"]:
        # Direct lookup by Uniware sale-order code — finds the exact order in
        # whatever facility it's in (bypasses the 30-cap and display-code match).
        try:
            o = get_sale_order_full(filters["order_code"], token)
            if o:
                o["_facility"] = active_facility  # show it in the current tab's context
                orders = [o]
            else:
                search_error = f"No order found with sale-order code '{filters['order_code']}'."
        except UniwareAuthError as e:
            search_error = str(e); auth_error = True
        except Exception as e:
            search_error = str(e)
    else:
        try:
            res = search_sale_orders(
                token,
                facility_code=active_facility,
                channel=filters["channel"] or None,
                status=filters["status"] or None,
                from_date=_iso_start(filters["from_date"]),
                to_date=_iso_end(filters["to_date"]),
                display_length=30,
            )
            for el in res["elements"]:
                el["_facility"] = active_facility
                orders.append(el)
        except UniwareAuthError as e:
            search_error = str(e); auth_error = True
        except Exception as e:
            search_error = str(e)

    with _channels_lock:
        for o in orders:
            if o.get("channel"):
                _channels_seen.add(o["channel"])

    # NOTE: per-order stock / allocatable / item-status is NOT computed here —
    # it's loaded asynchronously by /enrich after the page renders, so Search
    # and filters return instantly instead of waiting on dozens of API calls.
    return {
        "orders": orders,
        "search_error": search_error,
        "auth_error": auth_error,
    }


def _enrich_orders(token, facility, codes):
    """Heavy per-order work for /enrich: fetch items (cached, parallel), then
    compute allocatable (queue) or item-status counts (main). Returns
    {code: {ordered, allocatable}} or {code: {ordered, statuses}}."""
    is_queue = (facility != MAIN_FACILITY)

    def _load(code):
        try:
            items = _cached_line_items(code, token, facility)
        except Exception:
            items = []
        return [it for it in items if (it.get("facility") or facility) == facility]

    items_by_code = {}
    if codes:
        # Modest concurrency — many parallel calls from one IP get rate-limited.
        with ThreadPoolExecutor(max_workers=5) as ex:
            for code, its in zip(codes, ex.map(_load, codes)):
                items_by_code[code] = its

    out = {}
    if is_queue:
        skus = sorted({it["sku"] for its in items_by_code.values() for it in its if it.get("sku")})
        inv = {}
        if skus:
            try:
                inv = get_inventory_snapshot(token, skus, MAIN_FACILITY)
            except Exception:
                inv = {}
        for code, its in items_by_code.items():
            demand = {}
            for it in its:
                if it.get("sku"):
                    demand[it["sku"]] = demand.get(it["sku"], 0) + 1
            alloc = 0
            for sku, dem in demand.items():
                a = inv.get(sku, {}).get("available") or 0
                try:
                    a = max(int(a), 0)
                except (TypeError, ValueError):
                    a = 0
                alloc += min(dem, a)
            out[code] = {"ordered": len(its), "allocatable": alloc}
    else:
        for code, its in items_by_code.items():
            counts = {}
            for it in its:
                s = it.get("status") or "—"
                counts[s] = counts.get(s, 0) + 1
            out[code] = {"ordered": len(its), "statuses": counts}
    return out


@app.route("/", methods=["GET"])
def index():
    """Browse orders in the configured facilities; pick some to switch."""
    token = _current_token()
    if not token:
        return redirect(url_for("login"))
    _maybe_harvest(token)  # keep the channel list (channels-with-orders) fresh

    filters = {
        "channel": request.args.get("channel", "").strip(),
        "status": request.args.get("status", "").strip(),
        "from_date": request.args.get("from_date", "").strip(),
        "to_date": request.args.get("to_date", "").strip(),
        "order_code": request.args.get("order_code", "").strip(),
        "facility": request.args.get("facility", "").strip(),
    }

    all_codes = [f["code"] for f in FACILITIES]
    # Each tab is ONE facility. Default to the awaiting-allocation queue.
    active_facility = filters["facility"] if filters["facility"] in all_codes else "saleorderswitch"
    filters["facility"] = active_facility
    # Queue tab (dummy) = decide + shift, shows live stock. Main tab (zoukst)
    # = allocated orders, shows per-item status.
    is_queue = (active_facility != MAIN_FACILITY)

    # Serve a recently-built page instantly (fast tab switching). ?refresh=1
    # forces a fresh, live rebuild.
    cache_key = (active_facility, filters["channel"], filters["status"],
                 filters["from_date"], filters["to_date"], filters["order_code"])
    refresh = request.args.get("refresh")
    now = time.time()
    view = None
    if not refresh:
        with _page_lock:
            hit = _page_cache.get(cache_key)
            if hit and now - hit[0] < _PAGE_TTL:
                view = hit[1]
    if view is None:
        view = _build_orders_view(token, active_facility, is_queue, filters)
        # A genuine 401 means the token is dead — clear it and re-login cleanly.
        if view.get("auth_error"):
            session.clear()
            return redirect(url_for("login"))
        # Never cache error pages (avoids serving a stale error / bad-token build).
        if not view["search_error"]:
            with _page_lock:
                _page_cache[cache_key] = (now, view)

    orders = view["orders"]
    search_error = view["search_error"]

    with _channels_lock:
        if filters["channel"]:
            _channels_seen.add(filters["channel"])
        channel_options = sorted(_channels_seen)

    return render_template(
        "index.html",
        username=session.get("username", ""),
        active_module="facility",
        channels=channel_options,
        facilities=FACILITIES,
        orders=orders,
        search_error=search_error,
        main_facility=MAIN_FACILITY,
        tab=active_facility,
        is_queue=is_queue,
        filters=filters,
        can_act=can_act(session.get("username"), "facility"),
        admin=is_admin(session.get("username")),
    )


@app.route("/enrich", methods=["POST"])
def enrich():
    """Background enrichment for the order list (stock/allocatable/status)."""
    token = _current_token()
    if not token:
        return {"error": "auth"}, 401
    data = request.get_json(silent=True) or {}
    facility = data.get("facility") or MAIN_FACILITY
    codes = [c for c in (data.get("codes") or []) if c][:60]
    try:
        results = _enrich_orders(token, facility, codes)
    except Exception as e:
        return {"error": str(e), "results": {}}
    return {"queue": facility != MAIN_FACILITY, "results": results}


@app.route("/switch", methods=["POST"])
def switch_selected():
    """Switch each selected whole order to the chosen destination facility."""
    token = _current_token()
    if not token:
        return redirect(url_for("login"))
    if not can_act(session.get("username"), "facility"):
        return render_template("result.html", target_facility="", results=[{
            "order": "—", "ok": False, "count": 0,
            "message": "You have view-only access to Switch Facility. Ask an admin for Editor access."}])

    target_facility = request.form.get("target_facility", "").strip()
    selected = request.form.getlist("selected")  # each = "orderCode|currentFacility"
    requested_by = session.get("username", "")

    facility_by_code = {f["code"]: f for f in FACILITIES}
    chosen = facility_by_code.get(target_facility)

    results = []

    if not chosen:
        results.append({"order": "—", "ok": False, "message": "Pick a valid destination facility.", "count": 0})
        return render_template("result.html", results=results, target_facility=target_facility)
    if not selected:
        results.append({"order": "—", "ok": False, "message": "No orders were selected.", "count": 0})
        return render_template("result.html", results=results, target_facility=target_facility)

    for entry in selected:
        order_code, _, current_fac = entry.partition("|")
        order_code = order_code.strip()

        # Already at the destination — nothing to do.
        if current_fac == target_facility:
            results.append({
                "order": order_code, "ok": False, "count": 0,
                "message": f"Already in {target_facility}; skipped.",
            })
            continue

        rule_result, rule_reason = check_rules("", [], requested_by)
        ok = False
        msg = ""
        item_codes = []

        if rule_result in ("ALLOWED_NO_RULES", "ALLOW"):
            try:
                item_codes = get_sale_order_items(order_code, token, facility_code=current_fac)
                res = switch_facility(order_code, item_codes, target_facility, token)
                ok = bool(res.get("successful", False))
                msg = res.get("message", "") or ("OK" if ok else "Failed")
                if not ok and res.get("errors"):
                    msg = f"{msg}: {res.get('errors')}"
            except UniwareConfigError as e:
                msg = f"Config error: {e}"
            except UniwareAuthError as e:
                msg = f"Auth problem: {e}"
            except Exception as e:
                msg = f"Uniware call failed: {e}"
        else:
            msg = f"Blocked: {rule_reason}"

        results.append({"order": order_code, "ok": ok, "message": msg, "count": len(item_codes)})

        log_switch_event(
            sale_order_code=order_code,
            item_codes=item_codes,
            source_channel="",
            target_facility=target_facility,
            requested_by=requested_by,
            rule_check_result=rule_result,
            rule_check_reason=rule_reason,
            uniware_success=ok,
            uniware_message=msg,
        )

    # Orders changed facility — drop cached pages so the lists rebuild fresh.
    with _page_lock:
        _page_cache.clear()

    return render_template("result.html", results=results, target_facility=target_facility)


def _order_sku_rows(order, token, source_facility, committing):
    """
    Group an order's line items (only those currently in `source_facility`)
    by SKU. If committing (moving INTO main), cap by live main-facility stock;
    if releasing (moving OUT of main), everything is shiftable.
    """
    by_sku = {}
    for it in order.get("_items", []):
        if it.get("sku") and (it.get("facility") or source_facility) == source_facility:
            by_sku.setdefault(it["sku"], []).append(it["code"])
    inv = {}
    if committing and by_sku:
        try:
            inv = get_inventory_snapshot(token, list(by_sku.keys()), MAIN_FACILITY)
        except Exception:
            inv = {}
    rows = []
    for sku, codes in sorted(by_sku.items()):
        ordered = len(codes)
        if committing:
            avail = inv.get(sku, {}).get("available") or 0
            try:
                avail = max(int(avail), 0)
            except (TypeError, ValueError):
                avail = 0
            allocatable = min(ordered, avail)
        else:
            avail = None            # releasing has no stock gate
            allocatable = ordered
        rows.append({
            "sku": sku,
            "codes": codes,
            "ordered": ordered,
            "available": avail,
            "allocatable": allocatable,
        })
    return rows


def _shift_dirn(source_facility):
    """Given the source facility, return (dest_facility, committing?)."""
    dest = MAIN_FACILITY if source_facility != MAIN_FACILITY else "saleorderswitch"
    return dest, (dest == MAIN_FACILITY)


@app.route("/order/<path:order_code>", methods=["GET"])
def order_detail(order_code):
    """Per-order screen for partial shifting (Route 1: quantity / per-SKU)."""
    token = _current_token()
    if not token:
        return redirect(url_for("login"))

    source_facility = request.args.get("facility", "").strip() or "saleorderswitch"
    dest_facility, committing = _shift_dirn(source_facility)

    error = None
    order = None
    rows = []
    try:
        order = get_sale_order_full(order_code, token)
        if not order:
            error = f"Order {order_code} not found."
    except Exception as e:
        error = str(e)
    if order:
        rows = _order_sku_rows(order, token, source_facility, committing)

    return render_template(
        "order_detail.html",
        username=session.get("username", ""),
        order=order,
        order_code=order_code,
        rows=rows,
        total_ordered=sum(r["ordered"] for r in rows),
        total_allocatable=sum(r["allocatable"] for r in rows),
        source_facility=source_facility,
        dest_facility=dest_facility,
        committing=committing,
        main_facility=MAIN_FACILITY,
        error=error,
        can_act=can_act(session.get("username"), "facility"),
    )


@app.route("/order/<path:order_code>/shift", methods=["POST"])
def order_shift(order_code):
    """Shift a chosen subset of an order's item codes to the main facility."""
    token = _current_token()
    if not token:
        return redirect(url_for("login"))
    if not can_act(session.get("username"), "facility"):
        return render_template("result.html", target_facility="", results=[{
            "order": order_code, "ok": False, "count": 0,
            "message": "You have view-only access to Switch Facility. Ask an admin for Editor access."}])

    source_facility = request.form.get("source_facility", "").strip() or "saleorderswitch"
    dest_facility, committing = _shift_dirn(source_facility)

    try:
        order = get_sale_order_full(order_code, token)
    except Exception:
        order = None
    if not order:
        return render_template("result.html", target_facility=dest_facility,
                               results=[{"order": order_code, "ok": False, "count": 0,
                                         "message": "Could not load the order to shift."}])

    rows = _order_sku_rows(order, token, source_facility, committing)  # live caps

    selected = []
    auto_total = request.form.get("auto_total", "").strip()
    if auto_total.isdigit() and int(auto_total) > 0:
        # Total auto-pick: greedily take allocatable units across SKUs.
        remaining = int(auto_total)
        for r in rows:
            take = min(remaining, r["allocatable"])
            selected += r["codes"][:take]
            remaining -= take
            if remaining <= 0:
                break
    else:
        # Per-SKU quantities.
        for r in rows:
            raw = request.form.get(f"qty_{r['sku']}", "0").strip()
            q = int(raw) if raw.isdigit() else 0
            take = min(q, r["allocatable"])
            selected += r["codes"][:take]

    if not selected:
        return render_template("result.html", target_facility=dest_facility,
                               results=[{"order": order_code, "ok": False, "count": 0,
                                         "message": "Nothing to shift (0 units, or no allocatable stock)."}])

    ok = False
    msg = ""
    try:
        res = switch_facility(order_code, selected, dest_facility, token)
        ok = bool(res.get("successful", False))
        msg = res.get("message", "") or ("OK" if ok else "Failed")
        if not ok and res.get("errors"):
            msg = f"{msg}: {res.get('errors')}"
    except Exception as e:
        msg = str(e)

    log_switch_event(
        sale_order_code=order_code, item_codes=selected, source_channel="",
        target_facility=dest_facility, requested_by=session.get("username", ""),
        rule_check_result="ALLOWED_NO_RULES", rule_check_reason=None,
        uniware_success=ok, uniware_message=msg,
    )
    with _page_lock:
        _page_cache.clear()

    return render_template("result.html", target_facility=dest_facility,
                           results=[{"order": order_code, "ok": ok, "count": len(selected), "message": msg}])


# ---------------------------------------------------------------------------
# Module: Order Priority — bulk-set priorities from a CSV
# ---------------------------------------------------------------------------
def _parse_priority_csv(raw_text):
    """Parse an uploaded CSV into [{code, priority(int|None), raw_priority}]."""
    reader = csv.DictReader(io.StringIO(raw_text))
    rows = []
    for r in reader:
        rr = {(k or "").strip().lower().replace(" ", "_"): (v or "").strip() for k, v in r.items()}
        code = (rr.get("sale_order_code") or rr.get("saleordercode")
                or rr.get("order_code") or rr.get("code"))
        praw = (rr.get("priority") or rr.get("priority_status") or rr.get("priority_number"))
        if not code:
            continue
        try:
            pri = int(float(praw))
        except (TypeError, ValueError):
            pri = None
        rows.append({"code": code, "priority": pri, "raw_priority": praw or ""})
    return rows


def _apply_priorities(rows, token, facility):
    def _do(r):
        if r["priority"] is None:
            return {"order": r["code"], "priority": r["raw_priority"], "ok": False,
                    "message": "Invalid priority — must be a whole number (e.g. 1, 2)."}
        try:
            res = set_sale_order_priority(r["code"], r["priority"], token, facility)
            ok = res["successful"]
            msg = res["message"] or ("OK" if ok else "Failed")
            if not ok and res.get("errors"):
                msg = f"{msg}: {res['errors']}"
        except Exception as e:
            ok = False
            msg = str(e)
        return {"order": r["code"], "priority": r["priority"], "ok": ok, "message": msg}

    with ThreadPoolExecutor(max_workers=5) as ex:
        return list(ex.map(_do, rows))


@app.route("/priority", methods=["GET", "POST"])
def priority():
    token = _current_token()
    if not token:
        return redirect(url_for("login"))

    facility = (request.form.get("facility") or request.args.get("facility") or MAIN_FACILITY)
    may_act = can_act(session.get("username"), "priority")
    results = None
    error = None
    if request.method == "POST":
        if not may_act:
            error = "You have view-only access to Order Priority. Ask an admin for Editor access."
        else:
            f = request.files.get("csv_file")
            if not f or not f.filename:
                error = "Please choose a CSV file to upload."
            else:
                try:
                    raw = f.read().decode("utf-8-sig", errors="replace")
                    rows = _parse_priority_csv(raw)
                except Exception as e:
                    rows, error = None, f"Could not read the CSV: {e}"
                if rows is not None:
                    if not rows:
                        error = "No valid rows found. The file needs columns: sale_order_code, priority."
                    else:
                        results = _apply_priorities(rows, token, facility)

    return render_template(
        "priority.html",
        username=session.get("username", ""),
        active_module="priority",
        facilities=FACILITIES,
        facility=facility,
        results=results,
        error=error,
        can_act=may_act,
        admin=is_admin(session.get("username")),
    )


@app.route("/debug/order/<path:code>")
def debug_order(code):
    """Temporary diagnostic: show an order's items and which facility each is in."""
    token = _current_token()
    if not token:
        return redirect(url_for("login"))
    try:
        o = get_sale_order_full(code, token)
    except Exception as e:
        return {"error": str(e)}
    if not o:
        return {"error": "Order not found (Uniware get returned nothing for this code)."}
    items = o.get("_items", [])
    return {
        "code": o.get("code"),
        "channel": o.get("channel"),
        "status": o.get("status"),
        "item_count": len(items),
        "facilities_present": sorted({(it.get("facility") or "(blank)") for it in items}),
        "items_sample": [
            {"sku": it.get("sku"), "facility": it.get("facility"), "status": it.get("status")}
            for it in items[:20]
        ],
    }


@app.route("/priority/template")
def priority_template():
    csv_text = "sale_order_code,priority\nSO-EXAMPLE-1,1\nSO-EXAMPLE-2,2\n"
    return Response(csv_text, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=orderly_priority_template.csv"})


@app.route("/admin/access", methods=["GET", "POST"])
def admin_access():
    """Admin-only: manage per-module roles for users."""
    if not _current_token():
        return redirect(url_for("login"))
    me = session.get("username")
    if not is_admin(me):
        return render_template("admin.html", username=me, active_module="admin",
                               admin=False, forbidden=True, modules=MODULES, users=[], msg=None)

    msg = None
    if request.method == "POST":
        op = request.form.get("op")
        target = _norm_user(request.form.get("target_user"))
        if not target:
            msg = "Enter a user's login email."
        elif op == "remove":
            if target in {_norm_user(a) for a in BOOTSTRAP_ADMINS}:
                msg = f"{target} is a permanent admin and can't be removed."
            else:
                with _access_lock:
                    _access.pop(target, None)
                _save_access()
                msg = f"Removed {target} (back to Viewer)."
        else:  # save / update
            entry = {"admin": bool(request.form.get("is_admin"))}
            for m in MODULES:
                entry[m["key"]] = "editor" if request.form.get("role_" + m["key"]) == "editor" else "viewer"
            with _access_lock:
                _access[target] = entry
            _save_access()
            msg = f"Saved access for {target}."

    with _access_lock:
        stored = sorted(_access.keys())
    users = []
    seen = set()
    for uname in stored:
        acc = get_access(uname)
        users.append({"user": uname, "admin": acc["admin"], "roles": acc["roles"], "bootstrap": False})
        seen.add(uname)
    for b in BOOTSTRAP_ADMINS:
        bn = _norm_user(b)
        if bn not in seen:
            users.append({"user": bn, "admin": True, "roles": get_access(bn)["roles"], "bootstrap": True})

    return render_template("admin.html", username=me, active_module="admin",
                           admin=True, forbidden=False, modules=MODULES, users=users, msg=msg)


if __name__ == "__main__":
    # debug=False: never expose the interactive debugger on an app that
    # handles real credentials and triggers real inventory movement.
    app.run(host="0.0.0.0", port=5000, debug=False)
