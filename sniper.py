import json
import time
from typing import Any, Dict, List, Optional
from data.rolimons import TAG_MAP

VALUE_BUCKETS = [
    ("1k-10k", 1_000, 10_000),
    ("10k-50k", 10_000, 50_000),
    ("50k-100k", 50_000, 100_000),
    ("100k-500k", 100_000, 500_000),
    ("500k+", 500_000, float("inf")),
]


def assign_value_bucket(value: int) -> str:
    for label, low, high in VALUE_BUCKETS:
        if low <= value < high:
            return label
    return "Under 1k"


def make_ad_signature(ad: Dict[str, Any]) -> str:
    offer_sorted = sorted(ad.get("offer_items", []))
    req_sorted = sorted(ad.get("request_items", []))
    tags_sorted = sorted(ad.get("request_tags", []))
    return (
        f"u:{ad['user_id']}|o:{','.join(map(str, offer_sorted))}"
        f"|r:{','.join(map(str, req_sorted))}|t:{','.join(map(str, tags_sorted))}"
    )


class AdSniper:
    def __init__(
        self,
        min_deal_percent: float = 5.0,
        cooldown_seconds: int = 1800,
        filter_projecteds: bool = True,
        filter_tag_only_requests: bool = True,
    ):
        self.min_deal_percent = min_deal_percent
        self.cooldown_seconds = cooldown_seconds
        self.filter_projecteds = filter_projecteds
        self.filter_tag_only_requests = filter_tag_only_requests
        # {user_id: (signature, last_seen_ts)}
        self._seen_signatures: Dict[int, tuple[str, int]] = {}
        self._seen_ad_ids: set[int] = set()

    def evaluate_ad(
        self,
        ad: Dict[str, Any],
        items_catalog: Dict[int, Dict[str, Any]],
        now_ts: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        now = now_ts if now_ts is not None else int(time.time())
        offer_ids = ad.get("offer_items", [])
        request_ids = ad.get("request_items", [])
        request_tags = ad.get("request_tags", [])

        if not offer_ids:
            return None

        # Filter out ads only including tags on request side
        if self.filter_tag_only_requests and not request_ids:
            return None

        # Filter out if request side asks explicitly for Projecteds tag (9)
        if self.filter_projecteds and 9 in request_tags:
            return None

        offer_details = []
        for iid in offer_ids:
            item = items_catalog.get(iid)
            if not item:
                return None
            if self.filter_projecteds and item.get("projected"):
                return None
            offer_details.append(item)

        request_details = []
        for iid in request_ids:
            item = items_catalog.get(iid)
            if not item:
                return None
            request_details.append(item)

        offer_val = sum(x["value"] for x in offer_details) + int(ad.get("offer_robux", 0))
        offer_rap = sum(x["rap"] for x in offer_details) + int(ad.get("offer_robux", 0))
        req_val = sum(x["value"] for x in request_details) + int(ad.get("request_robux", 0))
        req_rap = sum(x["rap"] for x in request_details) + int(ad.get("request_robux", 0))

        if req_val <= 0:
            return None

        value_overpay = offer_val - req_val
        rap_overpay = offer_rap - req_rap
        deal_percent = round((value_overpay / req_val) * 100.0, 1)

        if deal_percent < self.min_deal_percent:
            return None

        # Deduplication by poster + signature + cooldown
        ad_id = int(ad["ad_id"])
        if ad_id in self._seen_ad_ids:
            return None

        sig = make_ad_signature(ad)
        user_id = int(ad["user_id"])
        prev = self._seen_signatures.get(user_id)
        if prev is not None:
            prev_sig, prev_ts = prev
            if prev_sig == sig and (now - prev_ts) < self.cooldown_seconds:
                return None

        self._seen_ad_ids.add(ad_id)
        self._seen_signatures[user_id] = (sig, now)

        bucket = assign_value_bucket(offer_val)
        tag_names = [TAG_MAP.get(t, f"Tag#{t}") for t in request_tags]

        note = (
            "High OP detected - double-check before sending."
            if deal_percent >= 20.0
            else "Solid value edge detected."
        )

        return {
            "ad_id": ad_id,
            "timestamp": ad.get("timestamp", now),
            "user_id": user_id,
            "username": ad.get("username", f"User_{user_id}"),
            "offer_items": offer_details,
            "request_items": request_details,
            "request_tags": tag_names,
            "offer_value": offer_val,
            "offer_rap": offer_rap,
            "request_value": req_val,
            "request_rap": req_rap,
            "value_overpay": value_overpay,
            "rap_overpay": rap_overpay,
            "deal_percent": deal_percent,
            "bucket": bucket,
            "note": note,
            "signature": sig,
            "links": {
                "rolimons_profile": f"https://www.rolimons.com/player/{user_id}",
                "recent_trade_ads": f"https://www.rolimons.com/player/{user_id}#trade-ads",
                "send_trade": f"https://www.roblox.com/users/{user_id}/trade",
            },
        }
