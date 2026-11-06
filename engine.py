from typing import Any, Dict, List, Optional
from data.sales import SalesClient
from db import Database
from oplookup import _percentile, _trim_iqr, compute_ad_implied_ops
from proofs import format_k


def is_semi_rare_item(item: Dict[str, Any]) -> bool:
    """
    Detects whether an item behaves as a semi-rare / rare:
      - Explicitly flagged `rare` on Rolimons, OR
      - Valued item with low/terrible/unassigned demand where Rolimons Value is >= 1.10x RAP
        and Value >= 25,000 (e.g., BPTH: 95k value, 83k RAP, demand 0), OR
      - Low total copies (<= 150).
    """
    if item.get("rare"):
        return True
    val = int(item.get("value", 0))
    rap = int(item.get("rap", 0))
    demand = int(item.get("demand", -1))
    is_valued = bool(item.get("is_valued", val != rap))
    if is_valued and val >= 25000 and rap > 0 and (val / rap) >= 1.10 and demand <= 1:
        return True
    qty = int(item.get("total_quantity", 0))
    if 0 < qty <= 150:
        return True
    return False


def item_liquidity_score(
    item: Dict[str, Any], proof_count: int = 0, daily_sales_vol: float = 0.0
) -> float:
    demand = int(item.get("demand", -1))
    base = {
        4: 0.95,  # Amazing (e.g. GQOTN, 8BRC, Bunny Ears)
        3: 0.80,  # High
        2: 0.60,  # Normal
        1: 0.40,  # Low
        0: 0.25,  # Terrible
        -1: 0.35, # Unassigned
    }.get(demand, 0.45)
    if is_semi_rare_item(item):
        base = max(base, 0.45)
    if daily_sales_vol >= 10.0:
        base = max(base, 0.85)
    elif daily_sales_vol >= 3.0:
        base = max(base, 0.65)
    if proof_count >= 3:
        base = min(1.0, base + 0.10)
    return base


