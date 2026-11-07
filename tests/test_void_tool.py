import os
import tempfile
import pytest

from db import Database, OWNER_DISCORD_ID
from engine import TradeEngine
from oplookup import compute_op_lookup
from proofs import get_item_proofs_formatted
from render import render_trade_card_png
from sniper import AdSniper
from tracker.matcher import match_moves_into_trades


@pytest.fixture
def sample_env():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    db = Database(path)

    items = {
        1001: {
            "item_id": 1001,
            "name": "Inferno Crown",
            "acronym": "IC",
            "rap": 9500,
            "value": 10000,
            "default_value": 10000,
            "demand": 3,
            "demand_label": "High",
            "trend": 2,
            "trend_label": "Stable",
            "projected": False,
            "hyped": False,
            "rare": False,
            "is_valued": True,
            "source": "rolimons",
        },
        1002: {
            "item_id": 1002,
            "name": "Frost Sword",
            "acronym": "FS",
            "rap": 5200,
            "value": 5500,
            "default_value": 5500,
            "demand": 2,
            "demand_label": "Normal",
            "trend": 2,
            "trend_label": "Stable",
            "projected": False,
            "hyped": False,
            "rare": False,
            "is_valued": True,
            "source": "rolimons",
        },
        1003: {
            "item_id": 1003,
            "name": "Shadow Helm",
            "acronym": "SH",
            "rap": 5000,
            "value": 5200,
            "default_value": 5200,
            "demand": 2,
            "demand_label": "Normal",
            "trend": 2,
            "trend_label": "Stable",
            "projected": False,
            "hyped": False,
            "rare": False,
            "is_valued": True,
            "source": "rolimons",
        },
        215724072: {
            "item_id": 215724072,
            "name": "President Doge",
            "acronym": "",
            "rap": 29825,
            "value": 29825,
            "default_value": 29825,
            "demand": -1,
            "demand_label": "Unassigned",
            "trend": -1,
            "trend_label": "Unassigned",
            "projected": False,
            "hyped": False,
            "rare": False,
            "is_valued": False,
            "source": "rolimons",
        },
        1213472762: {
            "item_id": 1213472762,
            "name": "Catching Snowflakes",
            "acronym": "CS",
            "rap": 13480,
            "value": 14000,
            "default_value": 14000,
            "demand": 2,
            "demand_label": "Normal",
            "trend": 2,
            "trend_label": "Stable",
            "projected": False,
            "hyped": False,
            "rare": False,
            "is_valued": True,
            "source": "rolimons",
        },
        1609401184: {
            "item_id": 1609401184,
            "name": "Supa Dupa Fly Cap",
            "acronym": "Dupa",
            "rap": 38533,
            "value": 40000,
            "default_value": 40000,
            "demand": 3,
            "demand_label": "High",
            "trend": 2,
            "trend_label": "Stable",
            "projected": False,
            "hyped": False,
            "rare": False,
            "is_valued": True,
            "source": "rolimons",
        },
    }
    db.upsert_items(items)
    db.upsert_sales_cache(
        item_id=215724072,
        avg_price_7d=23878,
        avg_price_30d=24500,
        daily_volume=0.8,
        lowest_resale=25500,
        recent_points=[{"date": "2026-11-05", "price": 23878, "volume": 1}],
    )
    catalog = db.get_all_items()
    yield db, catalog
    try:
        os.remove(path)
    except OSError:
        pass


def test_v4_strict_doge_snowflakes_vs_dupa_and_ledger(sample_env):
    db, catalog = sample_env
    eng = TradeEngine(db=db)

    res_up = eng.analyze_trade([215724072, 1213472762], [1609401184], catalog, engine_mode="v4")
    assert res_up["verdict"] in ("Strong Win", "Win")
    assert res_up["retrade_room"] > 3000
    assert len(res_up["ledger"]) >= 5
    doge_item = next(x for x in res_up["give_items"] if x["item_id"] == 215724072)
    assert doge_item["market_value"] < 26000

    res_down = eng.analyze_trade([1609401184], [215724072, 1213472762], catalog, engine_mode="v4")
    assert res_down["verdict"] == "Not Worth It"
    assert res_down["retrade_room"] < -4000


