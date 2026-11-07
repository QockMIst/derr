import asyncio
import base64
import os
import sqlite3
import tempfile
import threading
import time
import tomllib
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from calibrate import seed_reference_calibration_data
from data.catalog import RobloxCatalogClient
from data.roblox import RobloxClient
from data.rolimons import RolimonsClient
from data.sales import SalesClient
from db import Database
from engine import TradeEngine
from hf_sync import HuggingFacePersistence
from oplookup import compute_op_lookup
from proofs import find_item_by_query, get_item_proofs_formatted
from render import render_trade_card_png
from sniper import AdSniper
from tracker.scanner import InventoryScanner
from tracker.seed import seed_traders_from_ads

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.toml"


def load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    return {}


CONFIG = load_config()
db = Database(BASE_DIR / "void_data.sqlite3")
hf_sync = HuggingFacePersistence(BASE_DIR / "void_data.sqlite3", interval_seconds=600)
rolimons = RolimonsClient(cache_ttl=300)
roblox = RobloxClient(backoff_seconds=5.0)
catalog_client = RobloxCatalogClient()
sales_client = SalesClient()

sniper_cfg = CONFIG.get("sniper", {})
sniper = AdSniper(
    min_deal_percent=float(sniper_cfg.get("min_deal_percent", 5.0)),
    cooldown_seconds=int(sniper_cfg.get("cooldown_seconds", 1800)),
    filter_projecteds=bool(sniper_cfg.get("filter_projecteds", True)),
    filter_tag_only_requests=bool(sniper_cfg.get("filter_tag_only_requests", True)),
)

tracker_cfg = CONFIG.get("tracker", {})
scanner = InventoryScanner(
    db=db,
    rolimons=rolimons,
    roblox=roblox,
    match_window_seconds=int(tracker_cfg.get("match_window_seconds", 7200)),
)

engine_cfg = CONFIG.get("engine", {})
trade_engine = TradeEngine(
    db=db,
    sales_client=sales_client,
    risk_edge_1v1_rate=float(engine_cfg.get("risk_edge_1v1_rate", 0.05)),
)

ITEMS_CATALOG: Dict[int, Dict[str, Any]] = db.get_all_items()
RECENT_RAW_ADS: List[Dict[str, Any]] = []
SNIPED_DEALS: List[Dict[str, Any]] = []
SCANNER_STATUS: Dict[str, Any] = {
    "running": False,
    "last_scanned_user": None,
    "total_scans": 0,
    "moves_detected": 0,
    "trades_matched": 0,
    "catalog_crawl_done": False,
    "github_synced_at": 0,
}
_STOP_EVENT = threading.Event()


def refresh_items_catalog(force: bool = False) -> Dict[int, Dict[str, Any]]:
    global ITEMS_CATALOG
    try:
        fresh = rolimons.fetch_items(force=force)
        if fresh:
            db.upsert_items(fresh)
    except Exception:
        pass
    latest = db.get_all_items()
    ITEMS_CATALOG.clear()
    ITEMS_CATALOG.update(latest)
    return ITEMS_CATALOG


def poll_rolimons_ads_once() -> int:
    global RECENT_RAW_ADS, SNIPED_DEALS
    try:
        ads = rolimons.fetch_recent_ads()
    except Exception:
        return 0
    if not ads:
        return 0

    RECENT_RAW_ADS.clear()
    RECENT_RAW_ADS.extend(ads)
    seed_traders_from_ads(db, ads)

    try:
        sale_acts = rolimons.fetch_sale_activity()
        if sale_acts:
            db.record_live_sales(sale_acts)
    except Exception:
        pass

    new_deals = []
    for ad in ads:
        deal = sniper.evaluate_ad(ad, ITEMS_CATALOG)
        if deal:
            new_deals.append(deal)

    if new_deals:
        all_ids = []
        for d in new_deals:
            all_ids.extend(x["item_id"] for x in d["offer_items"])
            all_ids.extend(x["item_id"] for x in d["request_items"])
        thumbs = roblox.fetch_thumbnails(all_ids)
        if thumbs:
            db.update_thumbnails(thumbs)
            for iid, url in thumbs.items():
                if iid in ITEMS_CATALOG:
                    ITEMS_CATALOG[iid]["thumbnail_url"] = url
            for d in new_deals:
                for x in d["offer_items"] + d["request_items"]:
                    if x["item_id"] in thumbs:
                        x["thumbnail_url"] = thumbs[x["item_id"]]

        existing_ids = {d["ad_id"] for d in SNIPED_DEALS}
        for d in reversed(new_deals):
            if d["ad_id"] not in existing_ids:
                SNIPED_DEALS.insert(0, d)
        del SNIPED_DEALS[120:]

    return len(new_deals)


