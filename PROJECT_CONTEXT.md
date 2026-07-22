# Project Context: Facility Switch Gate

Give this file to Claude Code along with the rest of this folder so it
has full background before touching any code.

## The business problem

Inventory is scarce; demand exceeds supply. B2B channels (and
marketplace sell-in POs — Amazon/Flipkart/Myntra) push aggressively and
drain shared inventory before D2C/B2C ever gets a look, because there's
no deliberate allocation rule — whoever moves fastest wins by default.

## How the warehouse actually works (Uniware/Unicommerce)

- Orders get punched into a **channel-specific dummy facility** with no
  real stock. This gives visibility into demand ("how much production
  gap do we have") without touching real inventory.
- Real stock lives in the **main facility**.
- When the team is ready to fulfill, they **switch the order from the
  dummy facility to the main facility**. That switch is the exact
  moment Uniware moves real stock from `Available` → `Blocked`
  quantity — i.e. the moment inventory is actually committed to a
  channel.
- This switch is a real Uniware REST API:
  `POST /services/rest/v1/oms/saleorder/facility/switch`
  (docs: https://documentation.unicommerce.com/docs/saleorder-itemswitchfacility.html)
  It only works while the shipment is in "Created" state.

## The core design decision

The switch-facility action IS the natural gate — it's the single choke
point where "demand visibility" (dummy facility) turns into "real
inventory commitment" (main facility). Build a wrapper that becomes the
*only* way anyone performs this switch, so we have one place to log
everything and, later, enforce rules — without ever changing how the
team's day-to-day workflow feels.

## Locked decisions (already made, don't re-litigate these)

1. **Channel classification must be configurable**, not hardcoded —
   tagging a channel as B2B vs Protected (D2C/B2C) should be editable
   anytime without a code change.
2. **Start with a fixed days-of-cover floor** (not dynamic/DRR-based)
   for the protected reserve. Evolve to dynamic later.
3. **Alert-first, not hard-block.** The gate should flag/log rule
   breaches, not silently reject them, until the logic has been
   validated against real behavior.
4. **Build order: walking skeleton first.** Phase 0 = automate what's
   currently happening (log every switch, enforce nothing). Only after
   that's live and trusted do we add the floor/ATP logic, then the
   flagging layer. Don't front-load all the logic before anything ships.

## Current build status (Phase 0 — done, untested against live API)

- `app.py` — Flask app, serves the form, calls `check_rules()`
  (currently always allows), calls Uniware's switch API, logs the
  outcome.
- `uniware_client.py` — the only module that talks to Uniware.
- `bq_logger.py` — logs every request/outcome to BigQuery
  (`zouk-wh.MapleMonk.zouk_facility_switch_log`).
- `templates/` — the form the ops team uses instead of Uniware's UI.
- Not yet done: real Uniware credentials, real facility codes, the
  BigQuery table hasn't been created yet, nothing has been run against
  the live API.

## What's next (in order)

1. Get Uniware API user + access token.
2. Get dummy facility codes per channel + main facility code.
3. Create the BigQuery log table (DDL in README.md).
4. Run locally, test one real switch end-to-end.
5. Decide deployment (see "making this usable by the team" — likely
   GCP Cloud Run, same project as BigQuery).
6. Add authentication before anyone besides me uses it — this triggers
   real inventory movement.
7. Only after Phase 0 is stable for a while: add the fixed-floor check
   inside `check_rules()` in `app.py`. Don't touch anything else.

## Broader business context (for judgment calls Claude Code might need)

Swarda is Demand Coordinator at Zouk (Sea Turtle Pvt Ltd), a D2C
fashion accessories brand (~350 people, under MapleMonk). Channels:
Amazon (via Cocoblu), Flipkart, Myntra, Blinkit, Zepto, Swiggy/Instamart,
offline EBO, D2C/Shopify. Core data infra is BigQuery (`zouk-wh`
project, `MapleMonk` dataset), with `ZOUK_INVENTORY_FACT_ITEMS` as the
live inventory fact table. Also mid-build on a festive-season (Aug–Nov
2026) demand forecast validation system — this gate is a separate but
related workstream (protecting real-time allocation, not forecasting).
