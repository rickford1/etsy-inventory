"""
Thin wrapper around the Meta Marketing API (Graph API v21).
All methods return parsed JSON dicts/lists.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

META_ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]
META_AD_ACCOUNT_ID = os.environ["META_AD_ACCOUNT_ID"]
BASE = "https://graph.facebook.com/v21.0"


class MetaClient:
    def __init__(self):
        self._session = requests.Session()

    def _get(self, path: str, params: dict = None) -> dict:
        params = {**(params or {}), "access_token": META_ACCESS_TOKEN}
        resp = self._session.get(f"{BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    # --- Account / campaigns ---

    def get_account(self) -> dict:
        return self._get(
            f"/{META_AD_ACCOUNT_ID}",
            params={"fields": "id,account_id,name,account_status,currency,timezone_name,amount_spent"},
        )

    def get_campaigns(self) -> list[dict]:
        data = self._get(
            f"/{META_AD_ACCOUNT_ID}/campaigns",
            params={
                "fields": "id,name,objective,status,created_time,start_time,stop_time,daily_budget,lifetime_budget",
                "limit": 100,
            },
        )
        return data.get("data", [])

    # --- Insights ---

    def get_daily_insights(self, days_back: int = 90, level: str = "account") -> list[dict]:
        """Daily breakdown of spend / impressions / clicks for the past `days_back` days.
        level: 'account' for a single rollup, 'campaign' for per-campaign daily rows."""
        date_preset = {
            7: "last_7d", 14: "last_14d", 30: "last_30d", 90: "last_90d",
        }.get(days_back, "last_90d")

        params = {
            "date_preset": date_preset,
            "time_increment": 1,
            "fields": "spend,impressions,clicks,inline_link_clicks,cpc,cpm,ctr,reach,frequency,campaign_id,campaign_name",
            "level": level,
            "limit": 500,
        }
        data = self._get(f"/{META_AD_ACCOUNT_ID}/insights", params=params)
        return data.get("data", [])
