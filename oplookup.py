import math
import time
from typing import Any, Dict, List, Optional
from data.sales import SalesClient
from db import Database


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
        min_ratio = 0.98 if demand >= 3 else 0.75
        max_ratio = 10.0 if is_rare_or_unvalued else 1.5

        if req_ids == [item_id] and (min_ratio <= offer_val / req_val <= max_ratio):
            implied.append(float(offer_val - req_val))
        elif offer_ids == [item_id] and (min_ratio <= req_val / offer_val <= max_ratio):
            implied.append(float(req_val - offer_val))

    return implied


def compute_op_lookup(
    db: Database,
    item_id: int,
    items_catalog: Dict[int, Dict[str, Any]],
    recent_ads: Optional[List[Dict[str, Any]]] = None,
    sales_client: Optional[SalesClient] = None,
    window_days: int = 30,
    buy_percentile: float = 35.0,
    sell_percentile: float = 68.0,
    now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Computes Void-style OP lookup + Live Sales Valuation for an item:
      - avg_trades_per_day + daily_sales_volume (from Roblox sales history)
      - avg_op (from proofs, or Rolimons demand + real 7d/30d sales prices)
      - max_to_give (buying) & min_op_to_expect (selling)
      - sales_summary: 7d avg sale price, 30d avg sale price, daily volume, lowest reseller price
    """
    now = now_ts if now_ts is not None else int(time.time())
    cutoff = now - (window_days * 86400)

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

    # Fetch real sales data (priceDataPoints, volumeDataPoints, lowestResalePrice)
    sc = sales_client or SalesClient()
    sales_summary: Dict[str, Any] = {
        "avg_price_7d": 0,
        "avg_price_30d": 0,
        "daily_volume": 0.0,
        "lowest_resale": 0,
        "recent_points": [],
    }
    if item_id > 100000:
        try:
            sales_summary = sc.fetch_item_sales_summary(db, item_id, item)
        except Exception:
            pass

    avg_sale_7d = int(sales_summary.get("avg_price_7d", 0))
    avg_sale_30d = int(sales_summary.get("avg_price_30d", 0))
    daily_sales_vol = float(sales_summary.get("daily_volume", 0.0))
    lowest_resale = int(sales_summary.get("lowest_resale", 0))

    errors_pct: List[float] = []
    for p in window_proofs:
        if p.get("predicted_op") is not None and base_val > 0:
            err = abs(float(p["realized_op"]) - float(p["predicted_op"])) / float(base_val) * 100.0
            errors_pct.append(err)
    backtest_error_str = (
        f"±{round(sum(errors_pct) / len(errors_pct), 1)}% on last {len(errors_pct)} trades"
        if errors_pct
        else "Calibrating (awaiting more multi-proof trades)"
    )

    if len(proof_ops) >= 3:
        trimmed = _trim_iqr(proof_ops)
        median_op = int(round(_percentile(trimmed, 50)))
        buy_op = int(round(_percentile(trimmed, buy_percentile)))
        sell_op = int(round(_percentile(trimmed, sell_percentile)))
        min_op_seen = int(round(min(trimmed)))
        max_op_seen = int(round(max(trimmed)))

        oldest_ts = min(p["detected_at"] for p in window_proofs)
        span_days = max(1.0, (now - oldest_ts) / 86400.0)
        trades_per_day = round(len(window_proofs) / span_days, 2)

        confidence = "HIGH" if len(proof_ops) >= 6 else "MEDIUM"
        confidence_detail = f"{confidence} ({len(proof_ops)} verified proofs / {window_days}d)"
        status = "ok"
    elif len(proof_ops) >= 1:
        combined = proof_ops * 2 + ad_ops
        trimmed = _trim_iqr(combined)
        median_op = int(round(_percentile(trimmed, 50)))
        buy_op = int(round(_percentile(trimmed, buy_percentile)))
        sell_op = int(round(_percentile(trimmed, sell_percentile)))
        min_op_seen = int(round(min(trimmed)))
        max_op_seen = int(round(max(trimmed)))
        trades_per_day = round(max(daily_sales_vol * 0.4, len(proof_ops) / 7.0), 2)
        confidence = "LOW"
        confidence_detail = f"LOW ({len(proof_ops)} proof, supplemented by sales & {len(ad_ops)} ad quotes)"
        status = "low_data"
    else:
        # 0 local trade proofs: check Rolimons valuation + Roblox Sales History!
        demand = int(item.get("demand", -1))
        is_rare = bool(item.get("rare"))
        val_ratio = (base_val / max(1, rap)) if rap > 0 else 1.0
        is_semi = is_rare or (bool(item.get("is_valued", False)) and base_val >= 25000 and val_ratio >= 1.10 and demand <= 1)

        if is_rare and avg_sale_7d == 0:
            # True rare with 0 proofs AND 0 recent sales
            return {
                "item": item,
                "status": "insufficient_proofs",
                "confidence": "INSUFFICIENT",
                "confidence_detail": "INSUFFICIENT (0 trade proofs and 0 recent sales — refusing to guess on rare)",
                "avg_trades_per_day": 0.0,
                "daily_sales_volume": 0.0,
                "avg_op": None,
                "op_range": None,
                "max_op_to_give": None,
                "max_total_to_give": None,
                "min_op_to_expect": None,
                "min_total_to_expect": None,
                "proof_count": 0,
                "ad_quote_count": len(ad_ops),
                "sales_summary": sales_summary,
                "backtest_accuracy": backtest_error_str,
                "explanation": (
                    f"{item['name']} is a rare item with 0 local proofs and 0 recent sales. "
                    "Void Free outputs broken negative numbers here by using raw RAP/Value. "
                    "Wait for tracker proofs or log a real proof before pricing OP."
                ),
            }

        if bool(item.get("is_valued", False)) or demand >= 0:
            # Valued item (including Bunny Ears, 8BRC, GQOTN, BPTH)
            if is_semi:
                b_rate, m_rate, s_rate = (0.0, 0.03, 0.07)
            else:
                demand_op_rates = {
                    4: (0.04, 0.07, 0.11),   # Amazing (e.g. Bunny Ears, 8BRC, GQOTN)
                    3: (0.02, 0.05, 0.08),   # High
                    2: (-0.02, 0.01, 0.04),  # Normal
                    1: (-0.05, -0.02, 0.01), # Low
                    0: (-0.08, -0.05, -0.02),# Terrible
                }
                b_rate, m_rate, s_rate = demand_op_rates.get(demand, (0.0, 0.02, 0.05))
            buy_op = int(round(base_val * b_rate / 50.0) * 50)
            median_op = int(round(base_val * m_rate / 50.0) * 50)
            sell_op = int(round(base_val * s_rate / 50.0) * 50)
            min_op_seen = buy_op
            max_op_seen = sell_op
            est_activity = daily_sales_vol if daily_sales_vol > 0 else {4: 5.0, 3: 2.5, 2: 1.0, 1: 0.4, 0: 0.2}.get(demand, 0.5)
            trades_per_day = round(est_activity, 1)
            confidence = "SALES + DEMAND"
            confidence_detail = (
                f"SALES + DEMAND ({item.get('demand_label', 'Assigned')} demand, "
                f"~{daily_sales_vol}/day sold, 7d avg sale {avg_sale_7d:,})"
                if avg_sale_7d > 0
                else f"DEMAND BASELINE ({item.get('demand_label', 'Assigned')} demand)"
            )
            status = "sales_and_demand"
        elif avg_sale_7d > 0:
            # Unvalued RAP item (like Oozing Oscar) — price directly from 7d/30d sales vs RAP!
            median_op = int(round((avg_sale_7d - base_val) * 0.6))
            buy_op = int(round(min(0, median_op) - base_val * 0.03))
            sell_op = int(round(max(0, median_op) + base_val * 0.04))
            min_op_seen = buy_op
            max_op_seen = sell_op
            trades_per_day = round(daily_sales_vol, 1)
            confidence = "SALES DATA"
            confidence_detail = (
                f"LIVE SALES DATA (~{daily_sales_vol}/day sold, 7d avg sale {avg_sale_7d:,} R$, "
                f"30d avg {avg_sale_30d:,} R$)"
            )
            status = "sales_backed"
        else:
            return {
                "item": item,
                "status": "insufficient_proofs",
                "confidence": "INSUFFICIENT",
                "confidence_detail": "INSUFFICIENT (0 trade proofs and 0 recent sales)",
                "avg_trades_per_day": 0.0,
                "daily_sales_volume": 0.0,
                "avg_op": None,
                "op_range": None,
                "max_op_to_give": None,
                "max_total_to_give": None,
                "min_op_to_expect": None,
                "min_total_to_expect": None,
                "proof_count": 0,
                "ad_quote_count": len(ad_ops),
                "sales_summary": sales_summary,
                "backtest_accuracy": backtest_error_str,
                "explanation": f"No recent sales or trade proofs found for {item['name']}.",
            }

    return {
        "item": item,
        "status": status,
        "confidence": confidence,
        "confidence_detail": confidence_detail,
        "avg_trades_per_day": trades_per_day,
        "daily_sales_volume": daily_sales_vol,
        "avg_op": median_op,
        "op_range": [min_op_seen, max_op_seen],
        "max_op_to_give": buy_op,
        "max_total_to_give": base_val + buy_op,
        "min_op_to_expect": sell_op,
        "min_total_to_expect": base_val + sell_op,
        "proof_count": len(proof_ops),
        "ad_quote_count": len(ad_ops),
        "sales_summary": sales_summary,
        "backtest_accuracy": backtest_error_str,
        "explanation": (
            f"Based on {len(proof_ops)} trade proof(s), ~{daily_sales_vol} sales/day "
            f"(7d avg sale: {avg_sale_7d:,} R$, lowest reseller: {lowest_resale:,} R$). "
            f"Buy up to {base_val + buy_op:,} ({buy_op:+,} OP); "
            f"when selling, expect at least {base_val + sell_op:,} ({sell_op:+,} OP)."
        ),
    }
