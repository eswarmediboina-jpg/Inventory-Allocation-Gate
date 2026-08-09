"""
Thin wrapper around Uniware's Switch Facility Sale Order Items API.
Docs: https://documentation.unicommerce.com/docs/saleorder-itemswitchfacility.html

This is the ONLY place that should ever call Uniware.

Auth model: each user logs in through the app's UI with their own Uniware
username + password. We exchange those for an access token immediately
(login() below) and hand the token back to the caller, which keeps it in
the user's session. The PASSWORD is never stored here, never logged, and
never written to disk — it exists only for the duration of the login call.

Nothing here reads environment variables. The tenant URL is hardcoded
below; change it here if the tenant ever changes.
"""
import socket

import requests
import urllib3.util.connection as _urllib3_conn

# Force ALL outbound HTTP from this module over IPv4. This machine (esp. on
# a mobile hotspot) prefers IPv6, but Uniware's API IP-whitelist is keyed to
# our public IPv4 address — and the IPv6 "temporary" address rotates every
# few hours anyway. Pinning to IPv4 makes the source IP Uniware sees stable
# and matches the whitelisted address.
_urllib3_conn.allowed_gai_family = lambda: socket.AF_INET

# Hardcoded Uniware tenant. No .env needed.
UNIWARE_BASE_URL = "https://zoukst.unicommerce.com"

# Fixed by Uniware's OAuth docs.
_OAUTH_CLIENT_ID = "my-trusted-client"


class UniwareConfigError(Exception):
    pass


class UniwareAuthError(Exception):
    pass


def login(username: str, password: str) -> dict:
    """
    Exchange a Uniware username + password for tokens (OAuth 'password'
    grant). Returns {access_token, refresh_token, expires_in, ...}.
    Raises UniwareAuthError on bad credentials / any non-200.

    The password is used only for this request and is not retained.
    """
    if not username or not password:
        raise UniwareAuthError("Username and password are required.")
    resp = requests.get(
        f"{UNIWARE_BASE_URL}/oauth/token",
        params={
            "grant_type": "password",
            "client_id": _OAUTH_CLIENT_ID,
            "username": username,
            "password": password,
        },
        timeout=30,
    )
    return _parse_token_response(resp, "login")


def refresh(refresh_token: str) -> dict:
    """Renew an access token using a refresh token (no password needed)."""
    if not refresh_token:
        raise UniwareAuthError("No refresh token available.")
    resp = requests.get(
        f"{UNIWARE_BASE_URL}/oauth/token",
        params={
            "grant_type": "refresh_token",
            "client_id": _OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    return _parse_token_response(resp, "token refresh")


def _parse_token_response(resp: requests.Response, what: str) -> dict:
    if resp.status_code != 200:
        body = (resp.text or "").strip()[:400]
        # Server-side diagnostic (local log only; contains no password).
        print(f"[uniware auth] {what} -> HTTP {resp.status_code}: {body}")
        raise UniwareAuthError(
            f"{what} failed — Uniware returned HTTP {resp.status_code}. {body}"
        )
    try:
        data = resp.json()
    except ValueError:
        print(f"[uniware auth] {what}: non-JSON response: {(resp.text or '')[:400]}")
        raise UniwareAuthError(f"Uniware {what}: response was not JSON.")
    if not data.get("access_token"):
        print(f"[uniware auth] {what}: no access_token; keys={list(data.keys())}")
        raise UniwareAuthError(f"Uniware {what}: no access_token in response.")
    return data


def search_sale_orders(
    access_token: str,
    facility_code: str,
    channel: str = None,
    status: str = None,
    from_date: str = None,
    to_date: str = None,
    display_order_code: str = None,
    display_start: int = 0,
    display_length: int = 50,
) -> dict:
    """
    Search sale orders in one facility via Uniware's Search Sale Order API:
      POST /services/rest/v1/oms/saleOrder/search

    Returns {"elements": [...], "totalRecords": int}. Each element has
    code, displayOrderCode, channel, displayOrderDateTime, status, created.
    Only non-empty filters are sent, so blanks mean "no filter".
    """
    if not access_token:
        raise UniwareAuthError("Not logged in. Please log in again.")

    body = {
        "facilityCodes": [facility_code] if facility_code else None,
        "channel": channel or None,
        "status": status or None,
        "fromDate": from_date or None,
        "toDate": to_date or None,
        "displayOrderCode": display_order_code or None,
        "searchOptions": {
            "displayStart": display_start,
            "displayLength": display_length,
            # getCount over an unfiltered facility makes Uniware scan every
            # order and is the main cause of search timeouts. We don't need
            # the grand total, so skip it — big speedup.
            "getCount": False,
        },
    }
    # Drop keys that are None so we don't over-filter.
    body = {k: v for k, v in body.items() if v is not None}

    url = f"{UNIWARE_BASE_URL}/services/rest/v1/oms/saleOrder/search"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"bearer {access_token}",
    }
    # Most Uniware REST endpoints require the Facility header for context,
    # even where the docs mark it optional. Send the facility being searched.
    if facility_code:
        headers["Facility"] = facility_code
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    if resp.status_code != 200:
        raise UniwareConfigError(
            f"Order search failed (HTTP {resp.status_code}): {(resp.text or '')[:300]}"
        )
    data = resp.json()
    if data.get("successful") is False:
        raise UniwareConfigError(
            f"Uniware search error: {data.get('errors') or data.get('message')}"
        )
    return {
        "elements": data.get("elements") or [],
        "totalRecords": data.get("totalRecords", 0),
    }


def get_sale_order_items(sale_order_code: str, access_token: str, facility_code: str = None) -> list:
    """
    Fetch every sale-order-item CODE for a given order, used to switch a
    WHOLE order. Pass the facility the order currently lives in.

    Uses Uniware's Get Sale Order API:
      POST /services/rest/v1/oms/saleorder/get  body {"code": <order>}
    and reads saleOrderDTO.saleOrderItems[].code.
    """
    if not access_token:
        raise UniwareAuthError("Not logged in. Please log in again.")
    if not sale_order_code:
        raise UniwareConfigError("No sale order code provided.")

    url = f"{UNIWARE_BASE_URL}/services/rest/v1/oms/saleorder/get"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"bearer {access_token}",
    }
    if facility_code:
        headers["Facility"] = facility_code
    resp = requests.post(url, headers=headers, json={"code": sale_order_code}, timeout=30)
    if resp.status_code != 200:
        raise UniwareConfigError(
            f"Could not fetch order {sale_order_code} "
            f"(HTTP {resp.status_code}): {(resp.text or '')[:300]}"
        )
    data = resp.json()
    if data.get("successful") is False:
        raise UniwareConfigError(
            f"Uniware could not return order {sale_order_code}: "
            f"{data.get('errors') or data.get('message')}"
        )
    dto = data.get("saleOrderDTO") or {}
    items = dto.get("saleOrderItems") or []
    codes = [it.get("code") for it in items if it.get("code")]
    if not codes:
        raise UniwareConfigError(
            f"No items found on order {sale_order_code}. Check the order code."
        )
    return codes


