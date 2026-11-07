from itertools import combinations
from typing import Any, Dict, List, Optional
from data.roblox import RobloxClient
from db import Database
from engine import TradeEngine
from proofs import find_item_by_query, format_k


def _resolve_filter_ids(
    raw_csv: str, items_catalog: Dict[int, Dict[str, Any]]
) -> List[int]:
    out: List[int] = []
    if not raw_csv or not raw_csv.strip():
        return out
    for part in raw_csv.split(","):
        q = part.strip()
        if not q:
            continue
        if q.isdigit() and int(q) in items_catalog:
            out.append(int(q))
            continue
        found = find_item_by_query(q, items_catalog)
        if found:
            out.append(int(found["item_id"]))
    return out


def generate_trade_suggestions(
    username: str,
    roblox: RobloxClient,
    db: Database,
    trade_engine: TradeEngine,
    items_catalog: Dict[int, Dict[str, Any]],
    recent_ads: List[Dict[str, Any]],
    exclude_csv: str = "",
    include_csv: str = "",
    trade_type: str = "any",
    target_user: str = "",
    model: str = "star",
    max_options: int = 3,
) -> Dict[str, Any]:
    user_info = roblox.resolve_user(username)
    if not user_info:
        return {"error": f"Could not find Roblox user `{username}`."}

    uid = int(user_info["user_id"])
    uname = str(user_info["username"])
    inv_map = roblox.fetch_collectibles(uid)
    if inv_map is None:
        return {"error": f"`{uname}`'s inventory is private or temporarily rate-limited by Roblox."}
    if not inv_map:
        return {"error": f"`{uname}` has 0 tradable limiteds in their inventory."}

    exclude_set = set(_resolve_filter_ids(exclude_csv, items_catalog))
    include_list = _resolve_filter_ids(include_csv, items_catalog)
    include_set = set(include_list)

    user_items: List[Dict[str, Any]] = []
    seen_item_counts: Dict[int, int] = {}
    for _uaid, iid in inv_map.items():
        if iid in exclude_set:
            continue
        meta = items_catalog.get(iid)
        if not meta or int(meta.get("value", 0)) <= 0:
            continue
        if meta.get("projected") and iid not in include_set:
            continue
        cnt = seen_item_counts.get(iid, 0)
        if cnt >= 2:
            continue
        seen_item_counts[iid] = cnt + 1
        user_items.append(meta)

    if not user_items:
        return {"error": f"No eligible items left in `{uname}`'s inventory after applying filters."}

    user_items.sort(key=lambda x: (-int(x.get("value", 0)), -int(x.get("demand", -1))))
    top_pool = user_items[:16]
    for req_id in include_set:
        if all(x["item_id"] != req_id for x in top_pool) and req_id in items_catalog:
            top_pool.insert(0, items_catalog[req_id])
    top_pool = top_pool[:16]

    mode_filter = trade_type.strip().lower()
    give_packages: List[List[int]] = []
    seen_pkg_keys = set()

    for r in (1, 2, 3, 4):
        if mode_filter == "downgrade" and r > 2:
            continue
        if mode_filter == "upgrade" and r == 1 and len(top_pool) >= 2:
            pass
        for combo in combinations(top_pool, r):
            ids = [int(x["item_id"]) for x in combo]
            if include_set and not include_set.issubset(set(ids)):
                continue
            key = tuple(sorted(ids))
            if key in seen_pkg_keys:
                continue
            seen_pkg_keys.add(key)
            give_packages.append(ids)
            if len(give_packages) >= 85:
                break
        if len(give_packages) >= 85:
            break

    if not give_packages:
        return {"error": f"Could not build any valid offer packages containing `{include_csv}` from `{uname}`'s inventory."}

    target_info = None
    target_inv_items: List[Dict[str, Any]] = []
    if target_user and target_user.strip():
        target_info = roblox.resolve_user(target_user.strip())
        if not target_info:
            return {"error": f"Could not find target Roblox user `{target_user}`."}
        t_inv = roblox.fetch_collectibles(int(target_info["user_id"]))
        if t_inv:
            t_seen: Dict[int, int] = {}
            for _u, tiid in t_inv.items():
                m = items_catalog.get(tiid)
                if not m or m.get("projected") or int(m.get("value", 0)) <= 0:
                    continue
                if t_seen.get(tiid, 0) < 2:
                    t_seen[tiid] = t_seen.get(tiid, 0) + 1
                    target_inv_items.append(m)
            target_inv_items.sort(key=lambda x: (-int(x.get("value", 0)), -int(x.get("demand", -1))))

    candidate_offers: List[Dict[str, Any]] = []
    if target_info:
        t_uid = int(target_info["user_id"])
        t_uname = str(target_info["username"])
        for ad in recent_ads:
            if int(ad.get("user_id", 0)) == t_uid or str(ad.get("username", "")).lower() == t_uname.lower():
                off = [int(x) for x in ad.get("offer_items", []) if int(x) in items_catalog]
                if off:
                    candidate_offers.append(
                        {
                            "poster_id": t_uid,
                            "poster_name": t_uname,
                            "receive_ids": off[:4],
                            "requested_ids": [int(x) for x in ad.get("request_items", [])],
                            "source": "Target's Trade Ad",
                        }
                    )
        for r in (1, 2, 3):
            for combo in combinations(target_inv_items[:12], r):
                candidate_offers.append(
                    {
                        "poster_id": t_uid,
                        "poster_name": t_uname,
                        "receive_ids": [int(x["item_id"]) for x in combo],
                        "requested_ids": [],
                        "source": f"{t_uname}'s Inventory",
                    }
                )
    else:
        seen_ad_sigs = set()
        for ad in recent_ads:
            p_uid = int(ad.get("user_id", 0))
            if p_uid == uid:
                continue
            off = [int(x) for x in ad.get("offer_items", []) if int(x) in items_catalog]
            if not off:
                continue
            if any(items_catalog[x].get("projected") for x in off):
                continue
            sig = (p_uid, tuple(sorted(off)))
            if sig in seen_ad_sigs:
                continue
            seen_ad_sigs.add(sig)
            candidate_offers.append(
                {
                    "poster_id": p_uid,
                    "poster_name": str(ad.get("username", f"User_{p_uid}")),
                    "receive_ids": off[:4],
                    "requested_ids": [int(x) for x in ad.get("request_items", [])],
                    "source": "Live Rolimons Ad",
                }
            )

    scored_trades: List[Dict[str, Any]] = []
    fast_eval_cache: Dict[int, Dict[str, Any]] = {}

    def get_eval(iid: int) -> Dict[str, Any]:
        if iid not in fast_eval_cache:
            fast_eval_cache[iid] = trade_engine.evaluate_single_item(
                items_catalog[iid], recent_ads, items_catalog, model_key=model
            )
        return fast_eval_cache[iid]

    for g_ids in give_packages:
        g_val = sum(int(items_catalog[x]["value"]) for x in g_ids)
        if g_val <= 0:
            continue
        max_g = max(int(items_catalog[x]["value"]) for x in g_ids)
        g_set = set(g_ids)

        for cand in candidate_offers:
            r_ids = cand["receive_ids"]
            if g_set & set(r_ids):
                continue
            r_val = sum(int(items_catalog[x]["value"]) for x in r_ids)
            if r_val <= 0:
                continue

            ratio = r_val / float(g_val)
            if not (0.86 <= ratio <= 1.24):
                continue

            max_r = max(int(items_catalog[x]["value"]) for x in r_ids)
            is_upg = len(g_ids) > len(r_ids) and max_r > max_g
            is_dng = len(r_ids) > len(g_ids) and max_g > max_r

            if mode_filter == "upgrade" and not is_upg:
                continue
            if mode_filter == "downgrade" and not is_dng:
                continue

            if any(int(items_catalog[x].get("trend", -1)) == 0 for x in r_ids):
                continue

            g_mkt = sum(get_eval(x)["market_value"] for x in g_ids)
            r_mkt = sum(get_eval(x)["market_value"] for x in r_ids)
            avg_r_liq = sum(get_eval(x)["liquidity"] for x in r_ids) / max(1, len(r_ids))

            struct = 0.0
            if is_upg:
                struct = r_val * (0.03 + 0.04 * avg_r_liq)
            elif is_dng:
                avg_g_liq = sum(get_eval(x)["liquidity"] for x in g_ids) / max(1, len(g_ids))
                struct = -g_val * (0.04 + 0.035 * avg_g_liq)

            edge = (r_mkt - g_mkt) + struct
            if edge <= 0:
                continue

            req_overlap = len(g_set & set(cand.get("requested_ids") or []))
            accept_prob = 1.0
            if ratio > 1.12 and req_overlap == 0:
                accept_prob = 0.45
            elif req_overlap > 0:
                accept_prob = 1.35

            score = (edge / max(1.0, float(g_val))) * 100.0 * accept_prob * (0.65 + 0.35 * avg_r_liq)
            scored_trades.append(
                {
                    "score": score,
                    "give_ids": g_ids,
                    "receive_ids": r_ids,
                    "poster_id": cand["poster_id"],
                    "poster_name": cand["poster_name"],
                    "source": cand["source"],
                    "is_upgrade": is_upg,
                    "is_downgrade": is_dng,
                }
            )

    scored_trades.sort(key=lambda x: x["score"], reverse=True)

    selected: List[Dict[str, Any]] = []
    used_posters = set()
    used_recv_sets = set()

    for st in scored_trades:
        r_key = tuple(sorted(st["receive_ids"]))
        if r_key in used_recv_sets:
            continue
        if not target_info and st["poster_id"] in used_posters and len(scored_trades) > max_options * 2:
            continue

        analysis = trade_engine.analyze_trade(
            st["give_ids"],
            st["receive_ids"],
            items_catalog,
            engine_mode=model,
            recent_ads=recent_ads,
        )
        if analysis["color"] == "red":
            continue

        used_recv_sets.add(r_key)
        used_posters.add(st["poster_id"])
        trade_kind = "UPGRADE" if st["is_upgrade"] else ("DOWNGRADE" if st["is_downgrade"] else "SWAP")
        selected.append(
            {
                "index": len(selected) + 1,
                "trade_kind": trade_kind,
                "poster_id": st["poster_id"],
                "poster_name": st["poster_name"],
                "source": st["source"],
                "send_trade_url": f"https://www.roblox.com/users/{st['poster_id']}/trade",
                "rolimons_url": f"https://www.rolimons.com/player/{st['poster_id']}",
                "analysis": analysis,
            }
        )
        if len(selected) >= max_options:
            break

    return {
        "user_id": uid,
        "username": uname,
        "inventory_count": len(user_items),
        "options": selected,
    }
