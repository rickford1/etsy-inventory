"""
Local SQLite inventory database.
Tracks listings, materials, stock levels, and order history.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = "inventory.db"

GRAMS_PER_ITEM = 84
GRAMS_PER_ROLL = 1000

# Per-order consumables (counts)
BUNGEES_PER_ORDER = 2
ENVELOPES_PER_ORDER = 1
LABELS_PER_ORDER = 1


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS materials (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL UNIQUE,
                unit           TEXT NOT NULL,
                stock          REAL DEFAULT 0,
                cost_per_unit  REAL DEFAULT 0,
                low_threshold  REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS listings (
                listing_id   INTEGER PRIMARY KEY,
                title        TEXT NOT NULL,
                sku          TEXT,
                quantity     INTEGER DEFAULT 0,
                price        REAL,
                last_synced  TEXT
            );

            CREATE TABLE IF NOT EXISTS orders (
                receipt_id          INTEGER PRIMARY KEY,
                created_at          TEXT,
                buyer_name          TEXT,
                status              TEXT,
                total_price         REAL,
                cogs                REAL DEFAULT 0,
                processed           INTEGER DEFAULT 0,
                transaction_fee     REAL DEFAULT 0,
                processing_fee      REAL DEFAULT 0,
                offsite_ads_fee     REAL DEFAULT 0,
                listing_renewal_fee REAL DEFAULT 0,
                refund_amount       REAL DEFAULT 0,
                sales_tax           REAL DEFAULT 0,
                other_fees          REAL DEFAULT 0,
                shipping_cost       REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id      INTEGER REFERENCES orders(receipt_id),
                listing_id      INTEGER,
                transaction_id  INTEGER,
                title           TEXT,
                quantity        INTEGER,
                price           REAL
            );
            -- idx_items_txn index is created in _migrate_orders_columns after ensuring the column exists

            CREATE TABLE IF NOT EXISTS ledger_entries (
                entry_id        INTEGER PRIMARY KEY,
                sequence_number INTEGER,
                amount          REAL NOT NULL,
                currency        TEXT,
                description     TEXT,
                ledger_type     TEXT,
                reference_type  TEXT,
                reference_id    INTEGER,
                parent_entry_id INTEGER,
                created_at      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_ledger_type ON ledger_entries(ledger_type);
            CREATE INDEX IF NOT EXISTS idx_ledger_ref ON ledger_entries(reference_type, reference_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_created ON ledger_entries(created_at);

            CREATE TABLE IF NOT EXISTS meta_spend (
                date          TEXT NOT NULL,
                campaign_id   TEXT NOT NULL DEFAULT 'account',
                campaign_name TEXT,
                spend         REAL DEFAULT 0,
                impressions   INTEGER DEFAULT 0,
                clicks        INTEGER DEFAULT 0,
                link_clicks   INTEGER DEFAULT 0,
                cpc           REAL DEFAULT 0,
                ctr           REAL DEFAULT 0,
                reach         INTEGER DEFAULT 0,
                PRIMARY KEY (date, campaign_id)
            );

            CREATE INDEX IF NOT EXISTS idx_meta_date ON meta_spend(date);
        """)
        _migrate_orders_columns(conn)
        _seed_defaults(conn)


