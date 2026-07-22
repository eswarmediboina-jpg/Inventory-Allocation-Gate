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
    UniwareConfigError,
    UniwareAuthError,
)
from bq_logger import log_switch_event

app = Flask(__name__)
# Random per-process key: no secret is hardcoded or stored. Trade-off: a
# server restart invalidates sessions, so everyone logs in again. Fine for
# an internal tool.
app.secret_key = secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set to True once you serve over HTTPS (recommended for any real deploy).
    SESSION_COOKIE_SECURE=False,
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


@app.route("/", methods=["GET"])
def index():
    if not _current_token():
        return redirect(url_for("login"))
    return render_template(
        "index.html",
        channels=CHANNELS,
        facilities=available_facilities(),
        username=session.get("username", ""),
    )


@app.route("/submit", methods=["POST"])
def submit():
    token = _current_token()
    if not token:
        return redirect(url_for("login"))

    sale_order_code = request.form.get("sale_order_code", "").strip()
    item_codes_raw = request.form.get("item_codes", "").strip()
    channel = request.form.get("channel", "").strip()
    target_facility = request.form.get("target_facility", "").strip()
    # The person is whoever is logged in — not a free-text field.
    requested_by = session.get("username", "")

    item_codes = [c.strip() for c in item_codes_raw.split(",") if c.strip()]

    # Only allow switching to a facility we've explicitly configured.
    facility_by_code = {f["code"]: f for f in available_facilities()}
    chosen = facility_by_code.get(target_facility)
    direction = chosen["direction"] if chosen else ""

    rule_result, rule_reason = check_rules(channel, item_codes, requested_by)

    uniware_success = False
    uniware_message = ""

    if not chosen:
        uniware_message = "Invalid or missing destination facility. Pick one from the list."
    elif rule_result in ("ALLOWED_NO_RULES", "ALLOW"):
        try:
            result = switch_facility(sale_order_code, item_codes, target_facility, token)
            uniware_success = bool(result.get("successful", False))
            uniware_message = result.get("message", "") or "OK"
        except UniwareConfigError as e:
            uniware_message = f"Config error: {e}"
        except UniwareAuthError as e:
            uniware_message = f"Auth problem: {e}"
        except Exception as e:
            uniware_message = f"Uniware call failed: {e}"
    else:
        uniware_message = f"Blocked: {rule_reason}"

    log_switch_event(
        sale_order_code=sale_order_code,
        item_codes=item_codes,
        source_channel=channel,
        target_facility=target_facility,
        requested_by=requested_by,
        rule_check_result=rule_result,
        rule_check_reason=rule_reason,
        uniware_success=uniware_success,
        uniware_message=uniware_message,
    )

    return render_template(
        "result.html",
        sale_order_code=sale_order_code,
        item_codes=item_codes,
        channel=channel,
        target_facility=target_facility,
        direction=direction,
        rule_result=rule_result,
        uniware_success=uniware_success,
        uniware_message=uniware_message,
    )


if __name__ == "__main__":
    # debug=False: never expose the interactive debugger on an app that
    # handles real credentials and triggers real inventory movement.
    app.run(host="0.0.0.0", port=5000, debug=False)
