"""
Sync Etsy listings and orders into the local database.
New orders trigger automatic material deductions.
"""

import sqlite3
from datetime import datetime, timezone

from etsy_client import EtsyClient
from inventory import (
    DB_PATH,
    init_db, upsert_listing, upsert_order, upsert_order_item, clear_order_items,
    set_order_cogs, mark_order_processed,
    deduct_materials_for_order, get_low_materials, get_low_stock,
    upsert_ledger_entry, aggregate_fees_into_orders,
    apply_shipping_average, apply_period_average_fees,
    upsert_meta_spend, set_setting,
)


def stamp_last_sync():
    set_setting("last_sync_at", datetime.now(timezone.utc).isoformat())


def sync_listings(client: EtsyClient):
    print("Syncing listings...")
    listings = client.get_all_listings()
    for listing in listings:
        listing_id = listing["listing_id"]
        title = listing.get("title", "")
        price_obj = listing.get("price", {})
        price = float(price_obj.get("amount", 0)) / max(price_obj.get("divisor", 1), 1)

        try:
            inv = client.get_inventory(listing_id)
            products = inv.get("products", [])
            # Only count offerings that are currently enabled on non-deleted products.
            # A listing may have many disabled variations carrying stale quantity values
            # (e.g. 24 color/size combos where only one is actively sellable).
            quantity = sum(
                o.get("quantity", 0)
                for p in products
                if not p.get("is_deleted", False)
                for o in p.get("offerings", [])
                if o.get("is_enabled", True)
            )
            enabled_products = [p for p in products if not p.get("is_deleted", False)]
            sku = enabled_products[0].get("sku", "") if enabled_products else ""
        except Exception:
            quantity = listing.get("quantity", 0)
            sku = ""

        upsert_listing(listing_id, title, sku, quantity, price)

    print(f"  {len(listings)} listings synced")


def sync_orders(client: EtsyClient):
    print("Syncing orders...")
    orders = client.get_all_orders()
    new_count = 0

    for order in orders:
        receipt_id = order["receipt_id"]
        ts = order.get("create_timestamp")
        created_at = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if ts else ""
        )
        buyer_name = order.get("name", "")
        status = "shipped" if order.get("is_shipped") else "unshipped"
        total_obj = order.get("grandtotal", {})
        total = float(total_obj.get("amount", 0)) / max(total_obj.get("divisor", 1), 1)

        is_new = upsert_order(receipt_id, created_at, buyer_name, status, total)

        transactions = order.get("transactions", [])
        # Clear and re-insert items so transaction_id gets backfilled on legacy rows
        # without creating duplicates.
        clear_order_items(receipt_id)
        for item in transactions:
            price_obj = item.get("price", {})
            upsert_order_item(
                receipt_id,
                item.get("listing_id", 0),
                item.get("title", ""),
                item.get("quantity", 1),
                float(price_obj.get("amount", 0)) / max(price_obj.get("divisor", 1), 1),
                transaction_id=item.get("transaction_id"),
            )

        if is_new:
            item_count = sum(t.get("quantity", 1) for t in transactions)
            cogs = deduct_materials_for_order(item_count)
            set_order_cogs(receipt_id, cogs)
            mark_order_processed(receipt_id)
            new_count += 1

    print(f"  {len(orders)} orders synced ({new_count} new)")


def sync_ledger(client: EtsyClient):
    """Pull all ledger entries since shop creation; store + aggregate fees per order."""
    print("Syncing ledger entries...")
    entries = client.get_all_ledger_entries()
    for e in entries:
        ts = e.get("create_date") or e.get("created_timestamp")
        e["created_at"] = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if ts else ""
        )
        upsert_ledger_entry(e)
    print(f"  {len(entries)} ledger entries synced")

    print("  Aggregating fees per order...")
    aggregate_fees_into_orders()

    print("  Applying shipping cost average...")
    avg = apply_shipping_average()
    print(f"    avg shipping cost / order: ${avg:.2f}")

    print("  Applying period averages for non-attributable fees...")
    avgs = apply_period_average_fees()
    for k, v in avgs.items():
        print(f"    avg {k} / order: ${v:.2f}")


def sync_payments(client: EtsyClient):
    """Fetch processing fees from /receipts/{id}/payments for each receipt."""
    print("Syncing per-receipt payment fees...")
    conn = sqlite3.connect(DB_PATH)
    receipts = [r[0] for r in conn.execute(
        "SELECT receipt_id FROM orders WHERE status != 'cancelled'"
    ).fetchall()]
    conn.close()

    updated = 0
    for receipt_id in receipts:
        try:
            payments = client.get_receipt_payments(receipt_id)
        except Exception:
            continue
        if not payments:
            continue
        p = payments[0]
        fees_obj = p.get("adjusted_fees") or p.get("amount_fees", {})
        if not fees_obj:
            continue
        fee = float(fees_obj.get("amount", 0)) / max(fees_obj.get("divisor", 1), 1)
        conn2 = sqlite3.connect(DB_PATH)
        conn2.execute("UPDATE orders SET processing_fee=? WHERE receipt_id=?", (fee, receipt_id))
        conn2.commit()
        conn2.close()
        updated += 1
    print(f"  {updated} receipts updated with processing fees")


def sync_meta_spend(days_back: int = 90):
    """Pull daily per-campaign insights from Meta and upsert into meta_spend."""
    print(f"Syncing Meta ad spend (last {days_back} days)...")
    try:
        from meta_client import MetaClient
    except ImportError:
        print("  meta_client not available, skipping")
        return

    client = MetaClient()
    rows = client.get_daily_insights(days_back=days_back, level="campaign")
    if not rows:
        print("  no spend rows returned (campaign may not have run)")
        return

    for r in rows:
        upsert_meta_spend(
            date=r["date_start"],
            campaign_id=r.get("campaign_id", "account"),
            campaign_name=r.get("campaign_name", ""),
            spend=float(r.get("spend", 0)),
            impressions=int(r.get("impressions", 0)),
            clicks=int(r.get("clicks", 0)),
            link_clicks=int(r.get("inline_link_clicks", 0)),
            cpc=float(r.get("cpc", 0)),
            ctr=float(r.get("ctr", 0)),
            reach=int(r.get("reach", 0)),
        )
    total_spend = sum(float(r.get("spend", 0)) for r in rows)
    print(f"  {len(rows)} daily rows synced  |  ${total_spend:.2f} total spend")


def print_alerts():
    low_materials = get_low_materials()
    if low_materials:
        print(f"\nLow materials ({len(low_materials)}):")
        for m in low_materials:
            print(f"  {m['name']}: {m['stock']:.0f} {m['unit']} remaining")

    low_stock = get_low_stock(threshold=3)
    if low_stock:
        print(f"\nLow Etsy stock ({len(low_stock)} listings):")
        for item in low_stock:
            print(f"  [{item['quantity']}] {item['title']}")


def main():
    init_db()
    client = EtsyClient()
    sync_listings(client)
    sync_orders(client)
    sync_ledger(client)
    sync_payments(client)
    sync_meta_spend()
    stamp_last_sync()
    print_alerts()


if __name__ == "__main__":
    main()
