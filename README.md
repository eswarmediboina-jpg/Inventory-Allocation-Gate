# Facility Switch Gate — Phase 0

A single choke point for moving sale order items from a channel's dummy
facility into the main facility in Uniware. Today it enforces **zero
rules** — it logs every request and passes it straight through. The
point of Phase 0 is to get the whole team using this instead of the
Uniware UI's manual "switch facility" action, so that by the time we
add real rules (Phase 1+), no one has to change how they work — the
gate is already the front door.

## Why this exists

Orders get punched into a channel's dummy facility (no real stock there)
so we get demand visibility without touching real inventory. When the
team is ready to ship, they switch the order to the main facility —
*that's* the exact moment real inventory flips from Available → Blocked.
That switch is the natural place to eventually enforce channel
allocation rules (protecting D2C/B2C from being starved by aggressive
B2B POs). This tool wraps that action.

## One-time setup

### 1. Get Uniware API access
- Ask your Uniware account admin to create an API user and set a
  password for it.
- Use Uniware's Authentication API to exchange that username/password
  for an access token:
  https://documentation.unicommerce.com/docs/oauth.html
- Tokens likely expire — check the "Renew Access Token" API. For Phase
  0 it's fine to refresh manually; Phase 1 can automate this.

### 2. Get facility codes
- Main facility code, plus each channel's dummy facility code.
- Get these from Uniware admin, or via the Search Facility API:
  https://documentation.unicommerce.com/docs/facility-search.html

### 3. Create the BigQuery log table
Run this once against your `zouk-wh` project (same infra as your other
pipelines):

```sql
CREATE TABLE `zouk-wh.MapleMonk.zouk_facility_switch_log` (
  event_timestamp   TIMESTAMP,
  sale_order_code   STRING,
  item_codes        STRING,   -- comma-separated
  source_channel    STRING,
  target_facility   STRING,
  requested_by      STRING,
  rule_check_result STRING,   -- 'ALLOWED_NO_RULES' in Phase 0
  rule_check_reason STRING,
  uniware_success   BOOL,
  uniware_message   STRING
);
```

### 4. Configure and run
```bash
pip install -r requirements.txt
cp .env.example .env
# fill in .env with real values — never commit or paste this file anywhere
python app.py
```
Visit `http://localhost:5000` (or wherever you deploy it).

## Deployment note

This needs to run somewhere persistent and reachable by your ops team —
your WSL2 environment if it stays on, or a small always-on VM/Cloud Run
service. It's not a claude.ai artifact; it's a real backend that holds
API credentials, so it should live in your own infrastructure, same as
your other BigQuery pipelines.

## The critical adoption step

This only works as a "gate" if the team stops using Uniware's UI
directly for this specific action and uses this form instead. Worth
checking whether Uniware permissions can restrict the manual switch
action for the relevant user roles, so this form becomes the *only*
path — otherwise it's easy to bypass without anyone noticing.

## What Phase 1 looks like

All rule logic lives in one function in `app.py`:

```python
def check_rules(channel, item_codes, requested_by):
    return ("ALLOWED_NO_RULES", None)
```

Phase 1 replaces this with a real ATP/floor check per SKU — same
function signature, same form, same team workflow. Nothing else in the
app needs to change.
