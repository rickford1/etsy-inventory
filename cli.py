"""
Simple CLI for the Etsy inventory system.
Usage: python cli.py <command>

Commands:
  sync                              Pull latest listings + orders from Etsy
  listings                          Show all local listings
  low [N]                           Show listings with stock <= N (default 3)
  set-qty <listing_id> <qty>        Update quantity on Etsy + local DB

  orders [N]                        Show last N orders (default 20)
  revenue                           Show revenue + profit summary (all time, 30d, 90d)

  materials                         Show all materials with stock + cost
  set-stock <material> <amount>     Set material stock to exact amount
  add-stock <material> <amount>     Add to material stock (e.g. restocking a roll)
  set-cost <material> <cost>        Set cost per unit for a material

  settings                          Show all settings
  set-setting <key> <value>         Update a setting (e.g. filament_cost_per_roll 14.00)
"""

import sys

from etsy_client import EtsyClient
from inventory import (
    get_all_listings, get_low_stock, init_db, upsert_listing,
    get_all_materials, get_material, set_material_stock, set_material_cost, add_material_stock,
    get_all_settings, get_setting, set_setting,
    get_orders, get_revenue_summary, get_orders_with_fees,
)
from sync import sync_listings, sync_orders, sync_ledger, sync_payments


def cmd_sync():
    init_db()
    client = EtsyClient()
    sync_listings(client)
    sync_orders(client)
    sync_ledger(client)
    sync_payments(client)


def cmd_listings():
    init_db()
    listings = get_all_listings()
    if not listings:
        print("No listings in local DB. Run: python cli.py sync")
        return
    print(f"{'ID':<12} {'Qty':>4}  {'Price':>7}  Title")
    print("-" * 60)
    for l in listings:
        print(f"{l['listing_id']:<12} {l['quantity']:>4}  ${l['price']:>6.2f}  {l['title'][:40]}")


def cmd_low(threshold: int = 3):
    init_db()
    items = get_low_stock(threshold)
    if not items:
        print(f"No items with stock <= {threshold}")
        return
    print(f"Low stock (threshold={threshold}):")
    for item in items:
        print(f"  [{item['quantity']}] {item['listing_id']}  {item['title'][:50]}")


def cmd_set_qty(listing_id: int, quantity: int):
    init_db()
    client = EtsyClient()
    print(f"Setting listing {listing_id} quantity to {quantity}...")
    client.set_quantity(listing_id, quantity)
    # update local DB too
    inv = client.get_inventory(listing_id)
    products = inv.get("products", [])
    sku = products[0].get("sku", "") if products else ""
    listings = client.get_listings()
    title = next((l["title"] for l in listings if l["listing_id"] == listing_id), str(listing_id))
    upsert_listing(listing_id, title, sku, quantity, 0.0)
    print("Done.")


def cmd_orders(limit: int = 20):
    init_db()
    orders = get_orders_with_fees(limit)
    if not orders:
        print("No orders yet. Run: python cli.py sync")
        return
    header = f"{'Receipt':<12} {'Date':<12} {'Shipped':<10} {'Total':>7}  {'COGS':>6}  {'Ship':>6}  {'Fees':>6}  {'Profit':>7}  Item"
    print(header)
    print("-" * len(header))
    for o in orders:
        date = o["created_at"][:10] if o["created_at"] else "?"
        total = o["total_price"] or 0
        cogs  = o["cogs"] or 0
        ship  = o["shipping_cost"] or 0
        fees  = ((o["transaction_fee"] or 0) + (o["processing_fee"] or 0)
                 + (o["offsite_ads_fee"] or 0) + (o["listing_renewal_fee"] or 0)
                 + (o["sales_tax"] or 0) + (o["other_fees"] or 0))
        refund = o["refund_amount"] or 0
        profit = total - cogs - ship - fees - refund
        items = (o["items"] or "")[:40]
        print(f"{o['receipt_id']:<12} {date:<12} {o['status']:<10} ${total:>6.2f}  ${cogs:>5.2f}  ${ship:>5.2f}  ${fees:>5.2f}  ${profit:>6.2f}  {items}")


