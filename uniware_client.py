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
import requests

# Hardcoded Uniware tenant. No .env needed.
UNIWARE_BASE_URL = "https://zouk.unicommerce.com"

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
        raise UniwareAuthError(
            f"Uniware {what} failed (HTTP {resp.status_code}). "
            f"Check your username / password."
        )
    try:
        data = resp.json()
    except ValueError:
        raise UniwareAuthError(f"Uniware {what}: response was not JSON.")
    if not data.get("access_token"):
        raise UniwareAuthError(f"Uniware {what}: no access_token in response.")
    return data


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
