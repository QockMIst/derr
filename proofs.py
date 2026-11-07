import time
from typing import Any, Dict, List, Optional
from data.sales import SalesClient
from db import Database


def format_k(val: float) -> str:
    sign = "-" if val < 0 else ""
    v = abs(val)
    if v >= 1_000_000:
        return f"{sign}{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{sign}{v / 1_000:.1f}k"
    return f"{sign}{int(round(v))}"


def format_relative_time(ts: int, now_ts: Optional[int] = None) -> str:
    now = now_ts if now_ts is not None else int(time.time())
    diff = max(0, now - ts)
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    days = diff // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


def find_item_by_query(
    query: str, items_catalog: Dict[int, Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    q = query.strip().lower()
    if not q:
        return None
    if q.isdigit() and int(q) in items_catalog:
        return items_catalog[int(q)]

    for it in items_catalog.values():
        if it.get("acronym", "").lower() == q:
            return it

    for it in items_catalog.values():
        if it["name"].lower() == q:
            return it

    matches = [
        it
        for it in items_catalog.values()
        if q in it["name"].lower() or (it.get("acronym") and q in it["acronym"].lower())
    ]
    if matches:
        matches.sort(key=lambda x: (len(x["name"]), -x["value"]))
        return matches[0]
    return None


def get_item_proofs_formatted(
    db: Database,
    item_id: int,
    items_catalog: Dict[int, Dict[str, Any]],
    limit: int = 3,
    mode: str = "any",
    sales_client: Optional[SalesClient] = None,
) -> Dict[str, Any]:
    item_meta = items_catalog.get(
        item_id, {"item_id": item_id, "name": f"Item #{item_id}", "value": 0, "rap": 0}
    )
    raw_proofs = db.get_proofs_for_item(item_id, limit=limit * 3)

    if mode != "any":
        filtered_by_mode = []
        target_m = mode.lower()
        for p in raw_proofs:
            side = p.get("side", "A")
            cnt_a = len(p.get("items_a") or [])
            cnt_b = len(p.get("items_b") or [])
            item_side_cnt = cnt_a if side == "A" else cnt_b
            other_side_cnt = cnt_b if side == "A" else cnt_a
            if target_m == "downgrade":
                if other_side_cnt > item_side_cnt or p.get("trade_type", "").upper() == "DOWNGRADE":
                    filtered_by_mode.append(p)
            elif target_m == "upgrade":
                if item_side_cnt > other_side_cnt or p.get("trade_type", "").upper() == "UPGRADE":
                    filtered_by_mode.append(p)
            else:
                filtered_by_mode.append(p)
        raw_proofs = filtered_by_mode

    selected = raw_proofs[:limit]
    total = len(selected)
    formatted_cards = []

    for idx, p in enumerate(selected, start=1):
        op_val = int(p.get("op_value", 0))
        formatted_cards.append(
            {
                "index": idx,
                "total": total,
                "trade_id": p["id"],
                "title": f"Proof {idx}/{total} for {item_meta['name']} (mode {mode})",
                "age": format_relative_time(p["detected_at"]),
                "detected_at": p["detected_at"],
                "trade_type": p.get("trade_type", "SWAP"),
                "op_value": op_val,
                "op_rap": p["op_rap"],
                "realized_op": int(p.get("realized_op", op_val)),
                "op_label": f"OP {format_k(op_val)}",
                "user_a_id": p["user_a_id"],
                "user_a_name": p["user_a_name"],
                "user_b_id": p["user_b_id"],
                "user_b_name": p["user_b_name"],
                "items_a": p["items_a"],
                "items_b": p["items_b"],
                "value_a": p["value_a"],
                "rap_a": p["rap_a"],
                "value_b": p["value_b"],
                "rap_b": p["rap_b"],
                "predicted_op": p.get("predicted_op"),
                "warning": "ROBUX/USD CANT BE DETECTED",
            }
        )

    sc = sales_client or SalesClient()
    sales_summary = {"avg_price_7d": 0, "avg_price_30d": 0, "daily_volume": 0.0, "lowest_resale": 0, "recent_points": [], "live_sales": []}
    if item_id > 0:
        try:
            sales_summary = sc.fetch_item_sales_summary(db, item_id, item_meta)
        except Exception:
            pass

    return {
        "item": item_meta,
        "count": total,
        "proofs": formatted_cards,
        "sales": sales_summary,
    }