def _migrate_orders_columns(conn):
    """Add fee/shipping columns to existing orders + transaction_id to order_items (idempotent)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
    new_cols = [
        ("transaction_fee",     "REAL DEFAULT 0"),
        ("processing_fee",      "REAL DEFAULT 0"),
        ("offsite_ads_fee",     "REAL DEFAULT 0"),
        ("listing_renewal_fee", "REAL DEFAULT 0"),
        ("refund_amount",       "REAL DEFAULT 0"),
        ("sales_tax",           "REAL DEFAULT 0"),
        ("other_fees",          "REAL DEFAULT 0"),
        ("shipping_cost",       "REAL DEFAULT 0"),
    ]
    for col, defn in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {defn}")

    item_cols = {row[1] for row in conn.execute("PRAGMA table_info(order_items)").fetchall()}
    if "transaction_id" not in item_cols:
        conn.execute("ALTER TABLE order_items ADD COLUMN transaction_id INTEGER")
    # Safe to call after the column is guaranteed to exist
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_txn ON order_items(transaction_id)")

    ledger_cols = {row[1] for row in conn.execute("PRAGMA table_info(ledger_entries)").fetchall()}
    if "parent_entry_id" not in ledger_cols:
        conn.execute("ALTER TABLE ledger_entries ADD COLUMN parent_entry_id INTEGER")


def _seed_defaults(conn):
    # Default settings
    defaults = {
        "filament_cost_per_roll": "11.00",
        "active_filament": "filament_1",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )

    # Seed materials if not present
    materials = [
        ("filament_1", "grams", 1000, None, 200),
        ("filament_2", "grams", 1000, None, 200),
        ("bungee_cords", "count", 100, 0.10, 20),
        ("envelopes",    "count", 100, 0.15, 20),
        ("labels",       "count", 200, 0.05, 50),
    ]
    for name, unit, stock, cost, low in materials:
        conn.execute("""
            INSERT OR IGNORE INTO materials (name, unit, stock, cost_per_unit, low_threshold)
            VALUES (?, ?, ?, ?, ?)
        """, (name, unit, stock, cost, low))

    # Set filament cost from settings
    row = conn.execute("SELECT value FROM settings WHERE key='filament_cost_per_roll'").fetchone()
    if row:
        cost_per_gram = float(row["value"]) / GRAMS_PER_ROLL
        conn.execute(
            "UPDATE materials SET cost_per_unit=? WHERE name IN ('filament_1','filament_2')",
            (cost_per_gram,)
        )


# --- Settings ---

def get_all_settings() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM settings ORDER BY key").fetchall()
        return [dict(r) for r in rows]


def get_setting(key: str) -> str | None:
    with _db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str):
    with _db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        # Keep filament cost in sync
        if key == "filament_cost_per_roll":
            cost_per_gram = float(value) / GRAMS_PER_ROLL
            conn.execute(
                "UPDATE materials SET cost_per_unit=? WHERE name IN ('filament_1','filament_2')",
                (cost_per_gram,)
            )


def get_active_filament() -> str:
    return get_setting("active_filament") or "filament_1"


def set_active_filament(name: str):
    set_setting("active_filament", name)


# --- Materials ---

def get_material(name: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM materials WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def get_all_materials() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM materials ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def set_material_stock(name: str, stock: float):
    with _db() as conn:
        conn.execute("UPDATE materials SET stock=? WHERE name=?", (stock, name))


def set_material_cost(name: str, cost_per_unit: float):
    with _db() as conn:
        conn.execute("UPDATE materials SET cost_per_unit=? WHERE name=?", (cost_per_unit, name))


def add_material_stock(name: str, amount: float):
    with _db() as conn:
        conn.execute("UPDATE materials SET stock=stock+? WHERE name=?", (amount, name))


def deduct_materials_for_order(item_count: int) -> float:
    """Deduct filament + consumables for one order. Returns COGS."""
    filament_name = get_active_filament()
    filament_grams = item_count * GRAMS_PER_ITEM

    with _db() as conn:
        conn.execute(
            "UPDATE materials SET stock=MAX(0, stock-?) WHERE name=?",
            (filament_grams, filament_name)
        )
        conn.execute(
            "UPDATE materials SET stock=MAX(0, stock-?) WHERE name='bungee_cords'",
            (BUNGEES_PER_ORDER,)
        )
        conn.execute(
            "UPDATE materials SET stock=MAX(0, stock-?) WHERE name='envelopes'",
            (ENVELOPES_PER_ORDER,)
        )
        conn.execute(
            "UPDATE materials SET stock=MAX(0, stock-?) WHERE name='labels'",
            (LABELS_PER_ORDER,)
        )

        # Calculate COGS
        filament = conn.execute("SELECT cost_per_unit FROM materials WHERE name=?", (filament_name,)).fetchone()
        bungee   = conn.execute("SELECT cost_per_unit FROM materials WHERE name='bungee_cords'").fetchone()
        envelope = conn.execute("SELECT cost_per_unit FROM materials WHERE name='envelopes'").fetchone()
        label    = conn.execute("SELECT cost_per_unit FROM materials WHERE name='labels'").fetchone()

        cogs = (
            filament_grams * (filament["cost_per_unit"] if filament else 0)
            + BUNGEES_PER_ORDER * (bungee["cost_per_unit"] if bungee else 0)
            + ENVELOPES_PER_ORDER * (envelope["cost_per_unit"] if envelope else 0)
            + LABELS_PER_ORDER * (label["cost_per_unit"] if label else 0)
        )
        return cogs


def get_low_materials() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM materials WHERE stock <= low_threshold ORDER BY stock ASC"
        ).fetchall()
        return [dict(r) for r in rows]


# --- Listings ---

def upsert_listing(listing_id: int, title: str, sku: str, quantity: int, price: float):
    with _db() as conn:
        conn.execute("""
            INSERT INTO listings (listing_id, title, sku, quantity, price, last_synced)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
                title=excluded.title, sku=excluded.sku, quantity=excluded.quantity,
                price=excluded.price, last_synced=excluded.last_synced
        """, (listing_id, title, sku, quantity, price, datetime.utcnow().isoformat()))


def get_all_listings() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM listings ORDER BY title").fetchall()
        return [dict(r) for r in rows]


def get_low_stock(threshold: int = 3) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE quantity <= ? ORDER BY quantity ASC", (threshold,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Orders ---

def upsert_order(receipt_id: int, created_at: str, buyer_name: str, status: str, total: float) -> bool:
    """Returns True if this is a new order (not seen before)."""
    with _db() as conn:
        existing = conn.execute("SELECT receipt_id FROM orders WHERE receipt_id=?", (receipt_id,)).fetchone()
        if existing:
            conn.execute("UPDATE orders SET status=? WHERE receipt_id=?", (status, receipt_id))
            return False
        conn.execute("""
            INSERT INTO orders (receipt_id, created_at, buyer_name, status, total_price)
            VALUES (?, ?, ?, ?, ?)
        """, (receipt_id, created_at, buyer_name, status, total))
        return True


def set_order_cogs(receipt_id: int, cogs: float):
    with _db() as conn:
        conn.execute("UPDATE orders SET cogs=? WHERE receipt_id=?", (cogs, receipt_id))


def clear_order_items(receipt_id: int):
    with _db() as conn:
        conn.execute("DELETE FROM order_items WHERE receipt_id=?", (receipt_id,))


def upsert_order_item(receipt_id: int, listing_id: int, title: str, quantity: int, price: float, transaction_id: int = None):
    with _db() as conn:
        # Idempotent by (receipt_id, transaction_id) when transaction_id is known.
        if transaction_id:
            existing = conn.execute(
                "SELECT id FROM order_items WHERE receipt_id=? AND transaction_id=?",
                (receipt_id, transaction_id)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE order_items SET listing_id=?, title=?, quantity=?, price=? WHERE id=?",
                    (listing_id, title, quantity, price, existing["id"])
                )
                return
        conn.execute("""
            INSERT INTO order_items (receipt_id, listing_id, transaction_id, title, quantity, price)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (receipt_id, listing_id, transaction_id, title, quantity, price))


