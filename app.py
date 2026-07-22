"""
Facility Switch Gate — Phase 0

Purpose: become the ONLY way anyone moves a sale order's items from a
channel's dummy facility into the main facility. Today it enforces
NOTHING — it just logs every request and passes it straight through to
Uniware. That's deliberate: get everyone using this instead of the
Uniware UI first, prove it's reliable, THEN add rules inside
check_rules() below without changing how the team works at all.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # fill in real values, never commit this file
    export $(cat .env | xargs)   # or use python-dotenv / your process manager
    python app.py
"""
import os

from flask import Flask, render_template, request

from uniware_client import switch_facility, UniwareConfigError
from bq_logger import log_switch_event

app = Flask(__name__)

# Map each channel to its dummy facility code in Uniware.
# Fill these in via environment variables — get the codes from
# Uniware admin or the Search Facility API.
CHANNEL_DUMMY_FACILITIES = {
    "Amazon": os.environ.get("FACILITY_AMAZON_DUMMY", ""),
    "Flipkart": os.environ.get("FACILITY_FLIPKART_DUMMY", ""),
    "Myntra": os.environ.get("FACILITY_MYNTRA_DUMMY", ""),
    "Distributor_B2B": os.environ.get("FACILITY_DISTRIBUTOR_DUMMY", ""),
    "Blinkit": os.environ.get("FACILITY_BLINKIT_DUMMY", ""),
    "Zepto": os.environ.get("FACILITY_ZEPTO_DUMMY", ""),
    "Swiggy_Instamart": os.environ.get("FACILITY_SWIGGY_DUMMY", ""),
    "D2C_Shopify": os.environ.get("FACILITY_D2C_DUMMY", ""),
}

MAIN_FACILITY_CODE = os.environ.get("UNIWARE_MAIN_FACILITY_CODE", "")


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


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", channels=list(CHANNEL_DUMMY_FACILITIES.keys()))


@app.route("/submit", methods=["POST"])
def submit():
    sale_order_code = request.form.get("sale_order_code", "").strip()
    item_codes_raw = request.form.get("item_codes", "").strip()
    channel = request.form.get("channel", "").strip()
    requested_by = request.form.get("requested_by", "").strip()

    item_codes = [c.strip() for c in item_codes_raw.split(",") if c.strip()]

    rule_result, rule_reason = check_rules(channel, item_codes, requested_by)

    uniware_success = False
    uniware_message = ""

    if rule_result in ("ALLOWED_NO_RULES", "ALLOW"):
        try:
            result = switch_facility(sale_order_code, item_codes, MAIN_FACILITY_CODE)
            uniware_success = bool(result.get("successful", False))
            uniware_message = result.get("message", "") or "OK"
        except UniwareConfigError as e:
            uniware_message = f"Config error: {e}"
        except Exception as e:
            uniware_message = f"Uniware call failed: {e}"
    else:
        uniware_message = f"Blocked: {rule_reason}"

    log_switch_event(
        sale_order_code=sale_order_code,
        item_codes=item_codes,
        source_channel=channel,
        target_facility=MAIN_FACILITY_CODE,
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
        rule_result=rule_result,
        uniware_success=uniware_success,
        uniware_message=uniware_message,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