def cmd_revenue():
    init_db()
    summary = get_revenue_summary()

    def print_period(label, d):
        if not d or not d.get("order_count"):
            print(f"  {label}: no orders")
            return
        rev   = d["revenue"]
        cogs  = d["cogs"]
        fees  = d["fees"]
        ship  = d["shipping"]
        refs  = d["refunds"]
        net   = d["net_profit"]
        margin = (net / rev * 100) if rev else 0
        print(f"  {label}: {d['order_count']} orders")
        print(f"    revenue   ${rev:>9.2f}")
        print(f"    fees     -${fees:>9.2f}")
        print(f"    shipping -${ship:>9.2f}")
        print(f"    refunds  -${refs:>9.2f}")
        print(f"    COGS     -${cogs:>9.2f}")
        print(f"    profit    ${net:>9.2f}  ({margin:.0f}% margin)")
        print()

    print("Revenue summary:")
    print_period("Last 30d", summary.get("last_30", {}))
    print_period("Last 90d", summary.get("last_90", {}))
    print_period("All time", summary.get("all_time", {}))


def cmd_materials():
    init_db()
    mats = get_all_materials()
    print(f"{'Material':<20} {'Stock':>10}  {'Unit':<6}  {'Cost/unit':>10}  {'Low at':>8}")
    print("-" * 62)
    for m in mats:
        flag = " !" if m["stock"] <= m["low_threshold"] else ""
        print(f"{m['name']:<20} {m['stock']:>10.1f}  {m['unit']:<6}  ${m['cost_per_unit']:>9.4f}  {m['low_threshold']:>8.0f}{flag}")


def cmd_set_stock(name: str, amount: float):
    init_db()
    if not get_material(name):
        print(f"Unknown material: {name}")
        sys.exit(1)
    set_material_stock(name, amount)
    print(f"Set {name} stock to {amount}")


def cmd_add_stock(name: str, amount: float):
    init_db()
    if not get_material(name):
        print(f"Unknown material: {name}")
        sys.exit(1)
    add_material_stock(name, amount)
    m = get_material(name)
    print(f"Added {amount} to {name} — new stock: {m['stock']:.1f}")


def cmd_set_cost(name: str, cost: float):
    init_db()
    if not get_material(name):
        print(f"Unknown material: {name}")
        sys.exit(1)
    set_material_cost(name, cost)
    print(f"Set {name} cost to ${cost:.4f}/unit")


def cmd_settings():
    init_db()
    settings = get_all_settings()
    print(f"{'Key':<30}  Value")
    print("-" * 50)
    for s in settings:
        print(f"{s['key']:<30}  {s['value']}")


def cmd_set_setting(key: str, value: str):
    init_db()
    set_setting(key, value)
    print(f"Set {key} = {value}")


def main():
    args = sys.argv[1:]
    if not args or args[0] == "help":
        print(__doc__)
        return

    cmd = args[0]
    if cmd == "sync":
        cmd_sync()
    elif cmd == "listings":
        cmd_listings()
    elif cmd == "low":
        threshold = int(args[1]) if len(args) > 1 else 3
        cmd_low(threshold)
    elif cmd == "set-qty":
        if len(args) < 3:
            print("Usage: python cli.py set-qty <listing_id> <qty>")
            sys.exit(1)
        cmd_set_qty(int(args[1]), int(args[2]))
    elif cmd == "orders":
        limit = int(args[1]) if len(args) > 1 else 20
        cmd_orders(limit)
    elif cmd == "revenue":
        cmd_revenue()
    elif cmd == "materials":
        cmd_materials()
    elif cmd == "set-stock":
        if len(args) < 3:
            print("Usage: python cli.py set-stock <material> <amount>")
            sys.exit(1)
        cmd_set_stock(args[1], float(args[2]))
    elif cmd == "add-stock":
        if len(args) < 3:
            print("Usage: python cli.py add-stock <material> <amount>")
            sys.exit(1)
        cmd_add_stock(args[1], float(args[2]))
    elif cmd == "set-cost":
        if len(args) < 3:
            print("Usage: python cli.py set-cost <material> <cost>")
            sys.exit(1)
        cmd_set_cost(args[1], float(args[2]))
    elif cmd == "settings":
        cmd_settings()
    elif cmd == "set-setting":
        if len(args) < 3:
            print("Usage: python cli.py set-setting <key> <value>")
            sys.exit(1)
        cmd_set_setting(args[1], args[2])
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
