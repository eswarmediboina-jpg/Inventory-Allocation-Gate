"""
Logs every facility-switch request to BigQuery, regardless of outcome.
This log IS the "automate what's currently happening" deliverable —
it's the first real, structured record of channel-wise real allocation
events, independent of any rule logic layered on top later.
"""
import os
import datetime

from google.cloud import bigquery

BQ_PROJECT = os.environ.get("BQ_PROJECT", "zouk-wh")
BQ_DATASET = os.environ.get("BQ_DATASET", "MapleMonk")
BQ_TABLE = os.environ.get("BQ_TABLE", "zouk_facility_switch_log")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = bigquery.Client(project=BQ_PROJECT)
    return _client


def log_switch_event(
    sale_order_code: str,
    item_codes: list,
    source_channel: str,
    target_facility: str,
    requested_by: str,
    rule_check_result: str,
    rule_check_reason: str,
    uniware_success: bool,
    uniware_message: str,
):
    table_id = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"
    row = {
        "event_timestamp": datetime.datetime.utcnow().isoformat(),
        "sale_order_code": sale_order_code,
        "item_codes": ",".join(item_codes),
        "source_channel": source_channel,
        "target_facility": target_facility,
        "requested_by": requested_by,
        "rule_check_result": rule_check_result,   # e.g. 'ALLOWED_NO_RULES' in Phase 0
        "rule_check_reason": rule_check_reason or "",
        "uniware_success": uniware_success,
        "uniware_message": uniware_message or "",
    }
    try:
        client = _get_client()
        errors = client.insert_rows_json(table_id, [row])
        if errors:
            print(f"[bq_logger] insert errors: {errors}")
    except Exception as e:
        # Never let a logging failure block the actual request from
        # being visible to the user — but make sure it's not silent.
        print(f"[bq_logger] failed to log event: {e}")
