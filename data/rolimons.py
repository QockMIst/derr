import json
import time
import urllib.request
from typing import Any, Dict, List, Optional

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

DEMAND_LABELS = {
    -1: "Unassigned",
    0: "Terrible",
    1: "Low",
    2: "Normal",
    3: "High",
    4: "Amazing",
}

TREND_LABELS = {
    -1: "Unassigned",
    0: "Lowering",
    1: "Unstable",
    2: "Stable",
    3: "Raising",
    4: "Fluctuating",
}

TAG_MAP = {
    1: "Demand",
    2: "Rares",
    3: "Robux",
    4: "Any",
    5: "Upgrade",
    6: "Downgrade",
    7: "Rap",
    8: "Wishlist",
    9: "Projecteds",
    10: "Adds",
}


class RolimonsClient:
    ITEM_DETAILS_URL = "https://api.rolimons.com/items/v2/itemdetails"
    RECENT_ADS_URL = "https://api.rolimons.com/tradeads/v1/getrecentads"
    PLAYER_ASSETS_URL = "https://api.rolimons.com/players/v1/playerassets/{user_id}"
    SALE_ACTIVITY_URL = "https://api.rolimons.com/market/v1/saleactivity"

    def __init__(self, cache_ttl: int = 300):
        self.cache_ttl = cache_ttl
        self._items_cache: Dict[int, Dict[str, Any]] = {}
        self._last_items_fetch: float = 0.0

    def _get_json(self, url: str, timeout: int = 15) -> Dict[str, Any]:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def fetch_items(self, force: bool = False) -> Dict[int, Dict[str, Any]]:
        now = time.time()
        if not force and self._items_cache and (now - self._last_items_fetch < self.cache_ttl):
            return self._items_cache

        data = self._get_json(self.ITEM_DETAILS_URL)
        if not data.get("success"):
            return self._items_cache

        parsed: Dict[int, Dict[str, Any]] = {}
        for item_id_str, info in data.get("items", {}).items():
            item_id = int(item_id_str)
            rap = int(info[2]) if info[2] is not None and info[2] >= 0 else 0
            raw_val = int(info[3]) if info[3] is not None else -1
            default_val = int(info[4]) if info[4] is not None and info[4] >= 0 else rap
            val = raw_val if raw_val >= 0 else default_val
            item_type = int(info[10]) if len(info) > 10 and info[10] is not None else 1

            parsed[item_id] = {
                "item_id": item_id,
                "name": str(info[0]),
                "acronym": str(info[1] or ""),
                "rap": rap,
                "value": val,
                "is_valued": raw_val >= 0,
                "default_value": default_val,
                "demand": int(info[5]) if info[5] is not None else -1,
                "demand_label": DEMAND_LABELS.get(int(info[5]) if info[5] is not None else -1, "Unassigned"),
                "trend": int(info[6]) if info[6] is not None else -1,
                "trend_label": TREND_LABELS.get(int(info[6]) if info[6] is not None else -1, "Unassigned"),
                "projected": info[7] == 1,
                "hyped": info[8] == 1,
                "rare": info[9] == 1,
                "item_type": item_type,
                "source": "rolimons",
            }

        self._items_cache = parsed
        self._last_items_fetch = now
        return parsed

    def fetch_recent_ads(self) -> List[Dict[str, Any]]:
        data = self._get_json(self.RECENT_ADS_URL)
        if not data.get("success"):
            return []

        ads: List[Dict[str, Any]] = []
        for raw in data.get("trade_ads", []):
            try:
                ad_id = int(raw[0])
                timestamp = int(raw[1])
                user_id = int(raw[2])
                username = str(raw[3])
                offer_obj = raw[4] or {}
                request_obj = raw[5] or {}

                offer_items = [int(x) for x in offer_obj.get("items", [])]
                request_items = [int(x) for x in request_obj.get("items", [])]
                request_tags = [int(x) for x in request_obj.get("tags", [])]
                offer_robux = int(offer_obj.get("robux", 0))
                request_robux = int(request_obj.get("robux", 0))

                ads.append(
                    {
                        "ad_id": ad_id,
                        "timestamp": timestamp,
                        "user_id": user_id,
                        "username": username,
                        "offer_items": offer_items,
                        "offer_robux": offer_robux,
                        "request_items": request_items,
                        "request_tags": request_tags,
                        "request_tag_names": [TAG_MAP.get(t, f"Tag#{t}") for t in request_tags],
                        "request_robux": request_robux,
                    }
                )
            except (IndexError, ValueError, TypeError):
                continue
        return ads

    def fetch_sale_activity(self) -> List[Dict[str, Any]]:
        try:
            data = self._get_json(self.SALE_ACTIVITY_URL, timeout=10)
            if not data.get("success"):
                return []
            out: List[Dict[str, Any]] = []
            for row in data.get("activities", []):
                if len(row) >= 5:
                    out.append(
                        {
                            "activity_id": int(row[4]),
                            "timestamp": int(row[0]),
                            "item_id": int(row[1]),
                            "old_rap": int(row[2]),
                            "new_rap": int(row[3]),
                        }
                    )
            return out
        except Exception:
            return []

    def fetch_player_assets(self, user_id: int) -> Optional[Dict[int, int]]:
        url = self.PLAYER_ASSETS_URL.format(user_id=user_id)
        try:
            data = self._get_json(url, timeout=12)
            if not data.get("success") or data.get("playerPrivacyEnabled") or data.get("playerTerminated"):
                return None
            player_assets = data.get("playerAssets") or {}
            uaid_to_item: Dict[int, int] = {}
            for item_id_str, uaid_list in player_assets.items():
                iid = int(item_id_str)
                for uaid in uaid_list:
                    uaid_to_item[int(uaid)] = iid
            return uaid_to_item
        except Exception:
            return None