def sync_db_from_github_if_configured() -> int:
    url = str(tracker_cfg.get("github_db_raw_url", "")).strip()
    token = str(tracker_cfg.get("github_token", "")).strip()
    if not url or not url.startswith("http"):
        return 0

    sep = "&" if "?" in url else "?"
    bust_url = f"{url}{sep}_t={int(time.time())}"
    tmp_path: Optional[Path] = None
    merged_count = 0
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}
        if token:
            headers["Authorization"] = f"token {token}"
            if "raw.githubusercontent.com" in url:
                headers["Accept"] = "application/vnd.github.v3.raw"
        req = urllib.request.Request(bust_url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw_bytes = resp.read()
        if len(raw_bytes) < 4096 or not raw_bytes.startswith(b"SQLite format 3"):
            return 0

        with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3") as tmp:
            tmp.write(raw_bytes)
            tmp_path = Path(tmp.name)

        remote_conn = sqlite3.connect(str(tmp_path))
        remote_conn.row_factory = sqlite3.Row
        try:
            remote_trades = remote_conn.execute(
                "SELECT * FROM trades ORDER BY detected_at ASC"
            ).fetchall()
        except Exception:
            remote_trades = []
        try:
            remote_moves = remote_conn.execute(
                "SELECT * FROM uaid_moves ORDER BY detected_at ASC"
            ).fetchall()
        except Exception:
            remote_moves = []
        try:
            remote_traders = remote_conn.execute(
                "SELECT * FROM traders"
            ).fetchall()
        except Exception:
            remote_traders = []
        remote_conn.close()

        if remote_traders:
            for tr in remote_traders:
                if tr["username"] and not str(tr["username"]).startswith("User_"):
                    db.upsert_trader(int(tr["user_id"]), str(tr["username"]), int(tr["last_ad_at"]))

        existing_sigs = set()
        for t in db.get_recent_trades(limit=2000):
            existing_sigs.add((int(t["user_a_id"]), int(t["user_b_id"]), int(t["detected_at"])))
            existing_sigs.add((int(t["user_b_id"]), int(t["user_a_id"]), int(t["detected_at"])))

        for rt in remote_trades:
            ua_id = int(rt["user_a_id"])
            ub_id = int(rt["user_b_id"])
            det_at = int(rt["detected_at"])
            if (ua_id, ub_id, det_at) in existing_sigs:
                continue
            import json as _json

            items_a_list = _json.loads(rt["items_a_json"])
            items_b_list = _json.loads(rt["items_b_json"])
            if not items_a_list or not items_b_list:
                continue
            if any(int(x.get("item_id", 0)) <= 0 for x in items_a_list + items_b_list):
                continue
            uaids_a = {int(x.get("uaid", -1)) for x in items_a_list if x.get("uaid")}
            uaids_b = {int(x.get("uaid", -2)) for x in items_b_list if x.get("uaid")}
            if uaids_a & uaids_b:
                continue
            if {x.get("item_id") for x in items_a_list} == {x.get("item_id") for x in items_b_list} and len(items_a_list) == len(items_b_list):
                continue

            ua_name = str(rt["user_a_name"])
            if not ua_name or ua_name.startswith("User_"):
                ua_name = db.get_trader_name(ua_id)
            ub_name = str(rt["user_b_name"])
            if not ub_name or ub_name.startswith("User_"):
                ub_name = db.get_trader_name(ub_id)

            db.insert_trade(
                user_a_id=ua_id,
                user_a_name=ua_name,
                user_b_id=ub_id,
                user_b_name=ub_name,
                items_a=items_a_list,
                items_b=items_b_list,
                value_a=int(rt["value_a"]),
                rap_a=int(rt["rap_a"]),
                value_b=int(rt["value_b"]),
                rap_b=int(rt["rap_b"]),
                op_value=int(rt["op_value"]),
                op_rap=int(rt["op_rap"]),
                trade_type=str(rt["trade_type"]),
                detected_at=det_at,
            )
            existing_sigs.add((ua_id, ub_id, det_at))
            existing_sigs.add((ub_id, ua_id, det_at))
            merged_count += 1

        if remote_moves:
            with db._connect() as lconn:
                existing_mv = {
                    (int(r["uaid"]), int(r["from_user_id"]), int(r["to_user_id"]))
                    for r in lconn.execute("SELECT uaid, from_user_id, to_user_id FROM uaid_moves").fetchall()
                }
                new_mvs = [
                    (int(m["uaid"]), int(m["item_id"]), int(m["from_user_id"]), int(m["to_user_id"]), int(m["detected_at"]))
                    for m in remote_moves
                    if (int(m["uaid"]), int(m["from_user_id"]), int(m["to_user_id"])) not in existing_mv
                ]
                if new_mvs:
                    lconn.executemany(
                        "INSERT INTO uaid_moves (uaid, item_id, from_user_id, to_user_id, detected_at) VALUES (?, ?, ?, ?, ?)",
                        new_mvs,
                    )
            from tracker.matcher import match_moves_into_trades
            match_moves_into_trades(db, ITEMS_CATALOG, match_window_seconds=86400)

        SCANNER_STATUS["github_synced_at"] = int(time.time())
    except Exception:
        pass
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
    return merged_count


def _background_worker():
    SCANNER_STATUS["running"] = True
    last_ad_poll = 0.0
    last_item_poll = time.time()
    last_gh_sync = 0.0
    last_vr_poll = 0.0
    seen_vr_msgs: set[str] = set()
    ad_interval = float(sniper_cfg.get("poll_interval_seconds", 20))
    scan_interval = float(tracker_cfg.get("scan_interval_seconds", 2.0))

    def predict_op_callback(side_a_ids: List[int], side_b_ids: List[int]) -> Optional[int]:
        res = trade_engine.analyze_trade(side_a_ids, side_b_ids, ITEMS_CATALOG, engine_mode="star")
        return int(res.get("retrade_room", 0))

    while not _STOP_EVENT.is_set():
        now = time.time()
        if now - last_item_poll >= 300:
            refresh_items_catalog(force=True)
            last_item_poll = now

        if now - last_ad_poll >= ad_interval:
            poll_rolimons_ads_once()
            last_ad_poll = now

        if now - last_gh_sync >= 45:
            sync_db_from_github_if_configured()
            last_gh_sync = now

        if now - last_vr_poll >= 20:
            last_vr_poll = now
            try:
                from value_requests import poll_discord_value_requests_channel
                disc_cfg = CONFIG.get("discord", {})
                vr_chan = os.environ.get("VALUE_REQUESTS_CHANNEL_ID", "").strip() or str(
                    disc_cfg.get("value_requests_channel_id", "")
                ).strip()
                vr_tok = os.environ.get("DISCORD_SCANNER_TOKEN", "").strip() or str(
                    disc_cfg.get("scanner_token", "")
                ).strip()
                if vr_chan and vr_tok:
                    poll_discord_value_requests_channel(db, ITEMS_CATALOG, vr_chan, vr_tok, seen_vr_msgs)
            except Exception:
                pass

        try:
            for _ in range(3):
                step = scanner.scan_one_step(ITEMS_CATALOG, predict_op_fn=predict_op_callback)
                if step["scanned_user"] is not None:
                    SCANNER_STATUS["last_scanned_user"] = step["scanned_user"]
                    SCANNER_STATUS["total_scans"] += 1
                    SCANNER_STATUS["moves_detected"] += step["moves"]
                    SCANNER_STATUS["trades_matched"] += len(step["new_trades"])
        except Exception:
            pass

        _STOP_EVENT.wait(scan_interval)


def _background_catalog_crawler():
    def _on_batch(new_items: List[Dict[str, Any]]) -> None:
        batch_dict = {it["item_id"]: it for it in new_items}
        db.upsert_items(batch_dict)
        ITEMS_CATALOG.update(batch_dict)

    try:
        catalog_client.crawl_all_roblox_limiteds(
            existing_catalog=ITEMS_CATALOG,
            on_batch_callback=_on_batch,
            max_pages=80,
            page_delay=1.8,
        )
    except Exception:
        pass
    SCANNER_STATUS["catalog_crawl_done"] = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    hf_sync.restore_if_available()
    refresh_items_catalog(force=True)
    seed_reference_calibration_data(db, ITEMS_CATALOG)
    sync_db_from_github_if_configured()
    poll_rolimons_ads_once()

    _STOP_EVENT.clear()
    worker = threading.Thread(target=_background_worker, daemon=True)
    worker.start()

    crawler = threading.Thread(target=_background_catalog_crawler, daemon=True)
    crawler.start()

    discord_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip() or str(
        CONFIG.get("discord", {}).get("bot_token", "")
    ).strip()
    bot_task = None
    if discord_token:
        try:
            from bot import client as discord_client
            bot_task = asyncio.create_task(discord_client.start(discord_token))
        except Exception as exc:
            print(f"[Carbon Discord App] Startup error: {exc}")

    hf_sync.start_periodic_backup(_STOP_EVENT)
    yield
    _STOP_EVENT.set()
    if bot_task is not None:
        try:
            from bot import client as discord_client
            await discord_client.close()
        except Exception:
            pass
    hf_sync.backup_once()


app = FastAPI(title="Carbon", lifespan=lifespan)

WEB_DIR = BASE_DIR / "web"
WEB_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


class TradeCheckRequest(BaseModel):
    give: List[Any] = []
    receive: List[Any] = []
    give_robux: int = 0
    receive_robux: int = 0
    engine: str = "v4"


class ManualProofRequest(BaseModel):
    user_a_name: str = "TraderA"
    user_b_name: str = "TraderB"
    items_a: List[Any]
    items_b: List[Any]


def _resolve_item_inputs(raw_list: List[Any]) -> List[int]:
    resolved: List[int] = []
    for entry in raw_list[:4]:
        if isinstance(entry, int) or (isinstance(entry, str) and entry.strip().isdigit()):
            iid = int(entry)
            if iid not in ITEMS_CATALOG:
                fetched = catalog_client.fetch_single_asset(iid)
                if fetched:
                    db.upsert_items({iid: fetched})
                    ITEMS_CATALOG[iid] = fetched
            if iid in ITEMS_CATALOG:
                resolved.append(iid)
                continue
        item = find_item_by_query(str(entry), ITEMS_CATALOG)
        if not item:
            live_matches = catalog_client.search_keyword(str(entry), ITEMS_CATALOG)
            if live_matches:
                new_map = {x["item_id"]: x for x in live_matches}
                db.upsert_items(new_map)
                ITEMS_CATALOG.update(new_map)
                item = live_matches[0]
        if item:
            resolved.append(int(item["item_id"]))
    return resolved


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = WEB_DIR / "index.html"
    return html_path.read_text(encoding="utf-8")


@app.get("/api/status")
def api_status():
    stats = db.get_stats()
    return {
        "status": "online",
        "database": stats,
        "scanner": SCANNER_STATUS,
        "sniped_deals_cached": len(SNIPED_DEALS),
    }


@app.get("/api/items/search")
def api_search_items(q: str = "", limit: int = 20):
    query = q.strip().lower()
    if not query:
        top = sorted(ITEMS_CATALOG.values(), key=lambda x: x["value"], reverse=True)[:limit]
        return {"items": top}

    exact = []
    partial = []
    for it in ITEMS_CATALOG.values():
        acr = it.get("acronym", "").lower()
        name = it["name"].lower()
        if query == str(it["item_id"]) or query == acr or query == name:
            exact.append(it)
        elif query in name or (acr and query in acr):
            partial.append(it)

    partial.sort(key=lambda x: (-int(x.get("is_valued", False)), -x["value"], len(x["name"])))
    results = (exact + partial)[:limit]

    if len(results) < 5 and len(query) >= 2:
        live = catalog_client.search_keyword(q, ITEMS_CATALOG)
        if live:
            new_only = {x["item_id"]: x for x in live if x["item_id"] not in ITEMS_CATALOG}
            if new_only:
                db.upsert_items(new_only)
                ITEMS_CATALOG.update(new_only)
            seen_ids = {r["item_id"] for r in results}
            for x in live:
                if x["item_id"] not in seen_ids:
                    results.append(x)
                    seen_ids.add(x["item_id"])

    missing_thumbs = [x["item_id"] for x in results[:20] if not x.get("thumbnail_url")]
    if missing_thumbs:
        thumbs = roblox.fetch_thumbnails(missing_thumbs)
        if thumbs:
            db.update_thumbnails(thumbs)
            for iid, url in thumbs.items():
                if iid in ITEMS_CATALOG:
                    ITEMS_CATALOG[iid]["thumbnail_url"] = url
            for x in results:
                if x["item_id"] in thumbs:
                    x["thumbnail_url"] = thumbs[x["item_id"]]

    return {"items": results[:limit]}


@app.get("/api/sniper")
def api_sniper(
    bucket: str = "ALL",
    min_deal: float = 5.0,
    sends: str = "",
    wants: str = "",
):
    filtered = AdSniper.filter_deals(
        SNIPED_DEALS,
        bucket=bucket,
        min_deal=min_deal,
        sends_query=sends,
        wants_query=wants,
    )
    return {"deals": filtered[:50]}


@app.post("/api/sniper/refresh")
def api_sniper_refresh():
    added = poll_rolimons_ads_once()
    return {"added": added, "total": len(SNIPED_DEALS)}


@app.post("/api/tradecheck")
def api_tradecheck(req: TradeCheckRequest):
    give_ids = _resolve_item_inputs(req.give)
    recv_ids = _resolve_item_inputs(req.receive)
    if (not give_ids and req.give_robux <= 0) or (not recv_ids and req.receive_robux <= 0):
        raise HTTPException(
            status_code=400,
            detail="Add at least one valid item or Robux amount on both sides.",
        )

    thumbs = roblox.fetch_thumbnails(give_ids + recv_ids)
    if thumbs:
        db.update_thumbnails(thumbs)
        for iid, url in thumbs.items():
            if iid in ITEMS_CATALOG:
                ITEMS_CATALOG[iid]["thumbnail_url"] = url

    analysis = trade_engine.analyze_trade(
        give_ids,
        recv_ids,
        ITEMS_CATALOG,
        engine_mode=req.engine,
        recent_ads=RECENT_RAW_ADS,
        give_robux=req.give_robux,
        receive_robux=req.receive_robux,
    )

    png_bytes = render_trade_card_png(analysis["give_items"], analysis["receive_items"])
    analysis["card_png_base64"] = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return analysis


@app.get("/api/proofget")
def api_proofget(item: str, mode: str = "any"):
    found = find_item_by_query(item, ITEMS_CATALOG)
    if not found:
        live = catalog_client.search_keyword(item, ITEMS_CATALOG)
        if live:
            new_map = {x["item_id"]: x for x in live}
            db.upsert_items(new_map)
            ITEMS_CATALOG.update(new_map)
            found = live[0]
    if not found:
        raise HTTPException(status_code=404, detail=f"Item '{item}' not found.")

    if not found.get("thumbnail_url"):
        t_map = roblox.fetch_thumbnails([found["item_id"]])
        if found["item_id"] in t_map:
            found["thumbnail_url"] = t_map[found["item_id"]]
            db.update_thumbnails(t_map)

    res = get_item_proofs_formatted(
        db,
        found["item_id"],
        ITEMS_CATALOG,
        limit=5,
        mode=mode,
        sales_client=sales_client,
    )
    for card in res["proofs"]:
        all_ids = [x["item_id"] for x in card["items_a"] + card["items_b"]]
        thumbs = roblox.fetch_thumbnails(all_ids)
        for x in card["items_a"] + card["items_b"]:
            if x["item_id"] in thumbs:
                x["thumbnail_url"] = thumbs[x["item_id"]]
        png = render_trade_card_png(card["items_a"], card["items_b"])
        card["card_png_base64"] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    return res


@app.get("/api/oplookup")
def api_oplookup(item: str, days: int = 30):
    found = find_item_by_query(item, ITEMS_CATALOG)
    if not found:
        live = catalog_client.search_keyword(item, ITEMS_CATALOG)
        if live:
            new_map = {x["item_id"]: x for x in live}
            db.upsert_items(new_map)
            ITEMS_CATALOG.update(new_map)
            found = live[0]
    if not found:
        raise HTTPException(status_code=404, detail=f"Item '{item}' not found.")

    if not found.get("thumbnail_url"):
        t_map = roblox.fetch_thumbnails([found["item_id"]])
        if found["item_id"] in t_map:
            found["thumbnail_url"] = t_map[found["item_id"]]
            db.update_thumbnails(t_map)

    return compute_op_lookup(
        db=db,
        item_id=found["item_id"],
        items_catalog=ITEMS_CATALOG,
        recent_ads=RECENT_RAW_ADS,
        sales_client=sales_client,
        window_days=days,
    )


@app.post("/api/proofs/manual")
def api_add_manual_proof(req: ManualProofRequest):
    ids_a = _resolve_item_inputs(req.items_a)
    ids_b = _resolve_item_inputs(req.items_b)
    if not ids_a or not ids_b:
        raise HTTPException(status_code=400, detail="Both sides need at least 1 valid item.")

    items_a = [ITEMS_CATALOG[i] for i in ids_a]
    items_b = [ITEMS_CATALOG[i] for i in ids_b]
    val_a = sum(x["value"] for x in items_a)
    rap_a = sum(x["rap"] for x in items_a)
    val_b = sum(x["value"] for x in items_b)
    rap_b = sum(x["rap"] for x in items_b)

    trade_type = "SWAP"
    if len(items_a) > len(items_b):
        trade_type = "UPGRADE"
    elif len(items_b) > len(items_a):
        trade_type = "DOWNGRADE"

    pred = trade_engine.analyze_trade(ids_a, ids_b, ITEMS_CATALOG, engine_mode="v3.1")
    tid = db.insert_trade(
        user_a_id=999001,
        user_a_name=req.user_a_name,
        user_b_id=999002,
        user_b_name=req.user_b_name,
        items_a=items_a,
        items_b=items_b,
        value_a=val_a,
        rap_a=rap_a,
        value_b=val_b,
        rap_b=rap_b,
        op_value=abs(val_a - val_b),
        op_rap=abs(rap_a - rap_b),
        trade_type=trade_type,
        predicted_op=int(pred.get("retrade_room", 0)),
    )
    return {"trade_id": tid, "status": "saved"}


@app.get("/api/export/db")
def api_export_db():
    db_file = BASE_DIR / "void_data.sqlite3"
    if not db_file.exists():
        raise HTTPException(status_code=404, detail="Database file not found")
    return FileResponse(
        path=str(db_file),
        filename="void_data.sqlite3",
        media_type="application/x-sqlite3",
    )


@app.get("/api/card.png")
def api_render_card(offer: str = "", request: str = ""):
    offer_ids = _resolve_item_inputs([x for x in offer.split(",") if x.strip()])
    req_ids = _resolve_item_inputs([x for x in request.split(",") if x.strip()])
    offer_items = [ITEMS_CATALOG[i] for i in offer_ids]
    req_items = [ITEMS_CATALOG[i] for i in req_ids]
    png = render_trade_card_png(offer_items, req_items)
    return Response(content=png, media_type="image/png")


def _free_port_on_windows(port: int) -> None:
    if os.name != "nt":
        return
    try:
        import subprocess
        out = subprocess.check_output(
            f'netstat -aon | findstr :{port} | findstr LISTENING',
            shell=True,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        my_pid = os.getpid()
        for line in out.splitlines():
            parts = line.strip().split()
            if parts:
                pid_str = parts[-1]
                if pid_str.isdigit() and int(pid_str) != my_pid:
                    subprocess.run(
                        f"taskkill /F /PID {pid_str}",
                        shell=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
        time.sleep(0.5)
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn

    srv = CONFIG.get("server", {})
    host = os.environ.get("HOST", str(srv.get("host", "127.0.0.1")))
    port = int(os.environ.get("PORT", srv.get("port", 8765)))
    _free_port_on_windows(port)
    uvicorn.run("app:app", host=host, port=port, reload=False)
