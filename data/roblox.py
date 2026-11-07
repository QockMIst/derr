import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


class RobloxClient:
    COLLECTIBLES_URL = (
        "https://inventory.roblox.com/v1/users/{user_id}/assets/collectibles?limit=100&sortOrder=Asc"
    )
    THUMBNAILS_URL = (
        "https://thumbnails.roblox.com/v1/assets?assetIds={asset_ids}&size=150x150&format=Png&isCircular=false"
    )
    USERNAMES_URL = "https://users.roblox.com/v1/usernames/users"
    USER_INFO_URL = "https://users.roblox.com/v1/users/{user_id}"

    def __init__(self, backoff_seconds: float = 5.0):
        self.backoff_seconds = backoff_seconds
        self._rate_limited_until: float = 0.0

    def resolve_user(self, query: str) -> Optional[Dict[str, Any]]:
        q = str(query).strip()
        if not q:
            return None
        if q.isdigit():
            uid = int(q)
            try:
                req = urllib.request.Request(self.USER_INFO_URL.format(user_id=uid), headers=HEADERS)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    d = json.loads(resp.read().decode("utf-8", errors="replace"))
                    return {"user_id": int(d.get("id", uid)), "username": str(d.get("name", f"User_{uid}"))}
            except Exception:
                return {"user_id": uid, "username": f"User_{uid}"}
        try:
            body = json.dumps({"usernames": [q], "excludeBannedUsers": False}).encode("utf-8")
            hdrs = dict(HEADERS)
            hdrs["Content-Type"] = "application/json"
            req = urllib.request.Request(self.USERNAMES_URL, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                rows = payload.get("data") or []
                if rows:
                    return {
                        "user_id": int(rows[0]["id"]),
                        "username": str(rows[0].get("name") or q),
                    }
        except Exception:
            return None
        return None

    def fetch_collectibles(
        self, user_id: int, rap_out: Optional[Dict[int, int]] = None
    ) -> Optional[Dict[int, int]]:
        if time.time() < self._rate_limited_until:
            return None

        uaid_to_item: Dict[int, int] = {}
        cursor: Optional[str] = None
        pages = 0

        while pages < 5:
            pages += 1
            url = self.COLLECTIBLES_URL.format(user_id=user_id)
            if cursor:
                url += f"&cursor={cursor}"
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=12) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                for entry in payload.get("data", []):
                    uaid = entry.get("userAssetId")
                    asset_id = entry.get("assetId")
                    rap = entry.get("recentAveragePrice")
                    if uaid and asset_id:
                        uaid_to_item[int(uaid)] = int(asset_id)
                        if rap_out is not None and rap and int(rap) > 0:
                            rap_out[int(asset_id)] = int(rap)
                cursor = payload.get("nextPageCursor")
                if not cursor:
                    break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self._rate_limited_until = time.time() + self.backoff_seconds
                return None
            except Exception:
                return None

        return uaid_to_item

    def fetch_thumbnails(self, item_ids: List[int]) -> Dict[int, str]:
        result: Dict[int, str] = {}
        unique_ids = [int(x) for x in set(item_ids) if x]
        for i in range(0, len(unique_ids), 50):
            batch = unique_ids[i : i + 50]
            ids_str = ",".join(str(x) for x in batch)
            url = self.THUMBNAILS_URL.format(asset_ids=ids_str)
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=12) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                for item in payload.get("data", []):
                    tid = item.get("targetId")
                    img = item.get("imageUrl")
                    if tid and img:
                        result[int(tid)] = str(img)
            except Exception:
                continue
        return result

    def fetch_user_headshots(self, user_ids: List[int]) -> Dict[int, str]:
        result: Dict[int, str] = {}
        unique_ids = [int(x) for x in set(user_ids) if x]
        for i in range(0, len(unique_ids), 50):
            batch = unique_ids[i : i + 50]
            ids_str = ",".join(str(x) for x in batch)
            url = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={ids_str}&size=150x150&format=Png&isCircular=false"
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=12) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                for item in payload.get("data", []):
                    tid = item.get("targetId")
                    img = item.get("imageUrl")
                    if tid and img:
                        result[int(tid)] = str(img)
            except Exception:
                continue
        return result
