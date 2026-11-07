import math
import time
from typing import Any, Dict, List, Optional, Tuple
from data.sales import SalesClient
from db import Database
from proofs import format_k

MODEL_META = {
    "star": ("Carbon Star 1.1", "Experimental Fully Dynamic"),
    "max": ("Carbon Max 5.1", "Strict High-Precision"),
    "hume": ("Carbon Hume 4.2", "Expert Dynamic"),
    "legacy": ("Carbon Legacy 2.1", "Classic Proof + Demand"),
    "v4": ("Carbon Hume 4.2", "Expert Dynamic"),
    "v3.1": ("Carbon Hume 4.2", "Expert Dynamic"),
    "v3": ("Carbon Legacy 2.1", "Classic Proof + Demand"),
}


def resolve_model_label(mode: str) -> tuple[str, str]:
    m = (mode or "star").strip().lower()
    if m in MODEL_META:
        return MODEL_META[m]
    if "star" in m:
        return MODEL_META["star"]
    if "max" in m or "5" in m:
        return MODEL_META["max"]
    if "legacy" in m or "2" in m:
        return MODEL_META["legacy"]
    return MODEL_META["hume"]


def normalize_model_key(mode: str) -> str:
    m = (mode or "star").strip().lower()
    if "max" in m or "5" in m:
        return "max"
    if "legacy" in m or "2" in m or m == "v3":
        return "legacy"
    if "hume" in m or "4" in m or m in ("v4", "v3.1"):
        return "hume"
    return "star"


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f))


def _trim_iqr(vals: List[float]) -> List[float]:
    if len(vals) < 4:
        return sorted(vals)
    s = sorted(vals)
    q1 = _percentile(s, 25)
    q3 = _percentile(s, 75)
    iqr = q3 - q1
    low = q1 - 1.5 * iqr
    high = q3 + 1.5 * iqr
    trimmed = [x for x in s if low <= x <= high]
    return trimmed if trimmed else s


def is_low_copy_or_semi(
    item: Dict[str, Any], tracked_copies: int = 999
) -> tuple[bool, str]:
    if item.get("projected"):
        return False, ""
    if item.get("rare"):
        return True, "Rare"
    iid = int(item.get("item_id", 0))
    val = int(item.get("value", 0))
    rap = int(item.get("rap", 0))
    demand = int(item.get("demand", -1))
    is_val = bool(item.get("is_valued", val != rap))

    if is_val and val >= 25000 and rap > 0 and (val / rap) >= 1.12 and demand <= 1:
        return True, "Semi-Rare"
    if not is_val and not item.get("projected") and rap >= 7500:
        if 0 < tracked_copies <= 6 or (iid < 65_000_000 and tracked_copies <= 10):
            return True, "Low-Copy Semi-Rare"
    return False, ""


def compute_ad_implied_ops(
    item_id: int,
    ads: List[Dict[str, Any]],
    items_catalog: Dict[int, Dict[str, Any]],
) -> List[float]:
    implied: List[float] = []
    item_meta = items_catalog.get(item_id)
    if not item_meta or item_meta["value"] <= 0:
        return implied

    for ad in ads:
        offer_ids = ad.get("offer_items", [])
        req_ids = ad.get("request_items", [])
        if not offer_ids or not req_ids:
            continue

        if any(items_catalog.get(x, {}).get("projected") for x in offer_ids + req_ids):
            continue

        offer_val = sum(items_catalog.get(x, {}).get("value", 0) for x in offer_ids)
        req_val = sum(items_catalog.get(x, {}).get("value", 0) for x in req_ids)
        if offer_val <= 0 or req_val <= 0:
            continue

        is_rare_or_unvalued = item_meta.get("rare") or not item_meta.get("is_valued", True)
        demand = int(item_meta.get("demand", -1))
        min_ratio = 0.98 if demand >= 3 else 0.78
        max_ratio = 8.5 if is_rare_or_unvalued else 1.55

        if req_ids == [item_id] and (min_ratio <= offer_val / req_val <= max_ratio):
            implied.append(float(offer_val - req_val))
        elif offer_ids == [item_id] and (min_ratio <= req_val / offer_val <= max_ratio):
            implied.append(float(req_val - offer_val))

    return implied


