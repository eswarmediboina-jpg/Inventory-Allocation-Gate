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

# Channels — hardcoded (used only to label each log entry with the channel).
CHANNELS = [
    "Amazon", "Flipkart", "Myntra", "Distributor_B2B",
    "Blinkit", "Zepto", "Swiggy_Instamart", "D2C_Shopify",
]

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
    which = [filters["facility"]] if filters["facility"] in all_codes else all_codes

    orders = []
    search_error = None
    try:
        for fac in which:
            res = search_sale_orders(
                token,
                facility_code=fac,
                channel=filters["channel"] or None,
                status=filters["status"] or None,
                from_date=_iso_start(filters["from_date"]),
                to_date=_iso_end(filters["to_date"]),
                display_order_code=filters["order_code"] or None,
            )
            for el in res["elements"]:
                el["_facility"] = fac
                orders.append(el)
    except Exception as e:
        search_error = str(e)

    return render_template(
        "index.html",
        username=session.get("username", ""),
        channels=CHANNELS,
        facilities=FACILITIES,
        orders=orders,
        search_error=search_error,
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
