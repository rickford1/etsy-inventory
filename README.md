# Etsy Inventory & Analytics

A local inventory, cost, and marketing-analytics tool for any Etsy shop. It syncs listings, orders, fees, and Meta ad spend into a local SQLite database, then surfaces profit and ROAS through a CLI and a Streamlit dashboard.

Cost of goods is tracked with a configurable **bill of materials (BOM)**, so it works whether you sell 3D prints, jewelry, stickers, or anything else — define what each item (and each shipment) consumes, and COGS is computed automatically. It ships with a 3D-printing preset, or you can start empty and define your own.

## What it does

- **Inventory** — pulls active listing quantities from Etsy; tracks raw materials and low-stock thresholds.
- **COGS** — deducts each order's materials from stock via a per-listing + per-order bill of materials, and records cost of goods sold.
- **Revenue & profit** — reconciles orders against the Etsy payment ledger (transaction fees, processing fees, offsite ads, refunds, sales tax, shipping labels) for true net profit by period.
- **Marketing & ROAS** — pulls daily Meta ad insights and compares ad-day vs. baseline revenue to compute both *raw* and *lift* (incremental) ROAS.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Purpose |
| --- | --- |
| `ETSY_API_KEY` | Etsy app keystring (OAuth client id) |
| `ETSY_SHARED_SECRET` | Etsy app shared secret |
| `ETSY_REDIRECT_URI` | OAuth callback (default `http://localhost:3003/callback`) |
| `ETSY_SHOP_ID` | Your Etsy shop id |
| `META_ACCESS_TOKEN` | Meta Graph API token (optional — for ad analytics) |
| `META_AD_ACCOUNT_ID` | Meta ad account id, e.g. `act_XXXXXXXXXX` (optional) |
| `SHOP_NAME` | Display name shown in the dashboard sidebar |
| `SEED_PRESET` | Starter materials/BOM to seed on a fresh DB: `3d-printing` (default) or `none` |

Authorize with Etsy (OAuth2 + PKCE). This opens a browser and saves tokens to `tokens.json`:

```bash
python auth.py
```

## Usage

### Sync

Pull the latest listings, orders, ledger/fees, and Meta spend into the local DB:

```bash
python cli.py sync
```

### Dashboard

```bash
streamlit run app.py
```

Three pages: **Overview**, **Revenue & Profit**, and **Marketing & ROAS**. The sidebar has a "Sync now" button.

### CLI commands

```
sync                            Pull latest listings + orders + fees from Etsy and Meta spend
listings                        Show all local listings
low [N]                         Show listings with stock <= N (default 3)
set-qty <listing_id> <qty>      Update quantity on Etsy + local DB

orders [N]                      Show last N orders with per-order profit (default 20)
revenue                         Revenue + profit summary (all time, 30d, 90d)

materials                       Show all materials with stock + cost
add-material <name> <unit> [cost] [stock] [low]   Define a new material
set-stock <material> <amount>   Set material stock to an exact amount
add-stock <material> <amount>   Add to material stock (e.g. restocking a roll)
set-cost <material> <cost>      Set cost per unit for a material

bom                             Show the bill-of-materials that drives COGS
bom-set <target> <material> <qty>   Set a BOM qty (target = default | order | <listing_id>)
bom-rm <target> <material>      Remove a BOM entry

settings                        Show all settings
set-setting <key> <value>       Update a setting (e.g. filament_cost_per_roll 14.00)

marketing [days]                Meta ad spend / clicks / CPC / CTR (default 30 days)
roas [days]                     Meta spend vs Etsy revenue, raw + lift ROAS (default 90)
```

## How it works

- **`auth.py`** — Etsy OAuth2 + PKCE flow; tokens stored in `tokens.json` and auto-refreshed (Etsy access tokens expire hourly).
- **`etsy_client.py`** — thin wrapper over the Etsy Open API v3 (listings, inventory, receipts, per-receipt payments, payment-account ledger).
- **`meta_client.py`** — thin wrapper over the Meta Marketing API (Graph v21) for daily campaign insights.
- **`sync.py`** — orchestrates a full sync: listings → orders (with COGS deduction) → ledger (fee aggregation) → per-receipt processing fees → Meta spend.
- **`inventory.py`** — SQLite schema, migrations, and all queries; the payment ledger is the source of truth for period fee/profit totals.
- **`cli.py`** / **`app.py`** — command-line and Streamlit front ends.

### Cost of goods (the BOM)

COGS is driven by a bill of materials with two scopes:

- **Per-item** — materials consumed for each unit, keyed by listing. A listing with its own BOM uses it; otherwise it falls back to the `default` BOM (`listing_id` 0). Quantities scale with the item quantity in the order.
- **Per-order** — materials consumed once per shipment (packaging, etc.), regardless of how many items are in the order.

For each new order, the sync deducts every material from stock and sums `quantity × cost_per_unit` into that order's COGS.

```bash
python cli.py bom                          # see the current BOM
python cli.py bom-set default beads 10     # every item uses 10 beads
python cli.py bom-set order gift_box 1     # every order uses 1 gift box
python cli.py bom-set 123456 beads 25      # listing 123456 overrides: 25 beads/item
python cli.py bom-rm default beads         # remove an entry
```

The 3D-printing preset seeds a special material reference `@active_filament`, which resolves to whichever filament roll is set as active (`set-setting active_filament filament_2`) at deduction time — handy when you swap rolls.

### A note on ROAS

*Raw ROAS* (ad-day revenue ÷ spend) overstates ad impact, since most sales would have happened anyway. *Lift ROAS* compares average daily revenue on ad days vs. ad-off days and counts only the incremental difference — the more honest number. Lift needs ~14+ ad days to be statistically meaningful.

## Using it for your own shop

The Etsy/Meta sync, fee reconciliation, revenue, and ROAS work for any shop out of the box — just supply your own credentials in `.env`. To set up cost tracking:

1. Set `SEED_PRESET=none` in `.env` so the database starts without the 3D-printing materials.
2. Define your materials: `python cli.py add-material <name> <unit> [cost] [stock] [low]`.
3. Build your BOM with `bom-set` (see above). Until you do, COGS is simply `0` and revenue/fee tracking still works.
4. Set `SHOP_NAME` for the dashboard header.

## Notes

- `inventory.db`, is gitignored — it holds local data and secrets.
- Some fees (offsite ads, listing renewals) can't be attributed to a single order and are averaged across all orders for per-order display; period totals on the dashboard come straight from the ledger.