def estimate_dynamic_item_op_band(
    item: Dict[str, Any],
    proof_ops: List[float],
    ad_ops: List[float],
    hoard_stats: Dict[str, Any],
    lowest_resale: int = 0,
    model_key: str = "star",
) -> Dict[str, Any]:
    m_key = normalize_model_key(model_key)
    base_val = int(item.get("value", 0))
    rap = int(item.get("rap", 0))
    demand = int(item.get("demand", -1))
    trend = int(item.get("trend", -1))
    is_val = bool(item.get("is_valued", False))
    is_rare = bool(item.get("rare", False))
    is_proj = bool(item.get("projected", False))
    tracked_copies = int(hoard_stats.get("total_tracked", 0))
    hoard_pct = float(hoard_stats.get("hoard_percent", 0.0))

    model_mult = {
        "star": 1.00,
        "max": 0.78,
        "hume": 0.92,
        "legacy": 1.08,
    }.get(m_key, 1.00)

    if is_proj and not is_val:
        if lowest_resale > 0 and lowest_resale < rap * 0.65:
            real_v = float(lowest_resale) * 0.90
        elif rap >= 80000:
            real_v = min(22000.0, max(6500.0, float(rap) * 0.10))
        elif rap >= 20000:
            real_v = max(3500.0, float(rap) * 0.16)
        else:
            real_v = max(800.0, float(rap) * 0.28)
        if m_key == "max":
            real_v *= 0.85
        diff = real_v - base_val
        return {
            "low_op": int(round(diff * 1.1)),
            "mid_op": int(round(diff)),
            "high_op": int(round(diff * 0.9)),
            "category": "Projected",
            "reason": f"PROJECTED WARNING: Rapped at {format_k(rap)}, real trade value is ~{format_k(real_v)}",
        }

    if len(proof_ops) >= 2:
        trimmed = _trim_iqr(proof_ops[:12])
        p_low = 25 if m_key == "max" else (30 if m_key in ("star", "hume") else 35)
        p_mid = 45 if m_key == "max" else (50 if m_key in ("star", "hume") else 55)
        p_high = 65 if m_key == "max" else (75 if m_key in ("star", "hume") else 80)
        low_op = int(round(_percentile(trimmed, p_low)))
        mid_op = int(round(_percentile(trimmed, p_mid)))
        high_op = int(round(_percentile(trimmed, p_high)))
        return {
            "low_op": low_op,
            "mid_op": mid_op,
            "high_op": high_op,
            "category": f"Verified Proofs ({len(proof_ops)})",
            "reason": f"{len(proof_ops)} recorded trade proofs show {format_k(low_op)} to {format_k(high_op)} OP",
        }

    if len(proof_ops) == 1:
        p_op = proof_ops[0]
        all_samples = [p_op, p_op] + ad_ops
        trimmed = _trim_iqr(all_samples)
        mid_op = int(round(_percentile(trimmed, 50) * model_mult))
        low_op = int(round(mid_op * 0.85 if mid_op >= 0 else mid_op * 1.15))
        high_op = int(round(mid_op * 1.15 if mid_op >= 0 else mid_op * 0.85))
        return {
            "low_op": low_op,
            "mid_op": mid_op,
            "high_op": high_op,
            "category": "1 Recorded Proof",
            "reason": f"1 recorded proof at {format_k(int(round(p_op)))} OP (estimated range {format_k(low_op)} to {format_k(high_op)} OP)",
        }

    if len(ad_ops) >= 2:
        trimmed = _trim_iqr(ad_ops)
        low_op = int(round(_percentile(trimmed, 30) * model_mult))
        mid_op = int(round(_percentile(trimmed, 50) * model_mult))
        high_op = int(round(_percentile(trimmed, 75) * model_mult))
        return {
            "low_op": low_op,
            "mid_op": mid_op,
            "high_op": high_op,
            "category": "Live Ad Quotes (0 Proofs)",
            "reason": f"0 recorded proofs — {len(ad_ops)} live Rolimons trade ads quote {format_k(low_op)} to {format_k(high_op)} OP",
        }

    is_semi, semi_kind = is_low_copy_or_semi(item, tracked_copies)
    hoard_mult = 1.0 + min(0.20, hoard_pct / 140.0)

    if not is_val and is_semi:
        scarcity_mult = max(0.85, min(2.35, 3.8 / max(1.5, float(tracked_copies))))
        supply_est_op = float(base_val) * scarcity_mult
        if lowest_resale > base_val:
            bp_gap = float(lowest_resale - base_val)
            raw_est_op = min(bp_gap * 0.65, max(supply_est_op, bp_gap * 0.25))
            src_detail = (
                f"0 recorded proofs — estimated from {format_k(lowest_resale)} Best Price & {tracked_copies} tracked copies"
            )
        else:
            raw_est_op = supply_est_op
            src_detail = (
                f"0 recorded proofs — estimated from low supply ({tracked_copies} tracked copies, no seller wall data)"
            )

        est_mid = raw_est_op * hoard_mult * model_mult
        if lowest_resale > base_val:
            est_mid = min(est_mid, (lowest_resale - base_val) * 0.72)
        low_op = int(round((est_mid * 0.85) / 100.0) * 100)
        high_op = int(round((est_mid * 1.12) / 100.0) * 100)
        if lowest_resale > base_val:
            high_op = min(high_op, int(round((lowest_resale - base_val) * 0.80 / 100.0) * 100))
            low_op = min(low_op, high_op)
        mid_op = int(round(((low_op + high_op) / 2.0) / 100.0) * 100)
        return {
            "low_op": low_op,
            "mid_op": mid_op,
            "high_op": high_op,
            "category": f"{semi_kind} (Estimated)",
            "reason": f"Low-copy semi-rare ({src_detail}): estimated +{format_k(low_op)} to +{format_k(high_op)} OP",
        }

    if is_val:
        if is_rare:
            if demand >= 3:
                l_r, m_r, h_r = (0.04, 0.075, 0.12)
            elif demand == 2:
                l_r, m_r, h_r = (0.01, 0.04, 0.08)
            elif demand == 1:
                l_r, m_r, h_r = (-0.03, 0.01, 0.05)
            else:
                if base_val >= 20_000_000:
                    l_r, m_r, h_r = (-0.12, -0.07, -0.02)
                else:
                    l_r, m_r, h_r = (-0.02, 0.02, 0.06)
            cat_label = f"Rare ({item.get('demand_label', 'Assigned')} demand)"
        elif is_semi:
            if demand >= 2:
                l_r, m_r, h_r = (0.03, 0.06, 0.10)
            else:
                l_r, m_r, h_r = (0.015, 0.045, 0.085)
            cat_label = f"Semi-Rare ({item.get('demand_label', 'Low')} demand)"
        else:
            tier_scale = min(0.025, math.log10(max(1000, base_val) / 1000.0) * 0.01)
            rates = {
                4: (0.05 + tier_scale, 0.08 + tier_scale, 0.12 + tier_scale),
                3: (0.03 + tier_scale, 0.06 + tier_scale, 0.095 + tier_scale),
                2: (0.00, 0.025, 0.055),
                1: (-0.02, 0.005, 0.035),
                0: (-0.04, -0.01, 0.02),
            }.get(demand, (0.0, 0.02, 0.05))
            l_r, m_r, h_r = rates
            cat_label = f"Valued ({item.get('demand_label', 'Assigned')} demand)"

        if trend == 3:
            l_r += 0.015
            m_r += 0.02
            h_r += 0.025
        elif trend == 0:
            l_r -= 0.05
            m_r -= 0.05
            h_r -= 0.04
        elif trend == 1:
            l_r -= 0.025
            m_r -= 0.025
            h_r -= 0.02

        low_op = int(round((base_val * l_r * hoard_mult * model_mult) / 50.0) * 50)
        mid_op = int(round((base_val * m_r * hoard_mult * model_mult) / 50.0) * 50)
        high_op = int(round((base_val * h_r * hoard_mult * model_mult) / 50.0) * 50)
        return {
            "low_op": low_op,
            "mid_op": mid_op,
            "high_op": high_op,
            "category": cat_label,
            "reason": f"{cat_label} (0 proofs in DB — estimated {format_k(low_op)} to {format_k(high_op)} OP)",
        }

    low_op = int(round(-base_val * 0.03 / 50.0) * 50)
    mid_op = 0
    high_op = int(round(base_val * 0.03 / 50.0) * 50)
    return {
        "low_op": low_op,
        "mid_op": mid_op,
        "high_op": high_op,
        "category": "Unvalued RAP Item",
        "reason": f"Unvalued item trading near its {format_k(rap)} RAP",
    }