def test_dropping_item_trap_and_rare_downgrade(sample_env):
    db, catalog = sample_env
    eng = TradeEngine(db=db)

    dropping_cat = {
        **catalog,
        9001: {
            "item_id": 9001,
            "name": "Dropping Big Hat",
            "acronym": "DBH",
            "rap": 35000,
            "value": 44000,
            "default_value": 44000,
            "demand": 1,
            "demand_label": "Low",
            "trend": 0,
            "trend_label": "Lowering",
            "projected": False,
            "hyped": False,
            "rare": False,
            "is_valued": True,
            "source": "rolimons",
        },
        21070012: {
            "item_id": 21070012,
            "name": "Dominus Empyreus",
            "acronym": "Emp",
            "rap": 13577229,
            "value": 110000000,
            "default_value": 110000000,
            "demand": 0,
            "demand_label": "Terrible",
            "trend": 2,
            "trend_label": "Stable",
            "projected": False,
            "hyped": False,
            "rare": True,
            "is_valued": True,
            "source": "rolimons",
        },
        88885069: {
            "item_id": 88885069,
            "name": "Lord of the Federation",
            "acronym": "LOTF",
            "rap": 1277922,
            "value": 100000000,
            "default_value": 100000000,
            "demand": 0,
            "demand_label": "Terrible",
            "trend": 2,
            "trend_label": "Stable",
            "projected": False,
            "hyped": False,
            "rare": True,
            "is_valued": True,
            "source": "rolimons",
        },
        33171947: {
            "item_id": 33171947,
            "name": "Bling $$ Necklace",
            "acronym": "Bling",
            "rap": 4045444,
            "value": 30000000,
            "default_value": 30000000,
            "demand": 0,
            "demand_label": "Terrible",
            "trend": 2,
            "trend_label": "Stable",
            "projected": False,
            "hyped": False,
            "rare": True,
            "is_valued": True,
            "source": "rolimons",
        },
    }

    res_drop = eng.analyze_trade([1609401184], [9001], dropping_cat, engine_mode="v4")
    assert "Not Worth It" in res_drop["verdict"]
    assert res_drop["retrade_room"] < 0

    res_emp = eng.analyze_trade([21070012], [88885069, 33171947], dropping_cat, engine_mode="v4")
    assert res_emp["verdict"] == "Not Worth It"
    assert res_emp["retrade_room"] < 0


def test_whitelist_and_owner_protection(sample_env):
    db, _ = sample_env
    assert db.is_whitelisted(OWNER_DISCORD_ID) is True
    assert db.remove_whitelist(OWNER_DISCORD_ID) is False
    assert db.is_whitelisted(OWNER_DISCORD_ID) is True

    test_uid = 555666777888999
    assert db.is_whitelisted(test_uid) is False
    db.add_whitelist(test_uid, username="TestTrader", added_by=OWNER_DISCORD_ID)
    assert db.is_whitelisted(test_uid) is True
    users = db.get_whitelisted_users()
    assert len(users) == 2

    assert db.remove_whitelist(test_uid) is True
    assert db.is_whitelisted(test_uid) is False


def test_ad_sniper_and_matcher_no_stale_mirror(sample_env):
    db, catalog = sample_env
    db.upsert_trader(111, "TraderOne", 1700000000)

    db.record_inventory_snapshot(111, {90001: 1001}, scanned_at=1700000100)
    db.record_inventory_snapshot(555, {90002: 1002, 90003: 1003}, scanned_at=1700000100)

    moves_555 = db.record_inventory_snapshot(555, {90001: 1001}, scanned_at=1700000200)
    assert len(moves_555) >= 3
    assert db.get_trader_name(555) == "User_555"

    created = match_moves_into_trades(db, catalog, now_ts=1700000220)
    assert len(created) == 1

    sample_deals = [
        {
            "ad_id": 1,
            "bucket": "10k-50k",
            "deal_percent": 12.5,
            "offer_items": [{"item_id": 101, "name": "Black Paintball Tactical Helm", "acronym": "BPTH"}],
            "request_items": [{"item_id": 102, "name": "Classic Fedora", "acronym": "CF"}],
        }
    ]
    assert len(AdSniper.filter_deals(sample_deals, min_deal=5.0, sends_query="bpth")) == 1