def mark_order_processed(receipt_id: int):
    with _db() as conn:
        conn.execute("UPDATE orders SET processed=1 WHERE receipt_id=?", (receipt_id,))


def get_orders(limit: int = 50) -> list[dict]:
    with _db() as conn:
        rows = conn.execute("""
            SELECT o.receipt_id, o.created_at, o.buyer_name, o.status,
                   o.total_price, o.cogs,
                   GROUP_CONCAT(oi.title || ' x' || oi.quantity, ', ') as items
            FROM orders o
            LEFT JOIN order_items oi ON oi.receipt_id = o.receipt_id
            WHERE o.status != 'cancelled'
            GROUP BY o.receipt_id
            ORDER BY o.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_orders_with_fees(limit: int = 50) -> list[dict]:
    """Orders joined with items + all per-order fee/shipping fields."""
    with _db() as conn:
        rows = conn.execute("""
            SELECT o.receipt_id, o.created_at, o.status,
                   o.total_price, o.cogs, o.shipping_cost,
                   o.transaction_fee, o.processing_fee, o.offsite_ads_fee,
                   o.listing_renewal_fee, o.refund_amount, o.sales_tax, o.other_fees,
                   GROUP_CONCAT(oi.title || ' x' || oi.quantity, ', ') as items
            FROM orders o
            LEFT JOIN order_items oi ON oi.receipt_id = o.receipt_id
            WHERE o.status != 'cancelled'
            GROUP BY o.receipt_id
            ORDER BY o.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


# --- Ledger entries ---

def upsert_ledger_entry(entry: dict):
    """Insert/replace a ledger entry. amount stored in dollars (not cents)."""
    with _db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO ledger_entries
                (entry_id, sequence_number, amount, currency, description,
                 ledger_type, reference_type, reference_id, parent_entry_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry["entry_id"],
            entry.get("sequence_number"),
            entry["amount"] / 100.0,
            entry.get("currency"),
            entry.get("description"),
            entry.get("ledger_type"),
            entry.get("reference_type"),
            entry.get("reference_id"),
            entry.get("parent_entry_id"),
            entry.get("created_at"),
        ))