def compute_op_lookup(
    db: Database,
    item_id: int,
    items_catalog: Dict[int, Dict[str, Any]],
    recent_ads: Optional[List[Dict[str, Any]]] = None,
    sales_client: Optional[SalesClient] = None,
    window_days: int = 30,
    model: str = "star",
    now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    now = now_ts if now_ts is not None else int(time.time())
    cutoff = now - (window_days * 86400)
    model_name, model_desc = resolve_model_label(model)

    item = items_catalog.get(
        item_id,
        {
            "item_id": item_id,
            "name": f"Item #{item_id}",
            "acronym": "",
            "value": 0,
            "rap": 0,
            "demand": -1,
            "demand_label": "Unassigned",
            "trend": -1,
            "trend_label": "Unassigned",
            "rare": False,
            "projected": False,
            "is_valued": False,
        },
    )
    base_val = int(item["value"])
    rap = int(item.get("rap", 0))

    raw_proofs = db.get_proofs_for_item(item_id, limit=50)
    window_proofs = [p for p in raw_proofs if p["detected_at"] >= cutoff]
    proof_ops = [float(p["realized_op"]) for p in window_proofs]
    ad_ops = compute_ad_implied_ops(item_id, recent_ads or [], items_catalog)
    hoard_stats = db.get_item_hoard_stats(item_id)

    sc = sales_client or SalesClient()
    lowest_resale = 0
    try:
        s_sum = sc.fetch_item_sales_summary(db, item_id, item)
        lowest_resale = int(s_sum.get("lowest_resale", 0))
    except Exception:
        pass

    band = estimate_dynamic_item_op_band(
        item=item,
        proof_ops=proof_ops,
        ad_ops=ad_ops,
        hoard_stats=hoard_stats,
        lowest_resale=lowest_resale,
        model_key=model,
    )

    low_op = band["low_op"]
    mid_op = band["mid_op"]
    high_op = band["high_op"]

    return {
        "item": item,
        "model_name": model_name,
        "model_desc": model_desc,
        "status": "ok",
        "confidence": band["category"],
        "confidence_detail": f"{model_name} ({model_desc}) • {band['category']}",
        "avg_op": mid_op,
        "op_range": [low_op, high_op],
        "max_op_to_give": low_op,
        "max_total_to_give": base_val + low_op,
        "min_op_to_expect": high_op,
        "min_total_to_expect": base_val + high_op,
        "proof_count": len(proof_ops),
        "ad_quote_count": len(ad_ops),
        "hoard_stats": hoard_stats,
        "lowest_resale": lowest_resale,
        "explanation": band["reason"],
    }
