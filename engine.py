from typing import Any, Dict, List, Optional
from data.sales import SalesClient
from db import Database
from oplookup import (
    _percentile,
    _trim_iqr,
    compute_ad_implied_ops,
    estimate_dynamic_item_op_band,
    is_low_copy_or_semi,
    normalize_model_key,
    resolve_model_label,
)
from proofs import format_k


def is_rare_item(item: Dict[str, Any]) -> bool:
    return bool(item.get("rare"))


def is_semi_rare_item(item: Dict[str, Any], tracked_copies: int = 999) -> bool:
    if item.get("rare"):
        return False
    flag, _ = is_low_copy_or_semi(item, tracked_copies)
    return flag


def item_liquidity_score(
    item: Dict[str, Any], proof_count: int = 0, tracked_copies: int = 999
) -> float:
    demand = int(item.get("demand", -1))
    base = {
        4: 0.95,
        3: 0.82,
        2: 0.62,
        1: 0.42,
        0: 0.25,
        -1: 0.35,
    }.get(demand, 0.40)
    if is_rare_item(item) and demand <= 1:
        base = min(base, 0.25)
    elif is_semi_rare_item(item, tracked_copies):
        base = max(base, 0.42)
    if proof_count >= 3:
        base = min(1.0, base + 0.12)
    return base