def get_period_shipping_total(min_created: str = None, max_created: str = None) -> float:
    """Sum of shipping_labels ledger entries within a date range (or all-time).
    Returns a positive number (cost). Etsy stores label costs as negative ledger amounts."""
    with _db() as conn:
        q = "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries WHERE ledger_type='shipping_labels'"
        params = []
        if min_created:
            q += " AND created_at >= ?"
            params.append(min_created)
        if max_created:
            q += " AND created_at <= ?"
            params.append(max_created)
        return -float(conn.execute(q, params).fetchone()[0])  # negate to express as positive cost


def get_orders_count_in_period(min_created: str = None, max_created: str = None) -> int:
    with _db() as conn:
        q = "SELECT COUNT(*) FROM orders WHERE status != 'cancelled'"
        params = []
        if min_created:
            q += " AND created_at >= ?"
            params.append(min_created)
        if max_created:
            q += " AND created_at <= ?"
            params.append(max_created)
        return int(conn.execute(q, params).fetchone()[0])


def aggregate_fees_into_orders():
    """Compute per-order fee totals from ledger_entries.
    Attribution strategy:
      - transaction_fee: ledger ref=transaction → join via order_items.transaction_id
      - sales_tax: ledger ref=receipt → join via reference_id = orders.receipt_id
      - refunds: matched by parent_entry's transaction, then by direct receipt ref
      - processing_fee: set separately by sync_payments()
      - offsite_ads_fee, listing_renewal_fee: can't be order-attributed; averaged across all orders
        (see apply_shipping_average for the same pattern)
    """
    with _db() as conn:
        # Zero out fields we're about to recompute so this is idempotent
        conn.execute("""
            UPDATE orders
            SET transaction_fee=0, offsite_ads_fee=0, refund_amount=0,
                sales_tax=0, listing_renewal_fee=0, other_fees=0
        """)

        # transaction_fee: ref=transaction, joined via order_items
        conn.execute("""
            UPDATE orders
            SET transaction_fee = COALESCE((
                SELECT -SUM(le.amount)
                FROM ledger_entries le
                JOIN order_items oi ON oi.transaction_id = le.reference_id
                WHERE le.ledger_type = 'transaction'
                  AND le.reference_type = 'transaction'
                  AND oi.receipt_id = orders.receipt_id
            ), 0)
        """)

        # sales_tax: ref=receipt, joined directly
        conn.execute("""
            UPDATE orders
            SET sales_tax = COALESCE((
                SELECT -SUM(le.amount)
                FROM ledger_entries le
                WHERE le.ledger_type = 'sales_tax'
                  AND le.reference_type = 'receipt'
                  AND le.reference_id = orders.receipt_id
            ), 0)
        """)

        # refunds: REFUND_GROSS goes to buyer (negative ledger amount, cost to seller).
        # transaction_refund / REFUND_PROCESSING_FEE / sales_tax_refund are positive ledger
        # amounts (Etsy returning fees to seller after the refund). Net cost is the sum.
        # Try transaction-level match first, then receipt-level.
        conn.execute("""
            UPDATE orders
            SET refund_amount = COALESCE((
                SELECT -SUM(le.amount)
                FROM ledger_entries le
                WHERE le.ledger_type IN ('REFUND_GROSS','transaction_refund','sales_tax_refund','REFUND_PROCESSING_FEE')
                  AND (
                       le.reference_id = orders.receipt_id
                    OR le.reference_id IN (SELECT oi.transaction_id FROM order_items oi WHERE oi.receipt_id = orders.receipt_id)
                    OR le.parent_entry_id IN (
                        SELECT le2.entry_id FROM ledger_entries le2
                        WHERE le2.reference_id = orders.receipt_id
                           OR le2.reference_id IN (SELECT oi.transaction_id FROM order_items oi WHERE oi.receipt_id = orders.receipt_id)
                       )
                  )
            ), 0)
        """)


