import os
import threading
import time
import tomllib
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Query, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
DB_PATH = BASE_DIR / "void_data.sqlite3"


def load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    return {}


def sync_db_from_github_if_configured() -> bool:
    """
    If `GITHUB_DB_RAW_URL` (e.g. https://raw.githubusercontent.com/<user>/<repo>/main/void_data.sqlite3)
    is set in config.toml or environment, downloads the latest cloud-scanned SQLite database
    if it has more proofs/UAIDs than the local file.
    """
    raw_url = os.environ.get(
        "GITHUB_DB_RAW_URL",
        str(cfg.get("tracker", {}).get("github_db_raw_url", "")).strip(),
    )
    if not raw_url:
        return False
    try:
        import urllib.request

        req = urllib.request.Request(raw_url, headers={"User-Agent": "VoidLocal/2.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        if len(data) > 4096 and data.startswith(b"SQLite format 3"):
            tmp_path = BASE_DIR / "void_cloud_tmp.sqlite3"
            tmp_path.write_bytes(data)
            cloud_db = Database(tmp_path)
            cloud_stats = cloud_db.get_stats()
            local_stats = db.get_stats() if DB_PATH.exists() else {"proofs": -1, "tracked_uaids": -1}
            if (
                cloud_stats["proofs"] > local_stats["proofs"]
                or cloud_stats["tracked_uaids"] > local_stats["tracked_uaids"]
            ):
                DB_PATH.write_bytes(data)
                tmp_path.unlink(missing_ok=True)
                return True
            tmp_path.unlink(missing_ok=True)
    except Exception:
        pass
    return False


cfg = load_config()
hf_sync = HuggingFacePersistence(DB_PATH)
hf_sync.restore_if_available()

db = Database(DB_PATH)
sync_db_from_github_if_configured()
rolimons = RolimonsClient(cache_ttl=300)
roblox = RobloxClient(backoff_seconds=5.0)
catalog_client = RobloxCatalogClient()
sales_client = SalesClient()

sniper = AdSniper(
    min_deal_percent=float(cfg.get("sniper", {}).get("min_deal_percent", 5.0)),
    cooldown_seconds=int(cfg.get("sniper", {}).get("cooldown_seconds", 1800)),
    filter_projecteds=bool(cfg.get("sniper", {}).get("filter_projecteds", True)),
    filter_tag_only_requests=bool(cfg.get("sniper", {}).get("filter_tag_only_requests", True)),
)
scanner = InventoryScanner(
    db=db,
    rolimons=rolimons,
    roblox=roblox,
    match_window_seconds=int(cfg.get("tracker", {}).get("match_window_seconds", 7200)),
)
engine = TradeEngine(
    db=db,
    sales_client=sales_client,
    risk_edge_1v1_rate=float(cfg.get("engine", {}).get("risk_edge_1v1_rate", 0.05)),
)

ITEMS_CATALOG: Dict[int, Dict[str, Any]] = {}
RECENT_ADS_CACHE: List[Dict[str, Any]] = []
SNIPED_DEALS: List[Dict[str, Any]] = []
_STOP_EVENT = threading.Event()


def ensure_thumbnails(item_ids: List[int]) -> None:
    missing = [
        iid
        for iid in item_ids
        if iid in ITEMS_CATALOG and not ITEMS_CATALOG[iid].get("thumbnail_url")
    ]
    if not missing:
        return
    thumbs = roblox.fetch_thumbnails(missing)
    if thumbs:
        for iid, url in thumbs.items():
            if iid in ITEMS_CATALOG:
                ITEMS_CATALOG[iid]["thumbnail_url"] = url
        db.update_thumbnails(thumbs)


def refresh_market_and_ads() -> int:
    global ITEMS_CATALOG, RECENT_ADS_CACHE
    fetched = rolimons.fetch_items()
    if fetched:
        for iid, meta in fetched.items():
            meta["source"] = "rolimons"
            old = ITEMS_CATALOG.get(iid)
            if old:
                if old.get("thumbnail_url"):
                    meta["thumbnail_url"] = old["thumbnail_url"]
                if old.get("manual_value") is not None:
                    meta["manual_value"] = old["manual_value"]
                    meta["value"] = int(old["manual_value"])
        db.upsert_items(fetched)
        ITEMS_CATALOG = db.get_all_items()

    ads = rolimons.fetch_recent_ads()
    if ads:
        RECENT_ADS_CACHE = ads
        seed_traders_from_ads(db, ads)

    sale_acts = rolimons.fetch_sale_activity()
    if sale_acts:
        db.record_live_sales(sale_acts)

    new_hits = 0
    for ad in ads:
        deal = sniper.evaluate_ad(ad, ITEMS_CATALOG)
        if deal:
            all_ids = [x["item_id"] for x in deal["offer_items"] + deal["request_items"]]
            ensure_thumbnails(all_ids)
            SNIPED_DEALS.insert(0, deal)
            new_hits += 1

    del SNIPED_DEALS[200:]
    return new_hits


def _save_discovered_roblox_items(batch: List[Dict[str, Any]]) -> None:
    if not batch:
        return
    batch_map = {it["item_id"]: it for it in batch}
    db.upsert_items(batch_map)
    thumbs = roblox.fetch_thumbnails(list(batch_map.keys()))
    if thumbs:
        db.update_thumbnails(thumbs)
    for iid, it in batch_map.items():
        if thumbs and iid in thumbs:
            it["thumbnail_url"] = thumbs[iid]
        ITEMS_CATALOG[iid] = it


def _catalog_crawler_worker() -> None:
    """
    Crawls Roblox Catalog v2 (CreatorTargetId=1, SalesTypeFilter=2) in the background
    so every Roblox-made limited/collectible (including Oozing Oscar, etc.) is indexed
    into the local database without any UGC items.
    """
    try:
        catalog_client.crawl_all_roblox_limiteds(
            existing_catalog=ITEMS_CATALOG,
            on_batch_callback=_save_discovered_roblox_items,
            max_pages=80,
            page_delay=1.8,
        )
    except Exception:
        pass


def _background_worker() -> None:
    last_ad_poll = 0.0
    poll_interval = float(cfg.get("sniper", {}).get("poll_interval_seconds", 20))
    scan_interval = float(cfg.get("tracker", {}).get("scan_interval_seconds", 2.0))

    while not _STOP_EVENT.is_set():
        now = time.time()
        if now - last_ad_poll >= poll_interval:
            try:
                refresh_market_and_ads()
            except Exception:
                pass
            last_ad_poll = now

        try:
            if ITEMS_CATALOG:
                scanner.scan_one_step(ITEMS_CATALOG)
        except Exception:
            pass

        _STOP_EVENT.wait(scan_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ITEMS_CATALOG
    ITEMS_CATALOG = db.get_all_items()
    try:
        refresh_market_and_ads()
    except Exception:
        pass

    # Ensure Oozing Oscar (20011925) is immediately present even before full crawl finishes
    if 20011925 not in ITEMS_CATALOG:
        oscar_list = catalog_client.search_keyword("Oozing Oscar", ITEMS_CATALOG)
        if oscar_list:
            _save_discovered_roblox_items(oscar_list)

    worker = threading.Thread(target=_background_worker, daemon=True)
    worker.start()

    crawler = threading.Thread(target=_catalog_crawler_worker, daemon=True)
    crawler.start()

    hf_sync.start_periodic_backup(_STOP_EVENT)
    yield
    _STOP_EVENT.set()


app = FastAPI(title="Void Local Market Suite", lifespan=lifespan)

fonts_path = BASE_DIR / "web" / "fonts"
fonts_path.mkdir(parents=True, exist_ok=True)
app.mount("/fonts", StaticFiles(directory=str(fonts_path)), name="fonts")


class TradeCheckRequest(BaseModel):
    give_ids: List[int]
    receive_ids: List[int]
    engine: str = "v3.1"


class ManualProofRequest(BaseModel):
    side_a: str
    side_b: str


class SetItemValueRequest(BaseModel):
    item_id: int
    value: Optional[int] = None


def resolve_or_discover_item(query: str) -> Optional[Dict[str, Any]]:
    it = find_item_by_query(query, ITEMS_CATALOG)
    if it:
        ensure_thumbnails([it["item_id"]])
        return ITEMS_CATALOG[it["item_id"]]
    found = catalog_client.search_keyword(query, ITEMS_CATALOG)
    if found:
        _save_discovered_roblox_items(found)
        return ITEMS_CATALOG[found[0]["item_id"]]
    return None


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = BASE_DIR / "web" / "index.html"
    return html_path.read_text(encoding="utf-8")


@app.get("/api/status")
def api_status():
    return db.get_stats()


@app.get("/api/items/search")
def api_search_items(q: str = Query("")):
    query = q.strip()
    if not query:
        return []
    ql = query.lower()

    if ql.isdigit() and int(ql) in ITEMS_CATALOG:
        ensure_thumbnails([int(ql)])
        return [ITEMS_CATALOG[int(ql)]]

    matches = []
    for it in ITEMS_CATALOG.values():
        acr = it.get("acronym", "").lower()
        name = it["name"].lower()
        if acr == ql or name == ql:
            matches.append((0, -it["value"], it))
        elif ql in acr or ql in name:
            matches.append((1, -it["value"], it))
    matches.sort(key=lambda x: (x[0], x[1]))
    top = [m[2] for m in matches[:15]]

    # If fewer than 3 matches in local catalog, fall back live to Roblox Catalog v2 (Creator=Roblox, no UGC)
    if len(top) < 3 and len(query) >= 3:
        discovered = catalog_client.search_keyword(query, ITEMS_CATALOG)
        new_items = [d for d in discovered if d["item_id"] not in ITEMS_CATALOG]
        if new_items:
            _save_discovered_roblox_items(new_items)
        seen_ids = {x["item_id"] for x in top}
        for d in discovered:
            if d["item_id"] not in seen_ids and d["item_id"] in ITEMS_CATALOG:
                top.append(ITEMS_CATALOG[d["item_id"]])
                seen_ids.add(d["item_id"])

    ensure_thumbnails([x["item_id"] for x in top[:8]])
    return top[:15]


@app.post("/api/items/value")
def api_set_item_value(req: SetItemValueRequest):
    iid = int(req.item_id)
    if iid not in ITEMS_CATALOG:
        return {"error": f"Item {iid} not found"}
    db.set_manual_item_value(iid, req.value)
    ITEMS_CATALOG[iid]["manual_value"] = req.value
    if req.value is not None and req.value >= 0:
        ITEMS_CATALOG[iid]["value"] = int(req.value)
        ITEMS_CATALOG[iid]["is_valued"] = True
    else:
        ITEMS_CATALOG[iid]["value"] = int(ITEMS_CATALOG[iid].get("default_value", ITEMS_CATALOG[iid]["rap"]))
    return {"ok": True, "item": ITEMS_CATALOG[iid]}


@app.post("/api/tradecheck")
def api_tradecheck(req: TradeCheckRequest):
    ensure_thumbnails(req.give_ids + req.receive_ids)
    return engine.analyze_trade(
        give_item_ids=req.give_ids,
        receive_item_ids=req.receive_ids,
        items_catalog=ITEMS_CATALOG,
        engine_mode=req.engine,
        recent_ads=RECENT_ADS_CACHE,
    )


@app.get("/api/sniper")
def api_sniper(bucket: str = "ALL", min_deal: float = 5.0):
    results = []
    for d in SNIPED_DEALS:
        if d["deal_percent"] < min_deal:
            continue
        if bucket != "ALL" and d["bucket"] != bucket:
            continue
        results.append(d)
    return results[:50]


@app.post("/api/sniper/poll")
def api_sniper_poll():
    added = refresh_market_and_ads()
    return {"added": added, "total": len(SNIPED_DEALS)}


@app.get("/api/proofs")
def api_proofs(q: str = Query("")):
    item = resolve_or_discover_item(q)
    if not item:
        return {"error": f"Item '{q}' not found", "proofs": []}
    res = get_item_proofs_formatted(
        db, item["item_id"], ITEMS_CATALOG, limit=3, sales_client=sales_client
    )
    # Ensure thumbnails for every item inside the returned proofs
    proof_item_ids = []
    for p in res.get("proofs", []):
        for side_it in p.get("items_a", []) + p.get("items_b", []):
            if side_it.get("item_id"):
                proof_item_ids.append(int(side_it["item_id"]))
    if proof_item_ids:
        ensure_thumbnails(proof_item_ids)
        for p in res.get("proofs", []):
            for side_it in p.get("items_a", []) + p.get("items_b", []):
                iid = int(side_it.get("item_id", 0))
                if iid in ITEMS_CATALOG and ITEMS_CATALOG[iid].get("thumbnail_url"):
                    side_it["thumbnail_url"] = ITEMS_CATALOG[iid]["thumbnail_url"]
    return res


@app.post("/api/proofs/manual")
def api_add_manual_proof(req: ManualProofRequest):
    parts_a = [x.strip() for x in req.side_a.split(",") if x.strip()][:4]
    parts_b = [x.strip() for x in req.side_b.split(",") if x.strip()][:4]
    if not parts_a or not parts_b:
        return {"error": "Provide at least 1 real item on each side"}

    items_a = []
    for q in parts_a:
        it = resolve_or_discover_item(q)
        if not it:
            return {"error": f"Could not find Side A item: '{q}'"}
        items_a.append(
            {
                "item_id": it["item_id"],
                "uaid": int(time.time() * 1000) + len(items_a),
                "name": it["name"],
                "acronym": it.get("acronym", ""),
                "value": it["value"],
                "rap": it["rap"],
                "thumbnail_url": it.get("thumbnail_url", ""),
            }
        )

    items_b = []
    for q in parts_b:
        it = resolve_or_discover_item(q)
        if not it:
            return {"error": f"Could not find Side B item: '{q}'"}
        items_b.append(
            {
                "item_id": it["item_id"],
                "uaid": int(time.time() * 1000) + 10 + len(items_b),
                "name": it["name"],
                "acronym": it.get("acronym", ""),
                "value": it["value"],
                "rap": it["rap"],
                "thumbnail_url": it.get("thumbnail_url", ""),
            }
        )

    val_a = sum(x["value"] for x in items_a)
    rap_a = sum(x["rap"] for x in items_a)
    val_b = sum(x["value"] for x in items_b)
    rap_b = sum(x["rap"] for x in items_b)

    tid = db.insert_trade(
        user_a_id=8001,
        user_a_name="TraderA",
        user_b_id=8002,
        user_b_name="TraderB",
        items_a=items_a,
        items_b=items_b,
        value_a=val_a,
        rap_a=rap_a,
        value_b=val_b,
        rap_b=rap_b,
        op_value=abs(val_a - val_b),
        op_rap=abs(rap_a - rap_b),
        trade_type="SWAP",
    )
    return {"ok": True, "trade_id": tid}


@app.get("/api/op")
def api_op_lookup(q: str = Query("")):
    item = resolve_or_discover_item(q)
    if not item:
        return {"error": f"Item '{q}' not found"}
    return compute_op_lookup(
        db=db,
        item_id=item["item_id"],
        items_catalog=ITEMS_CATALOG,
        recent_ads=RECENT_ADS_CACHE,
        sales_client=sales_client,
    )


@app.get("/api/render/card.png")
def api_render_card(give: str = "", receive: str = ""):
    g_ids = [int(x) for x in give.split(",") if x.strip().isdigit()]
    r_ids = [int(x) for x in receive.split(",") if x.strip().isdigit()]
    ensure_thumbnails(g_ids + r_ids)
    g_items = [ITEMS_CATALOG[x] for x in g_ids if x in ITEMS_CATALOG]
    r_items = [ITEMS_CATALOG[x] for x in r_ids if x in ITEMS_CATALOG]
    png_bytes = render_trade_card_png(g_items, r_items)
    return Response(content=png_bytes, media_type="image/png")


if __name__ == "__main__":
    host = os.environ.get("HOST", cfg.get("server", {}).get("host", "127.0.0.1"))
    port = int(os.environ.get("PORT", cfg.get("server", {}).get("port", 8765)))
    url = f"http://{host}:{port}"
    print(f"Starting Void Local Market Suite at {url}")
    if host == "127.0.0.1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