class TradeEngine:
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
        model_key: str = "star",
        side: str = "receive",
    ) -> Dict[str, Any]:
        iid = int(item["item_id"])
        base_val = int(item.get("value", 0))
        rap = int(item.get("rap", 0))
        demand = int(item.get("demand", -1))
        trend = int(item.get("trend", -1))
        is_valued = bool(item.get("is_valued", False))
        is_proj = bool(item.get("projected", False))
        rare = is_rare_item(item)

        proofs = self.db.get_proofs_for_item(iid, limit=15)
        ops = [float(p["realized_op"]) for p in proofs]
        ad_ops = compute_ad_implied_ops(iid, recent_ads or [], items_catalog)
        hoard_stats = self.db.get_item_hoard_stats(iid)
        tracked_copies = int(hoard_stats.get("total_tracked", 0))
        semi = is_semi_rare_item(item, tracked_copies)

        lowest_resale = 0
        avg_sale_7d = 0
        try:
            s_info = self.sales_client.fetch_item_sales_summary(self.db, iid, item)
            lowest_resale = int(s_info.get("lowest_resale", 0))
            avg_sale_7d = int(s_info.get("avg_price_7d", 0))
        except Exception:
            pass

        liq = item_liquidity_score(item, len(ops), tracked_copies)

        best_price_note = ""
        if lowest_resale > 0 and base_val > 0:
            bp_ratio = lowest_resale / float(base_val)
            if bp_ratio >= 1.8:
                best_price_note = f"Best Price {format_k(lowest_resale)} is {int(round(bp_ratio * 100))}% of RAP/Value"
            elif bp_ratio <= 0.85 and not is_valued and not semi:
                best_price_note = f"Best Price {format_k(lowest_resale)} is {(1.0 - bp_ratio)*100:.0f}% below RAP"

        hoard_note = ""
        hoarder_cnt = int(hoard_stats.get("hoarder_count", 0))
        hoarded_copies = int(hoard_stats.get("hoarded_copies", 0))
        hoard_pct = float(hoard_stats.get("hoard_percent", 0.0))
        if hoarder_cnt > 0:
            hoard_note = f"{hoarder_cnt} hoarder(s) hold {hoarded_copies} copies ({hoard_pct:.1f}% of tracked supply)"

        band = estimate_dynamic_item_op_band(
            item=item,
            proof_ops=ops,
            ad_ops=ad_ops,
            hoard_stats=hoard_stats,
            lowest_resale=lowest_resale,
            model_key=model_key,
        )

        low_op = float(band["low_op"])
        mid_op = float(band["mid_op"])
        high_op = float(band["high_op"])
        reason = str(band["reason"])

        if not is_valued and not semi and not rare and not is_proj and demand < 0 and len(ops) == 0:
            if avg_sale_7d > 0 and avg_sale_7d < rap * 0.88 and rap >= 15000:
                bloated_gap = float(rap - avg_sale_7d)
                eff = -min(float(rap) * 0.25, bloated_gap * 0.90)
                low_op = eff * 1.1
                high_op = eff * 0.9
                mid_op = eff
                reason = f"Bloated RAP item ({format_k(rap)} RAP vs {format_k(avg_sale_7d)} true level)"
                return {
                    "has_proofs": False,
                    "proof_count": 0,
                    "low_op": low_op,
                    "high_op": high_op,
                    "mid_op": mid_op,
                    "effective_op": eff,
                    "market_value": base_val + eff,
                    "is_rare": rare,
                    "is_semi": False,
                    "is_projected": False,
                    "liquidity": liq,
                    "hoard_stats": hoard_stats,
                    "best_price_note": best_price_note,
                    "hoard_note": hoard_note,
                    "source_note": reason,
                }

        m_clean = normalize_model_key(model_key)
        if semi and not is_valued and mid_op > 0:
            retention = {
                "star": 0.72,
                "max": 0.52,
                "hume": 0.625,
                "legacy": 0.80,
            }.get(m_clean, 0.625)
            eff_op = mid_op * retention
        elif len(ops) >= 2:
            retention = {
                "star": 0.85,
                "max": 0.70,
                "hume": 0.78,
                "legacy": 0.90,
            }.get(m_clean, 0.80)
            eff_op = mid_op * retention
        else:
            eff_op = mid_op

        if trend == 0 and is_valued and side == "receive":
            drop_pen = -float(base_val) * (0.08 if m_clean in ("star", "max") else 0.05)
            eff_op += drop_pen
            reason += f" | Lowering trend ({format_k(drop_pen)} risk)"
        elif trend == 1 and is_valued and side == "receive":
            drop_pen = -float(base_val) * 0.045
            eff_op += drop_pen
            reason += f" | Unstable trend ({format_k(drop_pen)} risk)"

        return {
            "has_proofs": len(ops) > 0,
            "proof_count": len(ops),
            "low_op": low_op,
            "high_op": high_op,
            "mid_op": mid_op,
            "effective_op": eff_op,
            "market_value": base_val + eff_op,
            "is_rare": rare,
            "is_semi": semi,
            "is_projected": bool(is_proj and not is_valued),
            "liquidity": liq,
            "hoard_stats": hoard_stats,
            "best_price_note": best_price_note,
            "hoard_note": hoard_note,
            "source_note": reason,
        }

    def analyze_trade(
        self,
        give_item_ids: List[int],
        receive_item_ids: List[int],
        items_catalog: Dict[int, Dict[str, Any]],
        engine_mode: str = "star",
        recent_ads: Optional[List[Dict[str, Any]]] = None,
        give_robux: int = 0,
        receive_robux: int = 0,
    ) -> Dict[str, Any]:
        model_name, model_desc = resolve_model_label(engine_mode)
        mode = (engine_mode or "star").lower()
        g_robux = max(0, int(give_robux or 0))
        r_robux = max(0, int(receive_robux or 0))
        net_robux_give = int(round(g_robux * 0.70))
        net_robux_recv = int(round(r_robux * 0.70))

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
                    "trend": -1,
                    "trend_label": "Unassigned",
                    "rare": False,
                    "projected": False,
                    "is_valued": False,
                    "thumbnail_url": "",
                    "source": "rolimons",
                },
            )

        give_items = [resolve(iid) for iid in give_item_ids]
        receive_items = [resolve(iid) for iid in receive_item_ids]

        give_val = sum(int(x.get("value", 0)) for x in give_items) + net_robux_give
        give_rap = sum(int(x.get("rap", 0)) for x in give_items) + net_robux_give
        recv_val = sum(int(x.get("value", 0)) for x in receive_items) + net_robux_recv
        recv_rap = sum(int(x.get("rap", 0)) for x in receive_items) + net_robux_recv

        raw_val_op = recv_val - give_val
        raw_rap_op = recv_rap - give_rap

        ledger: List[Dict[str, Any]] = []
        bullets: List[str] = []
        factors: List[Dict[str, Any]] = []

        ledger.append(
            {
                "step": "1. Base Rolimons Value",
                "give": f"{give_val:,}",
                "receive": f"{recv_val:,}",
                "delta": f"{raw_val_op:+,}",
                "reason": f"Rolimons Value (RAP: {give_rap:,} vs {recv_rap:,})",
            }
        )

        give_evals = [
            self.evaluate_single_item(it, recent_ads, items_catalog, model_key=mode, side="give")
            for it in give_items
        ]
        recv_evals = [
            self.evaluate_single_item(it, recent_ads, items_catalog, model_key=mode, side="receive")
            for it in receive_items
        ]

        for it, ev in zip(give_items, give_evals):
            it["market_value"] = int(round(ev["market_value"]))
            it["effective_op"] = int(round(ev["effective_op"]))
            it["eval_reason"] = ev["source_note"]

        for it, ev in zip(receive_items, recv_evals):
            it["market_value"] = int(round(ev["market_value"]))
            it["effective_op"] = int(round(ev["effective_op"]))
            it["eval_reason"] = ev["source_note"]

        any_semi_or_lowcopy_recv = any(e["is_semi"] and not x.get("is_valued", False) for x, e in zip(receive_items, recv_evals))
        any_dropping_recv = any(int(x.get("trend", -1)) in (0, 1) for x in receive_items)
        any_proj_recv = any(e["is_projected"] for e in recv_evals)

        m_clean = normalize_model_key(mode)
        if any_semi_or_lowcopy_recv and len(give_items) == 1 and len(receive_items) == 1:
            give_mkt = float(give_val) if m_clean == "hume" else sum(e["market_value"] for e in give_evals) + net_robux_give
            recv_mkt = sum(e["market_value"] for e in recv_evals) + net_robux_recv
            risk_rate = {"star": 0.045, "max": 0.12, "hume": 0.05, "legacy": 0.015}.get(m_clean, 0.05)
            risk_edge = round(give_val * risk_rate)
            retrade_room = (recv_mkt - give_mkt) - risk_edge
        else:
            give_mkt = sum(e["market_value"] for e in give_evals) + net_robux_give
            recv_mkt = sum(e["market_value"] for e in recv_evals) + net_robux_recv
            risk_rate = {"star": 0.01, "max": 0.04, "hume": 0.0, "legacy": 0.0}.get(m_clean, 0.0)
            risk_edge = round(give_val * risk_rate) if len(give_items) == 1 and len(receive_items) == 1 else 0.0
            retrade_room = (recv_mkt - give_mkt) - risk_edge

        for it, ev in zip(give_items, give_evals):
            adj = int(round(ev["effective_op"]))
            mkt = int(round(ev["market_value"]))
            extra = [x for x in (ev.get("best_price_note"), ev.get("hoard_note")) if x]
            extra_str = (" • " + " • ".join(extra)) if extra else ""

            if m_clean == "star":
                factors.append(
                    {
                        "good": (-adj) >= 0,
                        "title": f"[Star Dynamic] {it['name']}",
                        "delta": -adj,
                        "text": f"**[Star Dynamic — Offer] {it['name']}**: Real Value **`{mkt:,}`** (`{it['value']:,}` Base • {ev['source_note']}{extra_str})",
                    }
                )
            elif m_clean == "max":
                factors.append(
                    {
                        "good": (-adj) >= 0,
                        "title": f"[Max Strict] {it['name']}",
                        "delta": -adj,
                        "text": f"**[Max Strict Floor — Offer] {it['name']}**: Verified Floor **`{mkt:,}`** (Liquidity `{int(ev['liquidity']*100)}%` • {ev['source_note']})",
                    }
                )
            elif m_clean == "legacy":
                factors.append(
                    {
                        "good": (-adj) >= 0,
                        "title": f"[Legacy] {it['name']}",
                        "delta": -adj,
                        "text": f"**[Legacy Demand/Proof — Offer] {it['name']}**: Market Level **`{mkt:,}`** ({ev['source_note']})",
                    }
                )
            else:
                if not (any_semi_or_lowcopy_recv and len(give_items) == 1 and len(receive_items) == 1) and adj != 0:
                    factors.append(
                        {
                            "good": (-adj) > 0,
                            "title": it["name"],
                            "delta": -adj,
                            "text": f"**{it['name']}** commands **{mkt:,}** real value ({ev['source_note']}{extra_str})",
                        }
                    )
                elif extra:
                    factors.append(
                        {
                            "good": True,
                            "title": it["name"],
                            "delta": 0,
                            "text": f"**{it['name']}**: {' • '.join(extra)}",
                        }
                    )
            ledger.append(
                {
                    "step": it["name"],
                    "give": f"{it['value']:,} -> {mkt:,}",
                    "receive": "-",
                    "delta": f"{-adj:+,}",
                    "reason": f"{ev['source_note']}{extra_str}",
                }
            )

        for it, ev in zip(receive_items, recv_evals):
            adj = int(round(ev["effective_op"]))
            mkt = int(round(ev["market_value"]))
            extra = [x for x in (ev.get("best_price_note"), ev.get("hoard_note")) if x]
            extra_str = (" • " + " • ".join(extra)) if extra else ""

            if m_clean == "star":
                factors.append(
                    {
                        "good": adj >= 0,
                        "title": f"[Star Dynamic] {it['name']}",
                        "delta": adj,
                        "text": f"**[Star Dynamic — Receive] {it['name']}**: Real Value **`{mkt:,}`** (`{it['value']:,}` Base • {ev['source_note']}{extra_str})",
                    }
                )
            elif m_clean == "max":
                is_unverified_semi = ev["is_semi"] and ev["proof_count"] < 2
                liq_pen = 0
                if is_unverified_semi or ev["liquidity"] < 0.50:
                    liq_pen = int(round(max(give_val, it["value"]) * 0.14))
                    retrade_room -= liq_pen
                factors.append(
                    {
                        "good": (adj - liq_pen) > 0 and not is_unverified_semi,
                        "title": f"[Max Strict] {it['name']}",
                        "delta": adj - liq_pen,
                        "text": (
                            f"**[Max Strict — Receive] {it['name']}**: Haircut Value **`{mkt - liq_pen:,}`** "
                            f"({ev['source_note']} • Liquidity `{int(ev['liquidity']*100)}%`"
                            + (f" • **-{format_k(liq_pen)} strict illiquidity/low-proof haircut**" if liq_pen > 0 else "")
                            + ")"
                        ),
                    }
                )
            elif m_clean == "legacy":
                factors.append(
                    {
                        "good": adj >= 0,
                        "title": f"[Legacy] {it['name']}",
                        "delta": adj,
                        "text": f"**[Legacy Demand/Proof — Receive] {it['name']}**: Raw Target **`{mkt:,}`** ({ev['source_note']})",
                    }
                )
            else:
                if ev["is_semi"] and not it.get("is_valued", False):
                    factors.append(
                        {
                            "good": True,
                            "title": it["name"],
                            "delta": adj,
                            "text": f"**{it['name']}** is {ev['source_note']}{extra_str}",
                        }
                    )
                elif adj != 0 or ev["is_projected"]:
                    factors.append(
                        {
                            "good": adj > 0,
                            "title": it["name"],
                            "delta": adj,
                            "text": f"**{it['name']}** commands **{mkt:,}** real value ({ev['source_note']}{extra_str})",
                        }
                    )
                elif extra:
                    factors.append(
                        {
                            "good": True,
                            "title": it["name"],
                            "delta": 0,
                            "text": f"**{it['name']}**: {' • '.join(extra)}",
                        }
                    )
            ledger.append(
                {
                    "step": it["name"],
                    "give": "-",
                    "receive": f"{it['value']:,} -> {mkt:,}",
                    "delta": f"{adj:+,}",
                    "reason": f"{ev['source_note']}{extra_str}",
                }
            )

        if risk_edge > 0:
            if m_clean == "max":
                factors.append(
                    {
                        "good": False,
                        "title": "Strict Capital Buffer",
                        "delta": -int(risk_edge),
                        "text": f"**[Max 5.1 Strict Buffer]**: Requires **+{format_k(risk_edge)}** (`{risk_rate*100:.0f}%`) verified safety margin before locking liquid value into a slow hold",
                    }
                )
            elif m_clean == "star":
                factors.append(
                    {
                        "good": retrade_room > 0,
                        "title": "Dynamic Velocity & Hold Adjustment",
                        "delta": -int(risk_edge),
                        "text": f"**[Star 1.1 Dynamic Friction]**: Deducts **-{format_k(risk_edge)}** (`{risk_rate*100:.1f}%`) for hold velocity; net dynamic retrade edge is **`{int(round(retrade_room)):+,}`**",
                    }
                )
            elif m_clean == "legacy":
                factors.append(
                    {
                        "good": retrade_room > 0,
                        "title": "Classic Swap Spread",
                        "delta": int(round(retrade_room)),
                        "text": f"**[Legacy 2.1 Spread]**: Raw proof/demand spread puts this trade at **`{int(round(retrade_room)):+,}`** overpay advantage",
                    }
                )
            else:
                factors.append(
                    {
                        "good": True,
                        "title": "1-for-1 Hold Margin",
                        "delta": -int(risk_edge),
                        "text": f"On a 1 for 1 like this, you still want at least about **+{format_k(risk_edge)}** of real edge after risk",
                    }
                )
                factors.append(
                    {
                        "good": retrade_room > 0,
                        "title": "Likely Retrade Room",
                        "delta": int(round(retrade_room)),
                        "text": f"After pricing in the likely retrade room, this looks closer to **+{format_k(retrade_room)}** (hold-and-retrade play rather than a quick flip)",
                    }
                )

        structure_bonus = 0.0
        max_give_single = max((int(x.get("value", 0)) for x in give_items), default=0)
        max_recv_single = max((int(x.get("value", 0)) for x in receive_items), default=0)

        if len(give_items) > len(receive_items) and max_recv_single > max_give_single:
            avg_recv_liq = sum(e["liquidity"] for e in recv_evals) / max(1, len(recv_evals))
            upgrade_rate = 0.01 if any_dropping_recv else (0.03 + 0.04 * avg_recv_liq)
            structure_bonus = recv_val * upgrade_rate
            struct_reason = (
                f"Consolidating {len(give_items)} items into **{receive_items[0]['name']}** saves **+{int(round(structure_bonus)):,}** (~{upgrade_rate*100:.1f}%) in upgrade OP"
            )
            factors.append(
                {
                    "good": True,
                    "title": "Upgrade Consolidation",
                    "delta": int(round(structure_bonus)),
                    "text": struct_reason,
                }
            )
            retrade_room += structure_bonus
            ledger.append(
                {
                    "step": "Upgrade Consolidation",
                    "give": f"{len(give_items)} items",
                    "receive": f"{len(receive_items)} item",
                    "delta": f"{int(round(structure_bonus)):+,}",
                    "reason": struct_reason,
                }
            )
        elif len(receive_items) > len(give_items) and max_give_single > max_recv_single:
            avg_give_liq = sum(e["liquidity"] for e in give_evals) / max(1, len(give_evals))
            avg_recv_liq = sum(e["liquidity"] for e in recv_evals) / max(1, len(recv_evals))
            all_recv_rare_low = all(
                e["is_rare"] and int(x.get("demand", -1)) <= 1
                for x, e in zip(receive_items, recv_evals)
            )
            if all_recv_rare_low:
                downgrade_cost = give_val * 0.18 + recv_val * 0.08
                struct_reason = (
                    f"Splitting **{give_items[0]['name']}** into {len(receive_items)} Terrible/Low-demand Rares requires ~**{int(round(downgrade_cost)):,}** OP"
                )
            else:
                unvalued_recv_count = sum(1 for x in receive_items if not x.get("is_valued", True))
                rate = 0.04 + (0.035 * avg_give_liq) + max(0.0, 0.035 * (1.0 - avg_recv_liq)) + (0.025 * unvalued_recv_count)
                downgrade_cost = give_val * rate
                struct_reason = (
                    f"Downgrading into {len(receive_items)} smaller items requires ~**{int(round(downgrade_cost)):,}** ({rate*100:.1f}%) overpay to re-upgrade"
                )
            structure_bonus = -downgrade_cost
            factors.append(
                {
                    "good": False,
                    "title": "Downgrade Cost",
                    "delta": int(round(structure_bonus)),
                    "text": struct_reason,
                }
            )
            retrade_room += structure_bonus
            ledger.append(
                {
                    "step": "Downgrade Cost",
                    "give": f"{len(give_items)} item",
                    "receive": f"{len(receive_items)} items",
                    "delta": f"{int(round(structure_bonus)):+,}",
                    "reason": struct_reason,
                }
            )

        ref_val = max(give_val, recv_val, 1)
        edge_pct = (retrade_room / ref_val) * 100.0

        if any_proj_recv and retrade_room < 0:
            verdict = "Not Worth It (Projected Warning)"
            color = "red"
        elif any_dropping_recv and retrade_room < ref_val * 0.03:
            verdict = "Not Worth It (Dropping Item)"
            color = "red"
        elif any_semi_or_lowcopy_recv and retrade_room > 0 and m_clean != "max":
            if m_clean == "hume":
                verdict = "Fair Swap" if raw_val_op < 0 else "Strong Win"
            elif m_clean == "star":
                verdict = "Win (Hold & Retrade)" if edge_pct >= 6.0 else "Fair Swap"
            else:
                verdict = "Strong Win" if edge_pct >= 5.0 else "Win"
            color = "green"
        elif edge_pct >= 6.5:
            verdict = "Strong Win"
            color = "green"
        elif edge_pct >= 2.0:
            verdict = "Win"
            color = "green"
        elif edge_pct >= -2.0:
            verdict = "Fair Swap"
            color = "green"
        elif edge_pct >= -5.0:
            verdict = "Slight Loss"
            color = "yellow"
        else:
            verdict = "Not Worth It"
            color = "red"

        for f in factors:
            bullets.append(f"{'🟢' if f['good'] else '🔴'} {f['text']}")

        ledger.append(
            {
                "step": "Net Retrade Room",
                "give": f"{int(round(give_mkt)):,}",
                "receive": f"{int(round(recv_mkt + structure_bonus - risk_edge)):,}",
                "delta": f"{int(round(retrade_room)):+,} ({edge_pct:+.1f}%)",
                "reason": f"{model_name} ({model_desc}) -> {verdict}",
            }
        )

        return {
            "engine": model_name,
            "engine_desc": model_desc,
            "verdict": verdict,
            "color": color,
            "give_items": give_items,
            "receive_items": receive_items,
            "give_robux": g_robux,
            "receive_robux": r_robux,
            "give_robux_net": net_robux_give,
            "receive_robux_net": net_robux_recv,
            "give_value": give_val,
            "give_rap": give_rap,
            "receive_value": recv_val,
            "receive_rap": recv_rap,
            "op_value": raw_val_op,
            "op_rap": raw_rap_op,
            "retrade_room": int(round(retrade_room)),
            "factors": factors,
            "ledger": ledger,
            "bullets": bullets,
            "disclaimer": f"{model_name} ({model_desc})",
        }