def apply_period_average_fees():
    """Distribute non-attributable fees (offsite_ads, listing_renewals, buyer_fee) evenly
    across orders, similar to shipping_cost. Stored on each order so per-order rows roughly
    sum to the period total. Returns a dict of the averages applied."""
    with _db() as conn:
        order_count = int(conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status != 'cancelled'"
        ).fetchone()[0])
        if order_count == 0:
            return {}

        averages = {}
        for col, ledger_type in [
            ("offsite_ads_fee",     "offsite_ads_fee"),
            ("listing_renewal_fee", "renew_sold_auto"),
        ]:
            total = -float(conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries WHERE ledger_type=?",
                (ledger_type,)
            ).fetchone()[0])
            avg = total / order_count
            conn.execute(f"UPDATE orders SET {col}=? WHERE status != 'cancelled'", (avg,))
            averages[col] = avg
        return averages


def apply_shipping_average():
    """Compute total_shipping / total_orders and stamp it onto every order.shipping_cost.
    Uses all-time average across the dataset."""
    with _db() as conn:
        total_shipping = -float(conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries WHERE ledger_type='shipping_labels'"
        ).fetchone()[0])
        order_count = int(conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status != 'cancelled'"
        ).fetchone()[0])
        if order_count == 0:
            return 0.0
        avg = total_shipping / order_count
        conn.execute("UPDATE orders SET shipping_cost=? WHERE status != 'cancelled'", (avg,))
        return avg


# --- Meta ad spend ---

def upsert_meta_spend(date: str, campaign_id: str, campaign_name: str,
                     spend: float, impressions: int, clicks: int,
                     link_clicks: int, cpc: float, ctr: float, reach: int):
    with _db() as conn:
        conn.execute("""
            INSERT INTO meta_spend
                (date, campaign_id, campaign_name, spend, impressions, clicks, link_clicks, cpc, ctr, reach)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, campaign_id) DO UPDATE SET
                campaign_name=excluded.campaign_name,
                spend=excluded.spend, impressions=excluded.impressions,
                clicks=excluded.clicks, link_clicks=excluded.link_clicks,
                cpc=excluded.cpc, ctr=excluded.ctr, reach=excluded.reach
        """, (date, campaign_id, campaign_name, spend, impressions, clicks, link_clicks, cpc, ctr, reach))


def get_meta_spend_recent(days: int = 30) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(f"""
            SELECT date, campaign_name, spend, impressions, clicks, link_clicks, cpc, ctr
            FROM meta_spend
            WHERE date >= date('now', '-{int(days)} days')
            ORDER BY date DESC, campaign_id
        """).fetchall()
        return [dict(r) for r in rows]


