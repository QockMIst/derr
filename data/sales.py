from datetime import datetime, timedelta, timezone
import json
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional
from db import Database

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


class SalesClient:
    CLASSIC_RESALE_URL = "https://economy.roblox.com/v1/assets/{item_id}/resale-data"
    COLLECTIBLE_RESALE_URL = (
        "https://apis.roblox.com/marketplace-sales/v1/item/{collectible_id}/resale-data"
    )
    COLLECTIBLE_RESELLERS_URL = (
        "https://apis.roblox.com/marketplace-sales/v1/item/{collectible_id}/resellers?limit=1"
    )
    CATALOG_SEARCH_V2_URL = (
        "https://catalog.roblox.com/v2/search/items/details"
        "?CreatorTargetId=1&CreatorType=User&Limit=10&IncludeNotForSale=true&Keyword={keyword}"
    )
    ECONOMY_DETAILS_URL = "https://economy.roblox.com/v2/assets/{item_id}/details"

    def _get_json(self, url: str, timeout: int = 10) -> Optional[Dict[str, Any]]:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None

    def _resolve_collectible_info(self, item_id: int, item_name: str) -> tuple[str, int]:
        det = self._get_json(self.ECONOMY_DETAILS_URL.format(item_id=item_id), timeout=8)
        cid = str((det or {}).get("CollectibleItemId") or "")
        lowest = 0
        if cid:
            resellers = self._get_json(
                self.COLLECTIBLE_RESELLERS_URL.format(collectible_id=cid), timeout=8
            )
            if resellers and resellers.get("data"):
                lowest = int(resellers["data"][0].get("price") or 0)
            return cid, lowest

        if item_name:
            cat = self._get_json(
                self.CATALOG_SEARCH_V2_URL.format(keyword=urllib.parse.quote(item_name)),
                timeout=8,
            )
            if cat and cat.get("data"):
                for entry in cat["data"]:
                    if int(entry.get("id", 0)) == int(item_id):
                        cid = str(entry.get("collectibleItemId") or "")
                        lowest = int(
                            entry.get("lowestResalePrice") or entry.get("lowestPrice") or 0
                        )
                        return cid, lowest
        return "", 0

    def fetch_item_sales_summary(
        self,
        db: Database,
        item_id: int,
        item_meta: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        meta = item_meta or {}
        rap_baseline = int(meta.get("rap", 0))

        if not force:
            cached = db.get_sales_cache(item_id, max_age_seconds=1800)
            if (
                cached
                and cached.get("recent_points")
                and int(cached.get("lowest_resale", 0)) > 0
                and (rap_baseline <= 0 or cached["avg_price_7d"] >= rap_baseline * 0.08)
            ):
                cached["live_sales"] = db.get_live_sales_for_item(item_id, limit=5)
                return cached

        cid = str(meta.get("collectible_item_id") or "")
        lowest_resale = int(meta.get("lowest_resale") or 0)

        resale_data: Optional[Dict[str, Any]] = None

        if not cid and int(meta.get("item_type", 1)) == 1:
            resale_data = self._get_json(self.CLASSIC_RESALE_URL.format(item_id=item_id))

        if not resale_data or not resale_data.get("priceDataPoints"):
            if not cid:
                cid, lowest_resale = self._resolve_collectible_info(
                    item_id, str(meta.get("name", ""))
                )
            if cid:
                resale_data = self._get_json(
                    self.COLLECTIBLE_RESALE_URL.format(collectible_id=cid)
                )

        if lowest_resale <= 0:
            if not cid:
                cid, lowest_resale = self._resolve_collectible_info(
                    item_id, str(meta.get("name", ""))
                )
            if lowest_resale <= 0 and cid:
                resellers = self._get_json(
                    self.COLLECTIBLE_RESELLERS_URL.format(collectible_id=cid), timeout=6
                )
                if resellers and resellers.get("data"):
                    lowest_resale = int(resellers["data"][0].get("price") or 0)

        price_pts = (resale_data or {}).get("priceDataPoints") or []
        vol_pts = (resale_data or {}).get("volumeDataPoints") or []

        vol_by_date = {str(v.get("date", ""))[:10]: int(v.get("value", 1)) for v in vol_pts if v.get("date")}

        min_valid_price = max(1, int(rap_baseline * 0.08))
        all_points: List[Dict[str, Any]] = []
        for pt in price_pts:
            d_str = str(pt.get("date", ""))[:10]
            p_val = int(round(float(pt.get("value", 0))))
            v_val = vol_by_date.get(d_str, 1)
            if p_val >= min_valid_price and d_str:
                all_points.append({"date": d_str, "price": p_val, "volume": v_val})

        all_points.sort(key=lambda x: x["date"], reverse=True)
        recent_points = all_points[:30]

        now_dt = datetime.now(timezone.utc)
        cutoff_30d = (now_dt - timedelta(days=30)).strftime("%Y-%m-%d")
        cutoff_90d = (now_dt - timedelta(days=90)).strftime("%Y-%m-%d")

        pts_last_30d = [x for x in recent_points if x["date"] >= cutoff_30d]
        pts_last_90d = [x for x in recent_points if x["date"] >= cutoff_90d]

        if pts_last_30d:
            pts_7 = pts_last_30d[:7]
            vol_7 = sum(x["volume"] for x in pts_7)
            avg_7d = int(round(sum(x["price"] * x["volume"] for x in pts_7) / max(1, vol_7)))
            vol_30 = sum(x["volume"] for x in pts_last_30d)
            avg_30d = int(round(sum(x["price"] * x["volume"] for x in pts_last_30d) / max(1, vol_30)))
            daily_vol = round(vol_30 / 30.0, 2)
        elif pts_last_90d:
            vol_90 = sum(x["volume"] for x in pts_last_90d)
            avg_7d = int(round(sum(x["price"] * x["volume"] for x in pts_last_90d) / max(1, vol_90)))
            avg_30d = avg_7d
            daily_vol = round(vol_90 / 90.0, 2)
        elif recent_points:
            latest_window = recent_points[:5]
            vol_w = sum(x["volume"] for x in latest_window)
            avg_7d = int(round(sum(x["price"] * x["volume"] for x in latest_window) / max(1, vol_w)))
            avg_30d = avg_7d
            daily_vol = 0.0
        else:
            avg_7d = rap_baseline
            avg_30d = rap_baseline
            daily_vol = 0.0

        db.upsert_sales_cache(
            item_id=item_id,
            avg_price_7d=avg_7d,
            avg_price_30d=avg_30d,
            daily_volume=daily_vol,
            lowest_resale=lowest_resale,
            recent_points=recent_points[:15],
        )

        return {
            "item_id": item_id,
            "avg_price_7d": avg_7d,
            "avg_price_30d": avg_30d,
            "daily_volume": daily_vol,
            "lowest_resale": lowest_resale,
            "recent_points": recent_points[:15],
            "live_sales": db.get_live_sales_for_item(item_id, limit=5),
        }
