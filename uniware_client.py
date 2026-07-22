"""
Thin wrapper around Uniware's Switch Facility Sale Order Items API.
Docs: https://documentation.unicommerce.com/docs/saleorder-itemswitchfacility.html

This is the ONLY place that should ever call this Uniware endpoint.
Every facility switch for the business must flow through here so that
(a) it gets logged, and (b) rule checks (added in Phase 1+) have exactly
one choke point to sit in front of.
"""
import os
import requests

UNIWARE_BASE_URL = os.environ.get("UNIWARE_BASE_URL", "").rstrip("/")
UNIWARE_ACCESS_TOKEN = os.environ.get("UNIWARE_ACCESS_TOKEN", "")
MAIN_FACILITY_CODE = os.environ.get("UNIWARE_MAIN_FACILITY_CODE", "")


class UniwareConfigError(Exception):
    pass


def switch_facility(sale_order_code: str, item_codes: list, facility_code: str = None) -> dict:
    """
    Moves the given sale order item(s) into the target facility
    (defaults to the main facility — i.e. dummy -> main).

    Returns the parsed JSON response from Uniware:
      {"successful": bool, "message": str, "errors": [...], "warnings": [...]}
    """
    if not UNIWARE_BASE_URL or not UNIWARE_ACCESS_TOKEN:
        raise UniwareConfigError(
            "UNIWARE_BASE_URL / UNIWARE_ACCESS_TOKEN not set. "
            "Fill these in your local .env — never hardcode them."
        )

    target_facility = facility_code or MAIN_FACILITY_CODE
    if not target_facility:
        raise UniwareConfigError("No target facility code configured.")

    url = f"{UNIWARE_BASE_URL}/services/rest/v1/oms/saleorder/facility/switch"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"bearer {UNIWARE_ACCESS_TOKEN}",
        "Facility": target_facility,
    }
    payload = {
        "facilityCode": target_facility,
        "saleOrderCode": sale_order_code,
        "saleOrderItemCodes": item_codes,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()