def get_roas_breakdown(days: int = 90) -> dict:
    """Compute side-by-side daily Meta spend and Etsy revenue. Returns both raw and lift ROAS."""
    with _db() as conn:
        meta_rows = conn.execute(f"""
            SELECT date, SUM(spend) as spend, SUM(link_clicks) as link_clicks
            FROM meta_spend
            WHERE date >= date('now', '-{int(days)} days')
            GROUP BY date
            ORDER BY date
        """).fetchall()

        etsy_rows = conn.execute(f"""
            SELECT substr(created_at, 1, 10) as date,
                   COUNT(*) as orders,
                   COALESCE(SUM(total_price), 0) as revenue
            FROM orders
            WHERE status != 'cancelled'
              AND created_at >= datetime('now', '-{int(days)} days')
            GROUP BY date
            ORDER BY date
        """).fetchall()

    meta_by_date = {r["date"]: dict(r) for r in meta_rows}
    etsy_by_date = {r["date"]: dict(r) for r in etsy_rows}

    # Days where ads ran
    ad_days = sorted(meta_by_date.keys())
    no_ad_days = sorted(d for d in etsy_by_date if d not in meta_by_date)

    ad_spend    = sum(meta_by_date[d]["spend"] for d in ad_days)
    ad_revenue  = sum(etsy_by_date.get(d, {"revenue": 0})["revenue"] for d in ad_days)
    ad_clicks   = sum(meta_by_date[d]["link_clicks"] for d in ad_days)
    ad_orders   = sum(etsy_by_date.get(d, {"orders": 0})["orders"] for d in ad_days)

    no_ad_revenue = sum(etsy_by_date[d]["revenue"] for d in no_ad_days)
    no_ad_orders  = sum(etsy_by_date[d]["orders"] for d in no_ad_days)

    raw_roas = (ad_revenue / ad_spend) if ad_spend > 0 else 0
    baseline_per_day = (no_ad_revenue / len(no_ad_days)) if no_ad_days else 0
    ad_per_day = (ad_revenue / len(ad_days)) if ad_days else 0
    lift_per_day = ad_per_day - baseline_per_day
    lift_total = lift_per_day * len(ad_days)
    lift_roas = (lift_total / ad_spend) if ad_spend > 0 else 0

    daily = []
    for d in sorted(set(meta_by_date) | set(etsy_by_date)):
        m = meta_by_date.get(d, {})
        e = etsy_by_date.get(d, {})
        daily.append({
            "date": d,
            "spend": m.get("spend", 0) or 0,
            "link_clicks": m.get("link_clicks", 0) or 0,
            "orders": e.get("orders", 0) or 0,
            "revenue": e.get("revenue", 0) or 0,
        })

    return {
        "daily": daily,
        "ad_days": len(ad_days),
        "no_ad_days": len(no_ad_days),
        "ad_spend": ad_spend,
        "ad_revenue": ad_revenue,
        "ad_clicks": ad_clicks,
        "ad_orders": ad_orders,
        "no_ad_revenue": no_ad_revenue,
        "no_ad_orders": no_ad_orders,
        "raw_roas": raw_roas,
        "baseline_per_day": baseline_per_day,
        "ad_per_day": ad_per_day,
        "lift_per_day": lift_per_day,
        "lift_total": lift_total,
        "lift_roas": lift_roas,
    }


def get_revenue_summary() -> dict:
    """Period totals are computed directly from ledger_entries (source of truth),
    not from per-order fee columns (which use averages for non-attributable items)."""
    # ledger_types that count as fees taken from the seller (negative ledger amounts).
    # Sales tax is a passthrough (seller collects, Etsy remits) — it's debited from
    # the account so it appears here.
    FEE_TYPES = (
        "transaction", "PAYMENT_PROCESSING_FEE", "offsite_ads_fee", "sales_tax",
        "renew_sold_auto", "renew_sold", "buyer_fee", "transaction_quantity",
    )
    # Refund-related: REFUND_GROSS is the cost (negative ledger), the *_refund types
    # are credits back to the seller (positive). Net is the true refund cost.
    REFUND_TYPES = (
        "REFUND_GROSS", "transaction_refund", "sales_tax_refund",
        "REFUND_PROCESSING_FEE", "transaction_quantity_refund", "renew_sold_auto_refund",
    )
    fee_q   = "SELECT COALESCE(-SUM(amount),0) FROM ledger_entries WHERE ledger_type IN ({}) {}"
    ref_q   = "SELECT COALESCE(-SUM(amount),0) FROM ledger_entries WHERE ledger_type IN ({}) {}"
    ship_q  = "SELECT COALESCE(-SUM(amount),0) FROM ledger_entries WHERE ledger_type='shipping_labels' {}"
    order_q = """SELECT COUNT(*) as order_count, COALESCE(SUM(total_price),0) as revenue,
                        COALESCE(SUM(cogs),0) as cogs
                 FROM orders WHERE status != 'cancelled' {}"""

    periods = {
        "all_time": "",
        "last_30":  "AND created_at >= datetime('now','-30 days')",
        "last_90":  "AND created_at >= datetime('now','-90 days')",
    }
    summary = {}
    with _db() as conn:
        fee_ph = ",".join("?" * len(FEE_TYPES))
        ref_ph = ",".join("?" * len(REFUND_TYPES))
        for name, where in periods.items():
            base = dict(conn.execute(order_q.format(where), ).fetchone())
            base["fees"] = float(conn.execute(
                fee_q.format(fee_ph, where), FEE_TYPES
            ).fetchone()[0])
            base["refunds"] = float(conn.execute(
                ref_q.format(ref_ph, where), REFUND_TYPES
            ).fetchone()[0])
            base["shipping"] = float(conn.execute(
                ship_q.format(where), ).fetchone()[0])
            base["net_profit"] = base["revenue"] - base["cogs"] - base["fees"] - base["shipping"] - base["refunds"]
            summary[name] = base
    return summary