class TradeEngine:
    """
    Symmetric Trade Analyser supporting 3 modes:
      - 'v3.1' (Pro): Prices every item on BOTH sides using proof median OP (when proofs exist),
        live/historical Roblox sales data (`priceDataPoints`, `volumeDataPoints`, `lowestResalePrice`),
        clamped live ad quotes, and small symmetric demand adjustments (±0..3%, never penalizing
        semi-rares like BPTH for low demand), plus upgrade/downgrade structure adjustments.
      - 'v3': Raw value difference with rarity/demand warnings.
      - 'free': Plain Rolimons value difference.
    """

    def __init__(
        self,
        db: Database,
        sales_client: Optional[SalesClient] = None,
        risk_edge_1v1_rate: float = 0.05,
        retrade_retention_factor: float = 0.625,
    ):
        self.db = db
        self.sales_client = sales_client or SalesClient()
        self.risk_edge_1v1_rate = risk_edge_1v1_rate
        self.retrade_retention_factor = retrade_retention_factor

    def evaluate_single_item(
        self,
        item: Dict[str, Any],
        recent_ads: Optional[List[Dict[str, Any]]],
        items_catalog: Dict[int, Dict[str, Any]],
    ) -> Dict[str, Any]:
        iid = int(item["item_id"])
        base_val = int(item.get("value", 0))
        rap = int(item.get("rap", 0))
        proofs = self.db.get_proofs_for_item(iid, limit=15)
        ops = [float(p["realized_op"]) for p in proofs]
        ad_ops = compute_ad_implied_ops(iid, recent_ads or [], items_catalog)
        semi = is_semi_rare_item(item)

        # Always fetch or read cached sales summary when proofs < 2
        sales_info: Dict[str, Any] = {
            "avg_price_7d": 0,
            "avg_price_30d": 0,
            "daily_volume": 0.0,
            "lowest_resale": 0,
            "recent_points": [],
        }
        if len(ops) < 2 and iid > 100000:
            try:
                sales_info = self.sales_client.fetch_item_sales_summary(self.db, iid, item)
            except Exception:
                pass

        daily_vol = float(sales_info.get("daily_volume", 0.0))
        avg_sale_7d = int(sales_info.get("avg_price_7d", 0))
        lowest_resale = int(sales_info.get("lowest_resale", 0))
        liq = item_liquidity_score(item, len(ops), daily_vol)

        if len(ops) >= 2:
            trimmed = _trim_iqr(ops[:6])
            low_op = _percentile(trimmed, 20)
            high_op = _percentile(trimmed, 85)
            mid_op = _percentile(trimmed, 50)
            weight = min(1.0, len(ops) / 3.0)
            eff_op = mid_op * self.retrade_retention_factor * weight
            return {
                "has_proofs": True,
                "proof_count": len(ops),
                "low_op": low_op,
                "high_op": high_op,
                "mid_op": mid_op,
                "effective_op": eff_op,
                "market_value": base_val + eff_op,
                "is_semi": semi,
                "liquidity": liq,
                "sales_info": sales_info,
                "source_note": f"{len(ops)} proofs",
            }
        elif len(ops) == 1:
            op = ops[0]
            eff_op = op * (self.retrade_retention_factor * 0.65)
            return {
                "has_proofs": True,
                "proof_count": 1,
                "low_op": op * 0.9,
                "high_op": op * 1.1,
                "mid_op": op,
                "effective_op": eff_op,
                "market_value": base_val + eff_op,
                "is_semi": semi,
                "liquidity": liq,
                "sales_info": sales_info,
                "source_note": "1 proof",
            }
        else:
            # 0 local trade proofs: use Rolimons demand/rarity + real Roblox sales data!
            demand = int(item.get("demand", -1))
            is_valued = bool(item.get("is_valued", False))

            if is_valued or demand >= 0:
                if semi:
                    bonus_rate = 0.02 if item.get("rare") else 0.01
                else:
                    bonus_rate = {
                        4: 0.03,   # Amazing (+3%, e.g. GQOTN, Bunny Ears, 8BRC)
                        3: 0.015,  # High (+1.5%)
                        2: 0.0,    # Normal (0%)
                        1: -0.015, # Low (-1.5%)
                        0: -0.025, # Terrible (-2.5% on non-semi normal items only)
                    }.get(demand, 0.0)
                eff = float(base_val) * bonus_rate
                return {
                    "has_proofs": False,
                    "proof_count": 0,
                    "low_op": eff,
                    "high_op": eff,
                    "mid_op": eff,
                    "effective_op": eff,
                    "market_value": base_val + eff,
                    "is_semi": semi,
                    "liquidity": liq,
                    "sales_info": sales_info,
                    "source_note": "Rolimons valuation + sales",
                }

            # Unvalued item (e.g. Oozing Oscar or RAP item): price from recent 7d sales & lowest resale!
            if avg_sale_7d > 0 and base_val > 0:
                # Blend 7-day average sale price and base RAP, clamped so a single spike doesn't fake a huge win
                sales_diff = float(avg_sale_7d - base_val)
                if lowest_resale > 0 and lowest_resale < avg_sale_7d:
                    sales_diff = float( min(avg_sale_7d, (avg_sale_7d + lowest_resale) // 2) - base_val )
                max_adj = base_val * (0.25 if semi else 0.08)
                eff = max(-max_adj, min(max_adj, sales_diff * 0.6))
                return {
                    "has_proofs": False,
                    "proof_count": 0,
                    "low_op": eff,
                    "high_op": eff,
                    "mid_op": eff,
                    "effective_op": eff,
                    "market_value": base_val + eff,
                    "is_semi": semi,
                    "liquidity": liq,
                    "sales_info": sales_info,
                    "source_note": "Roblox sales history",
                }

            if len(ad_ops) >= 2:
                trimmed = _trim_iqr(ad_ops)
                mid_op = _percentile(trimmed, 50)
                max_ad_adj = base_val * (0.12 if semi else 0.04)
                clamped_op = max(-max_ad_adj, min(max_ad_adj, mid_op * 0.35))
                return {
                    "has_proofs": False,
                    "proof_count": 0,
                    "low_op": min(trimmed),
                    "high_op": max(trimmed),
                    "mid_op": mid_op,
                    "effective_op": clamped_op,
                    "market_value": base_val + clamped_op,
                    "is_semi": semi,
                    "liquidity": liq,
                    "sales_info": sales_info,
                    "source_note": f"{len(ad_ops)} ad quotes",
                }

            return {
                "has_proofs": False,
                "proof_count": 0,
                "low_op": 0.0,
                "high_op": 0.0,
                "mid_op": 0.0,
                "effective_op": 0.0,
                "market_value": float(base_val),
                "is_semi": semi,
                "liquidity": liq,
                "sales_info": sales_info,
                "source_note": "base RAP/value",
            }

    def analyze_trade(
        self,
        give_item_ids: List[int],
        receive_item_ids: List[int],
        items_catalog: Dict[int, Dict[str, Any]],
        engine_mode: str = "v3.1",
        recent_ads: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        mode = engine_mode.lower()

        def resolve(iid: int) -> Dict[str, Any]:
            return items_catalog.get(
                iid,
                {
                    "item_id": iid,
                    "name": f"Item #{iid}",
                    "acronym": "",
                    "value": 0,
                    "rap": 0,
                    "demand": -1,
                    "demand_label": "Unassigned",
                    "rare": False,
                    "projected": False,
                    "is_valued": False,
                    "source": "rolimons",
                },
            )

        give_items = [resolve(iid) for iid in give_item_ids]
        receive_items = [resolve(iid) for iid in receive_item_ids]

        give_val = sum(int(x.get("value", 0)) for x in give_items)
        give_rap = sum(int(x.get("rap", 0)) for x in give_items)
        recv_val = sum(int(x.get("value", 0)) for x in receive_items)
        recv_rap = sum(int(x.get("rap", 0)) for x in receive_items)

        raw_val_op = recv_val - give_val
        raw_rap_op = recv_rap - give_rap

        bullets: List[str] = []
        verdict = "Not Worth It"
        color = "red"
        retrade_room = float(raw_val_op)

        if mode == "free":
            if raw_val_op > give_val * 0.04:
                verdict = "Win"
                color = "green"
            elif raw_val_op >= -give_val * 0.02:
                verdict = "Fair Swap"
                color = "green"
            else:
                verdict = "Not Worth It"
                color = "red"
            bullets.append(f"Raw value difference is **{format_k(raw_val_op)}**.")
            bullets.append("Free model only checks base Rolimons value and ignores proofs, sales, and upgrades.")

        elif mode == "v3":
            for it in receive_items:
                if is_semi_rare_item(it) or it.get("demand", -1) <= 1:
                    bullets.append(
                        f"**{it['name']}** may be very slow to move, but that slowness is usually paid for."
                    )
            bullets.append(
                f"Value difference is **{format_k(raw_val_op)}** "
                f"({'you give more than you receive' if raw_val_op < 0 else 'you receive more than you give'})."
            )
            bullets.append("Better if you plan to hold rather than flip it quick.")
            bullets.append("Not much proof weighting in V3 mode, so be a little careful.")
            if raw_val_op > give_val * 0.05:
                verdict = "Win"
                color = "green"
            elif raw_val_op >= -give_val * 0.02:
                verdict = "Fair Swap"
                color = "green"
            else:
                verdict = "Not Worth It"
                color = "red"

        else:  # V3.1 Symmetric Proof + Sales + Structure Model
            give_evals = [self.evaluate_single_item(it, recent_ads, items_catalog) for it in give_items]
            recv_evals = [self.evaluate_single_item(it, recent_ads, items_catalog) for it in receive_items]

            give_mkt = sum(e["market_value"] for e in give_evals)
            recv_mkt = sum(e["market_value"] for e in recv_evals)

            any_low_copy_recv = False
            any_unpriced_rare = False

            def _sales_snippet(ev: Dict[str, Any]) -> str:
                s = ev.get("sales_info") or {}
                avg7 = int(s.get("avg_price_7d", 0))
                vol = float(s.get("daily_volume", 0.0))
                low_r = int(s.get("lowest_resale", 0))
                parts = []
                if avg7 > 0:
                    parts.append(f"7d avg sale **{format_k(avg7)}**")
                if vol > 0:
                    parts.append(f"~**{vol}/day** sold")
                if low_r > 0:
                    parts.append(f"lowest listing **{format_k(low_r)}**")
                return f" (Sales: {', '.join(parts)})" if parts else ""

            for it, ev in zip(give_items, give_evals):
                s_note = _sales_snippet(ev)
                if ev["has_proofs"] and abs(ev["mid_op"]) >= it["value"] * 0.05:
                    bullets.append(
                        f"**You give {it['name']}**: proofs price it around **{format_k(ev['mid_op'])}** OP{s_note}."
                    )
                elif ev["is_semi"]:
                    bullets.append(
                        f"**You give {it['name']}**: semi-rare / low-copy (holds value even with {it.get('demand_label', 'Low')} demand){s_note}."
                    )
                elif int(it.get("demand", -1)) >= 3:
                    bullets.append(
                        f"**You give {it['name']}**: high liquidity ({it.get('demand_label', 'High')} demand){s_note}."
                    )
                elif s_note:
                    bullets.append(f"**You give {it['name']}**:{s_note}.")

            for it, ev in zip(receive_items, recv_evals):
                s_note = _sales_snippet(ev)
                is_low_copy = ev["is_semi"] or not it.get("is_valued", True) or ev["mid_op"] > it["value"] * 0.20
                if is_low_copy:
                    any_low_copy_recv = True
                    if ev["has_proofs"] and ev["mid_op"] > 0:
                        bullets.append(
                            f"**{it['name']}** is a low-copy, and the last few proofs still have it "
                            f"getting about **+{format_k(ev['low_op'])} to +{format_k(ev['high_op'])}** in OP."
                        )
                    elif ev["is_semi"] and it.get("is_valued", True):
                        bullets.append(
                            f"**{it['name']}** is a valued semi-rare ({format_k(it['value'])} val / {format_k(it['rap'])} RAP) — "
                            f"slow to move, but holds value without a low-demand penalty{s_note}."
                        )
                    elif s_note:
                        bullets.append(
                            f"**{it['name']}** (RAP item): active sales data checked{s_note}."
                        )
                    else:
                        any_unpriced_rare = True
                        bullets.append(
                            f"**{it['name']}** is a rare/low-copy item with no recent sales or local proofs — verify OP manually."
                        )
                elif int(it.get("demand", -1)) >= 3:
                    bullets.append(
                        f"**You receive {it['name']}**: fast-moving {it.get('demand_label', 'High')} demand item{s_note}."
                    )
                elif s_note:
                    bullets.append(f"**You receive {it['name']}**:{s_note}.")

            # Upgrade / Downgrade structural adjustment
            structure_bonus = 0.0
            max_give_single = max((int(x.get("value", 0)) for x in give_items), default=0)
            max_recv_single = max((int(x.get("value", 0)) for x in receive_items), default=0)

            if len(give_items) > len(receive_items) and max_recv_single > max_give_single:
                avg_recv_liq = sum(e["liquidity"] for e in recv_evals) / max(1, len(recv_evals))
                upgrade_rate = 0.04 + (0.04 * avg_recv_liq)
                structure_bonus = recv_val * upgrade_rate
                bullets.append(
                    f"**Upgrade advantage**: consolidating {len(give_items)} smaller items into {len(receive_items)} larger item "
                    f"saves ~**+{format_k(structure_bonus)}** in typical upgrade overpay."
                )
            elif len(receive_items) > len(give_items) and max_give_single > max_recv_single:
                avg_give_liq = sum(e["liquidity"] for e in give_evals) / max(1, len(give_evals))
                downgrade_cost = give_val * (0.04 + 0.03 * avg_give_liq)
                structure_bonus = -downgrade_cost
                bullets.append(
                    f"**Downgrade note**: splitting {len(give_items)} big item into {len(receive_items)} smaller items "
                    f"normally requires at least **+{format_k(downgrade_cost)}** overpay."
                )

            risk_edge = round(max(give_val, recv_val) * self.risk_edge_1v1_rate / 100.0) * 100
            raw_mkt_edge = (recv_mkt - give_mkt) + structure_bonus

            if len(give_items) == 1 and len(receive_items) == 1:
                bullets.append(
                    f"On a 1 for 1 like this, you still want at least about **+{format_k(risk_edge)}** of real edge after risk."
                )
                if any_low_copy_recv and any(e["has_proofs"] for e in recv_evals):
                    retrade_room = raw_mkt_edge - risk_edge
                else:
                    retrade_room = raw_mkt_edge
            else:
                retrade_room = raw_mkt_edge

            sign_str = "+" if retrade_room >= 0 else ""
            bullets.append(
                f"After pricing in the likely retrade room, this looks closer to **{sign_str}{format_k(retrade_room)}**."
            )

            if any_low_copy_recv and not any(int(x.get("demand", -1)) >= 3 for x in receive_items):
                bullets.append("This is more of a hold-and-retrade play than a quick flip.")
            else:
                bullets.append("Liquid demand & sales structure makes this suitable for a standard flip.")

            ref_val = max(give_val, recv_val, 1)
            edge_pct = (retrade_room / ref_val) * 100.0

            if any_unpriced_rare and raw_val_op < -ref_val * 0.05:
                verdict = "Needs Proofs (Unpriced Semi/Rare)"
                color = "yellow"
            elif raw_val_op < 0 and any_low_copy_recv and retrade_room > 0:
                verdict = "Fair Swap"
                color = "green"
            elif edge_pct >= 8.0:
                verdict = "Strong Win"
                color = "green"
            elif edge_pct >= 2.5:
                verdict = "Win"
                color = "green"
            elif edge_pct >= -3.0:
                verdict = "Fair Swap"
                color = "green"
            elif edge_pct >= -7.5:
                verdict = "Slight Loss"
                color = "yellow"
            else:
                verdict = "Not Worth It"
                color = "red"

        return {
            "engine": mode.upper(),
            "verdict": verdict,
            "color": color,
            "give_items": give_items,
            "receive_items": receive_items,
            "give_value": give_val,
            "give_rap": give_rap,
            "receive_value": recv_val,
            "receive_rap": recv_rap,
            "op_value": raw_val_op,
            "op_rap": raw_rap_op,
            "retrade_room": int(round(retrade_room)),
            "bullets": bullets,
            "disclaimer": "Suggestion only - please double-check before you trade.",
        }
