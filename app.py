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
import time
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor

# Optional: only used if you point BigQuery at a service-account key via
# GOOGLE_APPLICATION_CREDENTIALS in a local .env. Uniware uses no env at all.
from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask, render_template, request, session, redirect, url_for
)

from uniware_client import (
    login as uniware_login,
    refresh as uniware_refresh,
    switch_facility,
    get_sale_order_items,
    get_sale_order_line_items,
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

# Real channel codes accumulate here from actual order data (there's no
# "list channels" API), so the filter dropdown always matches Uniware's codes.
_channels_seen = set()
_channels_lock = threading.Lock()


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


@app.route("/", methods=["GET"])
def index():
    """Browse orders in the configured facilities; pick some to switch."""
    token = _current_token()
    if not token:
        return redirect(url_for("login"))

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

    orders = []
    search_error = None
    try:
        res = search_sale_orders(
            token,
            facility_code=active_facility,
            channel=filters["channel"] or None,
            status=filters["status"] or None,
            from_date=_iso_start(filters["from_date"]),
            to_date=_iso_end(filters["to_date"]),
            display_order_code=filters["order_code"] or None,
            display_length=30,
        )
        for el in res["elements"]:
            el["_facility"] = active_facility
            orders.append(el)
    except Exception as e:
        search_error = str(e)

    # Enrich each shown order with its line items + LIVE inventory from the
    # main facility (real stock pool). Item fetches run in parallel; then a
    # single bulk inventory-snapshot call covers all SKUs.
    inv_error = None
    status_cols = []
    if orders:
        def _load(o):
            try:
                return _cached_line_items(o["code"], token, o.get("_facility"))
            except Exception:
                return []
        try:
            with ThreadPoolExecutor(max_workers=24) as ex:
                per_order_items = list(ex.map(_load, orders))
        except Exception:
            per_order_items = [[] for _ in orders]
        for o, items in zip(orders, per_order_items):
            o["_items"] = items
            # Units ordered = count of sale-order-item codes.
            o["_ordered"] = len(items)

        if is_queue:
            # Queue tab: how many of each order's units can be allocated from
            # live main-facility stock right now.
            skus = sorted({it["sku"] for o in orders for it in o["_items"] if it.get("sku")})
            inv = {}
            if skus:
                try:
                    inv = get_inventory_snapshot(token, skus, MAIN_FACILITY)
                except Exception as e:
                    inv_error = str(e)
            for o in orders:
                demand = {}
                for it in o["_items"]:
                    sku = it.get("sku")
                    if sku:
                        demand[sku] = demand.get(sku, 0) + 1
                allocatable = 0
                for sku, dem in demand.items():
                    avail = inv.get(sku, {}).get("available") or 0
                    try:
                        avail = int(avail)
                    except (TypeError, ValueError):
                        avail = 0
                    allocatable += min(dem, max(avail, 0))
                o["_allocatable"] = allocatable
        else:
            # Main tab: pivot item statuses into one column each, counting
            # units (item codes) per status per order.
            seen = set()
            for o in orders:
                counts = {}
                for it in o["_items"]:
                    s = it.get("status") or "—"
                    counts[s] = counts.get(s, 0) + 1
                    seen.add(s)
                o["_status_summary"] = counts
            status_cols = sorted(seen)

    # Remember the real channel codes we've seen so the dropdown matches data.
    with _channels_lock:
        for o in orders:
            if o.get("channel"):
                _channels_seen.add(o["channel"])
        if filters["channel"]:
            _channels_seen.add(filters["channel"])
        channel_options = sorted(_channels_seen)

    return render_template(
        "index.html",
        username=session.get("username", ""),
        channels=channel_options,
        facilities=FACILITIES,
        orders=orders,
        search_error=search_error,
        inv_error=inv_error,
        main_facility=MAIN_FACILITY,
        tab=active_facility,
        is_queue=is_queue,
        status_cols=status_cols,
        filters=filters,
    )


@app.route("/switch", methods=["POST"])
def switch_selected():
    """Switch each selected whole order to the chosen destination facility."""
    token = _current_token()
    if not token:
        return redirect(url_for("login"))

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

    return render_template("result.html", results=results, target_facility=target_facility)


if __name__ == "__main__":
    # debug=False: never expose the interactive debugger on an app that
    # handles real credentials and triggers real inventory movement.
    app.run(host="0.0.0.0", port=5000, debug=False)