def get_sale_order_line_items(sale_order_code: str, access_token: str, facility_code: str = None) -> list:
    """
    Return an order's line items as [{"code","sku","qty"}], used to map live
    inventory beside each order. Pass the facility the order currently lives in.
    """
    if not access_token:
        raise UniwareAuthError("Not logged in. Please log in again.")
    url = f"{UNIWARE_BASE_URL}/services/rest/v1/oms/saleorder/get"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"bearer {access_token}",
    }
    if facility_code:
        headers["Facility"] = facility_code
    resp = requests.post(url, headers=headers, json={"code": sale_order_code}, timeout=30)
    if resp.status_code != 200:
        raise UniwareConfigError(
            f"Could not fetch order {sale_order_code} (HTTP {resp.status_code})"
        )
    dto = (resp.json() or {}).get("saleOrderDTO") or {}
    out = []
    for it in (dto.get("saleOrderItems") or []):
        out.append({
            "code": it.get("code"),
            "sku": it.get("itemSku"),
            "qty": it.get("quantity") or 1,
            "status": it.get("statusCode") or it.get("status") or "",
        })
    return out


def get_inventory_snapshot(access_token: str, skus: list, facility_code: str) -> dict:
    """
    Live inventory for a batch of SKUs in one facility via Uniware's
    Inventory Snapshot API. Returns {sku: {"available": int, "blocked": int}}.
    This is the real-time source of truth (not the 4x/day BigQuery table).
    """
    if not access_token:
        raise UniwareAuthError("Not logged in. Please log in again.")
    skus = [s for s in (skus or []) if s]
    if not skus:
        return {}
    if not facility_code:
        raise UniwareConfigError("No facility code for inventory lookup.")

    url = f"{UNIWARE_BASE_URL}/services/rest/v1/inventory/inventorySnapshot/get"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"bearer {access_token}",
        "Facility": facility_code,
    }
    # Snapshot API accepts up to 10k SKUs per call; we're well under that.
    resp = requests.post(url, headers=headers, json={"itemTypeSKUs": skus}, timeout=60)
    if resp.status_code != 200:
        raise UniwareConfigError(
            f"Inventory snapshot failed (HTTP {resp.status_code}): {(resp.text or '')[:200]}"
        )
    data = resp.json()
    out = {}
    for snap in (data.get("inventorySnapshots") or []):
        sku = snap.get("itemTypeSKU")
        if sku:
            out[sku] = {
                "available": snap.get("inventory"),
                "blocked": snap.get("inventoryBlocked"),
            }
    return out


def switch_facility(
    sale_order_code: str,
    item_codes: list,
    facility_code: str,
    access_token: str,
) -> dict:
    """
    Move the given sale order item(s) into `facility_code`, authenticating
    as the logged-in user (their `access_token` from the session).

    Returns the parsed JSON response from Uniware:
      {"successful": bool, "message": str, "errors": [...], "warnings": [...]}
    """
    if not access_token:
        raise UniwareAuthError("Not logged in. Please log in again.")
    if not facility_code:
        raise UniwareConfigError("No target facility code provided.")

    url = f"{UNIWARE_BASE_URL}/services/rest/v1/oms/saleorder/facility/switch"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"bearer {access_token}",
        # NOTE: Uniware's docs don't clearly state whether this header should
        # be the SOURCE facility (where the order currently is) or the TARGET
        # (where it's going). We send the target. If a switch fails with a
        # facility/permission error, try the source facility here instead.
        "Facility": facility_code,
    }
    payload = {
        "facilityCode": facility_code,
        "saleOrderCode": sale_order_code,
        "saleOrderItemCodes": item_codes,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()
