import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple
from db import Database


def match_moves_into_trades(
    db: Database,
    items_catalog: Dict[int, Dict[str, Any]],
    match_window_seconds: int = 7200,
    now_ts: Optional[int] = None,
    predict_op_fn: Optional[Callable[[List[int], List[int]], Optional[int]]] = None,
) -> List[int]:
    now = now_ts if now_ts is not None else int(time.time())
    since = now - (match_window_seconds * 4)
    moves = db.get_unmatched_moves(since)
    if not moves:
        return []

    directed: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
    for m in moves:
        u_from = int(m["from_user_id"])
        u_to = int(m["to_user_id"])
        if u_from != u_to:
            directed[(u_from, u_to)].append(m)

    visited_pairs = set()
    created_trade_ids: List[int] = []

    for (u_a, u_b), a_to_b_moves in list(directed.items()):
        pair_key = (min(u_a, u_b), max(u_a, u_b))
        if pair_key in visited_pairs:
            continue
        b_to_a_moves = directed.get((u_b, u_a), [])
        if not a_to_b_moves or not b_to_a_moves:
            continue

        visited_pairs.add(pair_key)

        uaids_a = {int(m["uaid"]) for m in a_to_b_moves}
        uaids_b = {int(m["uaid"]) for m in b_to_a_moves}
        overlap_uaids = uaids_a & uaids_b

        clean_a = [m for m in a_to_b_moves if int(m["uaid"]) not in overlap_uaids]
        clean_b = [m for m in b_to_a_moves if int(m["uaid"]) not in overlap_uaids]
        if not clean_a or not clean_b:
            continue

        seen_uaids_a = set()
        dedup_a = []
        for m in clean_a:
            u = int(m["uaid"])
            if u not in seen_uaids_a:
                seen_uaids_a.add(u)
                dedup_a.append(m)

        seen_uaids_b = set()
        dedup_b = []
        for m in clean_b:
            u = int(m["uaid"])
            if u not in seen_uaids_b:
                seen_uaids_b.add(u)
                dedup_b.append(m)

        ts_a = max(m["detected_at"] for m in dedup_a)
        ts_b = max(m["detected_at"] for m in dedup_b)
        if abs(ts_a - ts_b) > match_window_seconds:
            continue

        side_a_moves = dedup_a[:4]
        side_b_moves = dedup_b[:4]

        def build_side_items(move_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            out = []
            for mv in move_list:
                iid = int(mv["item_id"])
                meta = items_catalog.get(iid, {})
                out.append(
                    {
                        "item_id": iid,
                        "uaid": int(mv["uaid"]),
                        "name": meta.get("name", f"Item #{iid}"),
                        "acronym": meta.get("acronym", ""),
                        "value": int(meta.get("value", 0)),
                        "rap": int(meta.get("rap", 0)),
                        "thumbnail_url": meta.get("thumbnail_url", ""),
                    }
                )
            return out

        items_a = build_side_items(side_a_moves)
        items_b = build_side_items(side_b_moves)

        if {x["item_id"] for x in items_a} == {x["item_id"] for x in items_b} and len(items_a) == len(items_b):
            continue

        uaids_in_a = {x.get("uaid") for x in items_a if x.get("uaid")}
        uaids_in_b = {x.get("uaid") for x in items_b if x.get("uaid")}
        if uaids_in_a & uaids_in_b:
            continue

        val_a = sum(x["value"] for x in items_a)
        val_b = sum(x["value"] for x in items_b)
        if val_a <= 0 or val_b <= 0:
            continue
        ratio = max(val_a / val_b, val_b / val_a)
        if ratio > 2.0:
            continue

        rap_a = sum(x["rap"] for x in items_a)
        rap_b = sum(x["rap"] for x in items_b)

        op_val = abs(val_a - val_b)
        op_rap = abs(rap_a - rap_b)

        trade_type = "SWAP"
        if len(items_a) > len(items_b):
            trade_type = "UPGRADE" if val_b >= val_a else "SWAP"
        elif len(items_b) > len(items_a):
            trade_type = "DOWNGRADE" if val_a >= val_b else "SWAP"

        predicted_op = None
        if predict_op_fn is not None:
            try:
                predicted_op = predict_op_fn(
                    [x["item_id"] for x in items_a],
                    [x["item_id"] for x in items_b],
                )
            except Exception:
                predicted_op = None

        move_ids = [int(m["id"]) for m in a_to_b_moves + b_to_a_moves if "id" in m]
        detected_ts = max(ts_a, ts_b)

        tid = db.insert_trade(
            user_a_id=u_a,
            user_a_name=db.get_trader_name(u_a),
            user_b_id=u_b,
            user_b_name=db.get_trader_name(u_b),
            items_a=items_a,
            items_b=items_b,
            value_a=val_a,
            rap_a=rap_a,
            value_b=val_b,
            rap_b=rap_b,
            op_value=op_val,
            op_rap=op_rap,
            trade_type=trade_type,
            detected_at=detected_ts,
            move_ids=move_ids,
            predicted_op=predicted_op,
        )
        created_trade_ids.append(tid)

    return created_trade_ids
