"""
Streamlit dashboard for the Etsy inventory + analytics system.
Run with: streamlit run app.py
"""

import os
import sqlite3
from datetime import datetime, timezone

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from inventory import (
    DB_PATH, init_db,
    get_setting,
    get_revenue_summary,
    get_roas_breakdown,
    get_meta_spend_recent,
)


load_dotenv()
st.set_page_config(page_title="Etsy Shop Dashboard", layout="wide", page_icon=":bar_chart:")
init_db()


# --- Helpers ---

def fmt_dollars(v):
    return f"${(v or 0):,.2f}"


def fmt_pct(v):
    return f"{(v or 0):.1f}%"


def last_sync_display():
    ts = get_setting("last_sync_at")
    if not ts:
        return "never"
    try:
        dt = datetime.fromisoformat(ts)
        now = datetime.now(timezone.utc)
        delta = now - dt
        mins = int(delta.total_seconds() / 60)
        if mins < 60:
            return f"{mins} min ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except Exception:
        return ts


def run_sync():
    """Imports inside the function so the dashboard can start even if Etsy auth is missing."""
    from etsy_client import EtsyClient
    from sync import (
        sync_listings, sync_orders, sync_ledger, sync_payments,
        sync_meta_spend, stamp_last_sync,
    )
    client = EtsyClient()
    with st.status("Syncing...", expanded=True) as status:
        st.write("Pulling Etsy listings...")
        sync_listings(client)
        st.write("Pulling Etsy orders...")
        sync_orders(client)
        st.write("Pulling Etsy ledger (fees, refunds, shipping)...")
        sync_ledger(client)
        st.write("Pulling per-receipt processing fees...")
        sync_payments(client)
        st.write("Pulling Meta ad spend...")
        sync_meta_spend()
        stamp_last_sync()
        status.update(label="Sync complete", state="complete")


# --- Sidebar nav ---

with st.sidebar:
    st.title(os.getenv("SHOP_NAME") or "Etsy Shop")
    st.caption(f"Last sync: {last_sync_display()}")
    if st.button("Sync now", use_container_width=True, type="primary"):
        run_sync()
        st.rerun()
    st.divider()
    page = st.radio("Page", ["Overview", "Revenue & Profit", "Marketing & ROAS"], label_visibility="collapsed")


# --- Overview ---

def page_overview():
    st.header("Overview")
    summary = get_revenue_summary()
    all_time = summary["all_time"]
    last_30 = summary["last_30"]
    last_90 = summary["last_90"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Lifetime revenue", fmt_dollars(all_time["revenue"]), help=f"{all_time['order_count']} orders all-time")
    c2.metric("Lifetime profit", fmt_dollars(all_time["net_profit"]),
              help=f"After fees, shipping, refunds, COGS  |  {all_time['net_profit']/all_time['revenue']*100:.0f}% margin" if all_time["revenue"] else None)
    c3.metric("Last 30d revenue", fmt_dollars(last_30["revenue"]), help=f"{last_30['order_count']} orders")
    c4.metric("Last 30d profit", fmt_dollars(last_30["net_profit"]),
              help=f"{last_30['net_profit']/last_30['revenue']*100:.0f}% margin" if last_30["revenue"] else None)

    st.divider()
    st.subheader("This vs last")
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Last 30 days**")
        st.markdown(
            f"- Revenue: **{fmt_dollars(last_30['revenue'])}**  ·  {last_30['order_count']} orders\n"
            f"- Fees: -{fmt_dollars(last_30['fees'])}\n"
            f"- Shipping labels: -{fmt_dollars(last_30['shipping'])}\n"
            f"- Refunds: -{fmt_dollars(last_30['refunds'])}\n"
            f"- COGS: -{fmt_dollars(last_30['cogs'])}\n"
            f"- **Profit: {fmt_dollars(last_30['net_profit'])}**"
        )
    with col_b:
        st.markdown("**Last 90 days**")
        st.markdown(
            f"- Revenue: **{fmt_dollars(last_90['revenue'])}**  ·  {last_90['order_count']} orders\n"
            f"- Fees: -{fmt_dollars(last_90['fees'])}\n"
            f"- Shipping labels: -{fmt_dollars(last_90['shipping'])}\n"
            f"- Refunds: -{fmt_dollars(last_90['refunds'])}\n"
            f"- COGS: -{fmt_dollars(last_90['cogs'])}\n"
            f"- **Profit: {fmt_dollars(last_90['net_profit'])}**"
        )


# --- Revenue & Profit ---

def daily_revenue_df(days: int) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            f"""
            SELECT substr(created_at, 1, 10) as date,
                   COUNT(*) as orders,
                   COALESCE(SUM(total_price), 0) as revenue,
                   COALESCE(SUM(cogs), 0) as cogs,
                   COALESCE(SUM(transaction_fee + processing_fee + offsite_ads_fee
                               + listing_renewal_fee + sales_tax + other_fees), 0) as fees,
                   COALESCE(SUM(shipping_cost), 0) as shipping,
                   COALESCE(SUM(refund_amount), 0) as refunds
            FROM orders
            WHERE status != 'cancelled'
              AND created_at >= datetime('now', '-{int(days)} days')
            GROUP BY date
            ORDER BY date
            """, conn)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["profit"] = df["revenue"] - df["cogs"] - df["fees"] - df["shipping"] - df["refunds"]
    return df


