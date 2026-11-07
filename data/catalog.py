import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def make_acronym(name: str) -> str:
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", name) if w]
    if len(words) <= 1:
        return ""
    return "".join(w[0].upper() for w in words[:6])


class RobloxCatalogClient:
    SEARCH_V2_URL = (
        "https://catalog.roblox.com/v2/search/items/details"
        "?CreatorTargetId=1&CreatorType=User&SalesTypeFilter=2&Limit=30&IncludeNotForSale=true"
    )
    KEYWORD_V2_URL = (
        "https://catalog.roblox.com/v2/search/items/details"
        "?CreatorTargetId=1&CreatorType=User&Limit=30&IncludeNotForSale=true&Keyword={keyword}"
    )
    ECONOMY_DETAILS_URL = "https://economy.roblox.com/v2/assets/{asset_id}/details"
    COLLECTIBLE_RESALE_URL = (
        "https://apis.roblox.com/marketplace-sales/v1/item/{collectible_id}/resale-data"
    )

    def _get_json(self, url: str, timeout: int = 12) -> Optional[Dict[str, Any]]:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise
            return None
        except Exception:
            return None

    def fetch_collectible_rap(self, collectible_item_id: str) -> int:
        if not collectible_item_id:
            return 0
        url = self.COLLECTIBLE_RESALE_URL.format(collectible_id=collectible_item_id)
        try:
            data = self._get_json(url, timeout=10)
            if data and data.get("recentAveragePrice"):
                return int(round(float(data["recentAveragePrice"])))
        except Exception:
            pass
        return 0

    def parse_catalog_entry(self, entry: Dict[str, Any], fetch_rap: bool = False) -> Optional[Dict[str, Any]]:
        if int(entry.get("creatorTargetId", 0)) != 1:
            return None
        if entry.get("itemType") != "Asset":
            return None

        restrictions = set(entry.get("itemRestrictions") or [])
        collectible_id = entry.get("collectibleItemId")
        has_resellers = bool(entry.get("hasResellers"))
        if not (
            "Limited" in restrictions
            or "LimitedUnique" in restrictions
            or "Collectible" in restrictions
            or collectible_id
            or has_resellers
        ):
            return None

        item_id = int(entry["id"])
        name = str(entry.get("name") or f"Item #{item_id}")
        lowest_resale = int(entry.get("lowestResalePrice") or entry.get("lowestPrice") or 0)
        total_qty = int(entry.get("totalQuantity") or 0)

        rap = 0
        if fetch_rap and collectible_id:
            rap = self.fetch_collectible_rap(str(collectible_id))
        if rap <= 0:
            rap = lowest_resale

        return {
            "item_id": item_id,
            "name": name,
            "acronym": make_acronym(name),
            "rap": rap,
            "value": rap,
            "default_value": rap,
            "is_valued": False,
            "demand": -1,
            "demand_label": "Unassigned",
            "trend": -1,
            "trend_label": "Unassigned",
            "projected": False,
            "hyped": False,
            "rare": (0 < total_qty <= 50),
            "source": "roblox_only",
            "collectible_item_id": str(collectible_id or ""),
            "total_quantity": total_qty,
            "lowest_resale": lowest_resale,
        }

    def search_keyword(
        self, query: str, existing_catalog: Dict[int, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        q = query.strip()
        if not q:
            return []

        m = re.search(r"(?:rolimons\.com/item/|roblox\.com/catalog/)(\d+)", q)
        asset_id_int: Optional[int] = None
        if m:
            asset_id_int = int(m.group(1))
        elif q.isdigit():
            asset_id_int = int(q)

        if asset_id_int is not None:
            if asset_id_int in existing_catalog:
                return [existing_catalog[asset_id_int]]
            item = self.fetch_single_asset(asset_id_int)
            return [item] if item else []

        url = self.KEYWORD_V2_URL.format(keyword=urllib.parse.quote(q))
        try:
            data = self._get_json(url)
        except Exception:
            return []
        if not data:
            return []

        results: List[Dict[str, Any]] = []
        for raw in data.get("data", []):
            iid = int(raw.get("id", 0))
            if iid in existing_catalog:
                results.append(existing_catalog[iid])
                continue
            parsed = self.parse_catalog_entry(raw, fetch_rap=(len(results) < 5))
            if parsed:
                results.append(parsed)
        return results

    def fetch_single_asset(self, asset_id: int) -> Optional[Dict[str, Any]]:
        url = self.ECONOMY_DETAILS_URL.format(asset_id=asset_id)
        try:
            data = self._get_json(url)
        except Exception:
            return None
        if not data:
            return None

        creator = data.get("Creator") or {}
        if int(creator.get("CreatorTargetId") or creator.get("Id") or 0) != 1:
            return None

        is_limited = bool(
            data.get("IsLimited")
            or data.get("IsLimitedUnique")
            or data.get("CollectibleItemId")
            or data.get("ProductType") == "Collectible Item"
        )
        if not is_limited:
            return None

        cid = str(data.get("CollectibleItemId") or "")
        rap = self.fetch_collectible_rap(cid) if cid else 0
        name = str(data.get("Name") or f"Item #{asset_id}")
        return {
            "item_id": int(asset_id),
            "name": name,
            "acronym": make_acronym(name),
            "rap": rap,
            "value": rap,
            "default_value": rap,
            "is_valued": False,
            "demand": -1,
            "demand_label": "Unassigned",
            "trend": -1,
            "trend_label": "Unassigned",
            "projected": False,
            "hyped": False,
            "rare": False,
            "source": "roblox_only",
            "collectible_item_id": cid,
        }

    def crawl_all_roblox_limiteds(
        self,
        existing_catalog: Dict[int, Dict[str, Any]],
        on_batch_callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        max_pages: int = 120,
        page_delay: float = 1.6,
    ) -> List[Dict[str, Any]]:
        discovered: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        pages = 0

        while pages < max_pages:
            pages += 1
            url = self.SEARCH_V2_URL
            if cursor:
                url += f"&Cursor={urllib.parse.quote(cursor)}"
            try:
                data = self._get_json(url)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(8.0)
                    continue
                break
            except Exception:
                break

            if not data:
                break

            batch_new: List[Dict[str, Any]] = []
            for entry in data.get("data", []):
                iid = int(entry.get("id", 0))
                if iid in existing_catalog:
                    continue
                parsed = self.parse_catalog_entry(entry, fetch_rap=True)
                if parsed:
                    batch_new.append(parsed)
                    discovered.append(parsed)

            if batch_new and on_batch_callback:
                on_batch_callback(batch_new)

            cursor = data.get("nextPageCursor")
            if not cursor:
                break
            time.sleep(page_delay)

        return discovered
