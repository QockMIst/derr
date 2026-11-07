from typing import Any, Dict, List
from db import Database


def seed_traders_from_ads(db: Database, ads: List[Dict[str, Any]]) -> int:
    seen = set()
    for ad in ads:
        uid = int(ad["user_id"])
        uname = str(ad.get("username", ""))
        ts = int(ad.get("timestamp", 0))
        db.upsert_trader(uid, uname, ts)
        seen.add(uid)
    return len(seen)
