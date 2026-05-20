"""
Thin wrapper around the Etsy Open API v3.
All methods return parsed JSON dicts/lists.
"""

import os
import time

import requests
from dotenv import load_dotenv

from auth import get_valid_token

load_dotenv()

ETSY_API_KEY = os.environ["ETSY_API_KEY"]
ETSY_SHARED_SECRET = os.environ["ETSY_SHARED_SECRET"]
SHOP_ID = os.environ["ETSY_SHOP_ID"]
BASE = "https://openapi.etsy.com/v3/application"


class EtsyClient:
    def __init__(self):
        self._session = requests.Session()

    def _headers(self):
        return {
            "Authorization": f"Bearer {get_valid_token()}",
            "x-api-key": f"{ETSY_API_KEY}:{ETSY_SHARED_SECRET}",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        resp = self._session.get(f"{BASE}{path}", headers=self._headers(), params=params)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, body: dict) -> dict:
        resp = self._session.put(
            f"{BASE}{path}",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        return resp.json()

    # --- Shop ---

    def get_shop(self) -> dict:
        return self._get(f"/shops/{SHOP_ID}")

    # --- Listings ---

    def get_listings(self, state: str = "active", limit: int = 100, offset: int = 0) -> list[dict]:
        data = self._get(f"/shops/{SHOP_ID}/listings/{state}", params={"limit": limit, "offset": offset})
        return data.get("results", [])

    def get_all_listings(self, state: str = "active") -> list[dict]:
        all_listings = []
        offset = 0
        while True:
            batch = self.get_listings(state=state, limit=100, offset=offset)
            all_listings.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        return all_listings

    # --- Inventory ---

    def get_inventory(self, listing_id: int) -> dict:
        return self._get(f"/listings/{listing_id}/inventory")

    def update_inventory(self, listing_id: int, inventory: dict) -> dict:
        """
        inventory must be the full dict returned by get_inventory() with your
        quantity changes applied. Etsy requires the complete products array.
        """
        return self._put(f"/listings/{listing_id}/inventory", inventory)

    def set_quantity(self, listing_id: int, quantity: int, offering_index: int = 0) -> dict:
        """Convenience: set quantity on a single-variation listing."""
        inv = self.get_inventory(listing_id)
        inv["products"][0]["offerings"][offering_index]["quantity"] = quantity
        # Remove read-only fields Etsy rejects on PUT
        for product in inv.get("products", []):
            product.pop("product_id", None)
            product.pop("is_deleted", None)
            for offering in product.get("offerings", []):
                offering.pop("offering_id", None)
                offering.pop("is_enabled", None)
        return self.update_inventory(listing_id, inv)

    # --- Orders ---

    def get_orders(self, limit: int = 100, offset: int = 0, was_paid: bool = True) -> list[dict]:
        params = {"limit": limit, "offset": offset, "was_paid": str(was_paid).lower()}
        data = self._get(f"/shops/{SHOP_ID}/receipts", params=params)
        return data.get("results", [])

    def get_all_orders(self) -> list[dict]:
        all_orders = []
        offset = 0
        while True:
            batch = self.get_orders(limit=100, offset=offset)
            all_orders.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
            time.sleep(0.1)  # stay under 10 QPS
        return all_orders

    # --- Payments (per-receipt processing fee) ---

    def get_receipt_payments(self, receipt_id: int) -> list[dict]:
        data = self._get(f"/shops/{SHOP_ID}/receipts/{receipt_id}/payments")
        return data.get("results", [])

    # --- Ledger (true source of fees) ---

    def get_ledger_entries(self, min_created: int, max_created: int,
                          limit: int = 100, offset: int = 0) -> list[dict]:
        """Etsy enforces a 31-day max window per call."""
        params = {
            "min_created": min_created,
            "max_created": max_created,
            "limit": limit,
            "offset": offset,
        }
        data = self._get(f"/shops/{SHOP_ID}/payment-account/ledger-entries", params=params)
        return data.get("results", [])

    def get_all_ledger_entries(self, since_ts: int = None) -> list[dict]:
        """Pull all ledger entries since `since_ts` (epoch) up to now, walking 31-day windows."""
        now = int(time.time())
        # Default: shop creation date — pulled from get_shop() if not given.
        if since_ts is None:
            shop = self.get_shop()
            since_ts = shop.get("create_date", now - 365 * 86400)

        WINDOW = 31 * 86400 - 60  # stay just under the 31-day limit
        all_entries: list[dict] = []
        seen: set[int] = set()

        win_start = since_ts
        while win_start < now:
            win_end = min(win_start + WINDOW, now)
            offset = 0
            while True:
                batch = self.get_ledger_entries(win_start, win_end, limit=100, offset=offset)
                fresh = [e for e in batch if e["entry_id"] not in seen]
                for e in fresh:
                    seen.add(e["entry_id"])
                all_entries.extend(fresh)
                if len(batch) < 100:
                    break
                offset += 100
                time.sleep(0.15)
            win_start = win_end
            time.sleep(0.15)
        return all_entries