def fee_breakdown_df(days: int) -> pd.DataFrame:
    """Period-accurate fee breakdown from ledger_entries (source of truth)."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(f"""
            SELECT ledger_type, COALESCE(-SUM(amount), 0) as total
            FROM ledger_entries
            WHERE created_at >= datetime('now', '-{int(days)} days')
              AND ledger_type IN (
                'transaction','PAYMENT_PROCESSING_FEE','offsite_ads_fee','sales_tax',
                'renew_sold_auto','renew_sold','buyer_fee','transaction_quantity','shipping_labels'
              )
            GROUP BY ledger_type
        """).fetchall()
    label_map = {
        "transaction":             "Transaction fee (6.5%)",
        "PAYMENT_PROCESSING_FEE":  "Processing fee (3% + $0.25)",
        "offsite_ads_fee":         "Offsite ads fee",
        "sales_tax":               "Sales tax (passthrough)",
        "renew_sold_auto":         "Listing renewals",
        "renew_sold":              "Listing renewals",
        "buyer_fee":               "Buyer fees",
        "transaction_quantity":    "Quantity fees",
        "shipping_labels":         "Shipping labels",
    }
    agg = {}
    for ledger_type, total in rows:
        label = label_map.get(ledger_type, ledger_type)
        agg[label] = agg.get(label, 0) + (total or 0)
    return pd.DataFrame([{"category": k, "amount": v} for k, v in agg.items() if v > 0])


def page_revenue():
    st.header("Revenue & Profit")
    days = st.slider("Window (days)", min_value=14, max_value=365, value=90, step=7)

    summary = get_revenue_summary()
    key_period = "last_30" if days == 30 else ("last_90" if days == 90 else "all_time")
    period_data = summary[key_period]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Revenue", fmt_dollars(period_data["revenue"]), help=f"{period_data['order_count']} orders")
    c2.metric("Net profit", fmt_dollars(period_data["net_profit"]),
              help=f"{period_data['net_profit']/period_data['revenue']*100:.0f}% margin" if period_data["revenue"] else None)
    c3.metric("Fees + shipping", fmt_dollars(period_data["fees"] + period_data["shipping"]))
    c4.metric("COGS", fmt_dollars(period_data["cogs"]))

    df = daily_revenue_df(days)
    if df.empty:
        st.info("No data in window.")
        return

    st.subheader("Daily revenue + profit")
    long_df = df.melt(id_vars=["date"], value_vars=["revenue", "profit"],
                     var_name="series", value_name="amount")
    chart = alt.Chart(long_df).mark_line(point=True).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("amount:Q", title="USD"),
        color=alt.Color("series:N",
                       scale=alt.Scale(domain=["revenue", "profit"], range=["#4C78A8", "#54A24B"]),
                       legend=alt.Legend(title=None)),
        tooltip=[alt.Tooltip("date:T"), alt.Tooltip("amount:Q", format=",.2f"), "series:N"],
    ).properties(height=300)
    st.altair_chart(chart, use_container_width=True)

    st.subheader("Where the money goes (period totals)")
    fee_df = fee_breakdown_df(days)
    if not fee_df.empty:
        pie = alt.Chart(fee_df).mark_arc(innerRadius=50).encode(
            theta=alt.Theta("amount:Q"),
            color=alt.Color("category:N", legend=alt.Legend(title=None)),
            tooltip=["category:N", alt.Tooltip("amount:Q", format=",.2f")],
        ).properties(height=300)
        st.altair_chart(pie, use_container_width=True)


# --- Marketing & ROAS ---

def page_marketing():
    st.header("Marketing & ROAS")
    days = st.slider("Window (days)", min_value=14, max_value=365, value=90, step=7)

    r = get_roas_breakdown(days)
    if r["ad_days"] == 0:
        st.info("No Meta ad spend in this window. Run a sync once your campaign has data.")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Ad spend", fmt_dollars(r["ad_spend"]))
    c2.metric("Ad days", f"{r['ad_days']}")
    c3.metric("Raw ROAS", f"{r['raw_roas']:.2f}x", help="Etsy revenue on ad days ÷ ad spend. Inflated — most sales would've happened anyway.")
    c4.metric("Lift ROAS", f"{r['lift_roas']:.2f}x",
              delta=f"{(r['lift_roas']-1)*100:+.0f}% vs break-even",
              help="Incremental revenue (ad-day avg − no-ad-day avg) × ad days, ÷ spend. The honest number.")

    if r['ad_days'] < 14:
        st.warning(f"Only {r['ad_days']} ad days — lift ROAS isn't statistically reliable yet. Run a longer campaign.")

    st.divider()
    st.subheader("Daily: Meta spend vs Etsy revenue")
    df = pd.DataFrame(r["daily"])
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        base = alt.Chart(df).encode(x=alt.X("date:T", title=None))
        bars = base.mark_bar(color="#9467bd", opacity=0.6).encode(
            y=alt.Y("spend:Q", title="Meta spend ($)"),
            tooltip=[alt.Tooltip("date:T"), alt.Tooltip("spend:Q", format=",.2f"), "link_clicks:Q"],
        )
        line = base.mark_line(color="#1f77b4", point=True).encode(
            y=alt.Y("revenue:Q", title="Etsy revenue ($)", axis=alt.Axis(orient="right")),
            tooltip=[alt.Tooltip("date:T"), alt.Tooltip("revenue:Q", format=",.2f"), "orders:Q"],
        )
        chart = alt.layer(bars, line).resolve_scale(y="independent").properties(height=350)
        st.altair_chart(chart, use_container_width=True)

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Baseline (ad-off days)**")
        st.markdown(
            f"- {r['no_ad_days']} days\n"
            f"- {r['no_ad_orders']} orders\n"
            f"- {fmt_dollars(r['no_ad_revenue'])} revenue\n"
            f"- **{fmt_dollars(r['baseline_per_day'])}/day** average"
        )
    with c2:
        st.markdown("**While ads ran**")
        st.markdown(
            f"- {r['ad_days']} days\n"
            f"- {r['ad_orders']} orders\n"
            f"- {fmt_dollars(r['ad_revenue'])} revenue\n"
            f"- **{fmt_dollars(r['ad_per_day'])}/day** average\n"
            f"- Daily lift: **{fmt_dollars(r['lift_per_day'])}**\n"
            f"- Total lift: **{fmt_dollars(r['lift_total'])}** vs **{fmt_dollars(r['ad_spend'])}** spent"
        )

    st.divider()
    st.subheader("Recent ad days")
    rows = get_meta_spend_recent(days)
    if rows:
        recent = pd.DataFrame(rows)
        recent = recent.rename(columns={
            "date": "Date", "campaign_name": "Campaign", "spend": "Spend",
            "impressions": "Impressions", "clicks": "Clicks",
            "link_clicks": "Link clicks", "cpc": "CPC", "ctr": "CTR %",
        })
        st.dataframe(recent, hide_index=True, use_container_width=True)


# --- Router ---

if page == "Overview":
    page_overview()
elif page == "Revenue & Profit":
    page_revenue()
elif page == "Marketing & ROAS":
    page_marketing()
