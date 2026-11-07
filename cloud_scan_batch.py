import json
import os
import sqlite3
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).parent / "void_data.sqlite3"
CONFIG_PATH = Path(__file__).parent / "config.toml"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def get_json(url: str, timeout: int = 12) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS items (
            item_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            acronym TEXT NOT NULL DEFAULT '',
            rap INTEGER NOT NULL DEFAULT 0,
            value INTEGER NOT NULL DEFAULT 0,
            default_value INTEGER NOT NULL DEFAULT 0,
            demand INTEGER NOT NULL DEFAULT -1,
            trend INTEGER NOT NULL DEFAULT -1,
            projected INTEGER NOT NULL DEFAULT 0,
            hyped INTEGER NOT NULL DEFAULT 0,
            rare INTEGER NOT NULL DEFAULT 0,
            thumbnail_url TEXT DEFAULT '',
            source TEXT NOT NULL DEFAULT 'rolimons',
            manual_value INTEGER DEFAULT NULL,
            collectible_item_id TEXT DEFAULT '',
            updated_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS traders (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            last_ad_at INTEGER NOT NULL DEFAULT 0,
            last_scanned_at INTEGER NOT NULL DEFAULT 0,
            scan_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS owners (
            uaid INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            seen_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_owners_user ON owners(user_id);
        CREATE INDEX IF NOT EXISTS idx_owners_item ON owners(item_id);
        CREATE TABLE IF NOT EXISTS uaid_moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uaid INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL,
            to_user_id INTEGER NOT NULL,
            detected_at INTEGER NOT NULL,
            matched_trade_id INTEGER DEFAULT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_moves_pair ON uaid_moves(from_user_id, to_user_id, matched_trade_id);
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_a_id INTEGER NOT NULL,
            user_a_name TEXT NOT NULL DEFAULT '',
            user_b_id INTEGER NOT NULL,
            user_b_name TEXT NOT NULL DEFAULT '',
            items_a_json TEXT NOT NULL,
            items_b_json TEXT NOT NULL,
            value_a INTEGER NOT NULL,
            rap_a INTEGER NOT NULL,
            value_b INTEGER NOT NULL,
            rap_b INTEGER NOT NULL,
            op_value INTEGER NOT NULL,
            op_rap INTEGER NOT NULL,
            trade_type TEXT NOT NULL DEFAULT 'SWAP',
            detected_at INTEGER NOT NULL,
            predicted_op INTEGER DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS trade_items (
            trade_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            side TEXT NOT NULL,
            uaid INTEGER NOT NULL DEFAULT 0,
            item_value INTEGER NOT NULL,
            item_rap INTEGER NOT NULL,
            other_side_value INTEGER NOT NULL,
            side_total_value INTEGER NOT NULL,
            realized_op INTEGER NOT NULL,
            detected_at INTEGER NOT NULL,
            PRIMARY KEY (trade_id, item_id, uaid)
        );
        """
    )
    with conn:
        bad_ids = [
            r["id"]
            for r in conn.execute(
                """
                SELECT id FROM trades
                WHERE items_a_json = items_b_json
                   OR items_a_json LIKE '%"item_id": 0%'
                   OR items_b_json LIKE '%"item_id": 0%'
                """
            ).fetchall()
        ]
        if bad_ids:
            ph = ",".join("?" for _ in bad_ids)
            conn.execute(f"DELETE FROM trade_items WHERE trade_id IN ({ph})", bad_ids)
            conn.execute(f"DELETE FROM trades WHERE id IN ({ph})", bad_ids)
    return conn


def fetch_player_inv(user_id: int) -> Optional[Dict[int, int]]:
    d2 = get_json(
        f"https://inventory.roblox.com/v1/users/{user_id}/assets/collectibles?limit=100&sortOrder=Asc"
    )
    if d2 and "data" in d2:
        out2: Dict[int, int] = {}
        for entry in d2.get("data", []):
            if entry.get("userAssetId") and entry.get("assetId"):
                out2[int(entry["userAssetId"])] = int(entry["assetId"])
        return out2
    return None


def seed_ads(conn: sqlite3.Connection) -> None:
    ads_data = get_json("https://api.rolimons.com/tradeads/v1/getrecentads")
    if ads_data and ads_data.get("success"):
        with conn:
            for raw in ads_data.get("trade_ads", []):
                try:
                    uid = int(raw[2])
                    uname = str(raw[3])
                    ts = int(raw[1])
                    conn.execute(
                        """
                        INSERT INTO traders (user_id, username, last_ad_at, last_scanned_at, scan_count)
                        VALUES (?, ?, ?, 0, 0)
                        ON CONFLICT(user_id) DO UPDATE SET
                            username = CASE WHEN excluded.username != '' THEN excluded.username ELSE traders.username END,
                            last_ad_at = MAX(traders.last_ad_at, excluded.last_ad_at)
                        """,
                        (uid, uname, ts),
                    )
                except Exception:
                    continue


def resolve_username(conn: sqlite3.Connection, uid: int) -> str:
    row = conn.execute("SELECT username FROM traders WHERE user_id = ?", (uid,)).fetchone()
    if row and row["username"] and not str(row["username"]).startswith("User_"):
        return str(row["username"])
    u_data = get_json(f"https://users.roblox.com/v1/users/{uid}", timeout=5)
    if u_data and u_data.get("name"):
        uname = str(u_data["name"])
        conn.execute(
            "UPDATE traders SET username = ? WHERE user_id = ?",
            (uname, uid),
        )
        return uname
    return f"User_{uid}"


def match_pending(conn: sqlite3.Connection, items_catalog: Dict[int, Dict[str, Any]], now: int) -> int:
    unmatched = conn.execute(
        "SELECT * FROM uaid_moves WHERE matched_trade_id IS NULL AND detected_at >= ?",
        (now - 28800,),
    ).fetchall()

    directed = defaultdict(list)
    for m in unmatched:
        u_from = int(m["from_user_id"])
        u_to = int(m["to_user_id"])
        if u_from != u_to:
            directed[(u_from, u_to)].append(dict(m))

    visited = set()
    trades_created = 0
    for (u_a, u_b), a_to_b in list(directed.items()):
        pk = (min(u_a, u_b), max(u_a, u_b))
        if pk in visited:
            continue
        b_to_a = directed.get((u_b, u_a), [])
        if not a_to_b or not b_to_a:
            continue
        visited.add(pk)

        uaids_a = {int(m["uaid"]) for m in a_to_b}
        uaids_b = {int(m["uaid"]) for m in b_to_a}
        overlap = uaids_a & uaids_b
        clean_a = [m for m in a_to_b if int(m["uaid"]) not in overlap]
        clean_b = [m for m in b_to_a if int(m["uaid"]) not in overlap]
        if not clean_a or not clean_b:
            continue

        side_a = clean_a[:4]
        side_b = clean_b[:4]

        def _build(mv_list):
            res = []
            for mv in mv_list:
                iid = int(mv["item_id"])
                meta = items_catalog.get(iid, {"name": f"Item #{iid}", "acronym": "", "value": 0, "rap": 0})
                res.append(
                    {
                        "item_id": iid,
                        "uaid": int(mv["uaid"]),
                        "name": meta["name"],
                        "acronym": meta["acronym"],
                        "value": int(meta["value"]),
                        "rap": int(meta["rap"]),
                    }
                )
            return res

        items_a = _build(side_a)
        items_b = _build(side_b)
        if {x["item_id"] for x in items_a} == {x["item_id"] for x in items_b} and len(items_a) == len(items_b):
            continue

        val_a = sum(x["value"] for x in items_a)
        rap_a = sum(x["rap"] for x in items_a)
        val_b = sum(x["value"] for x in items_b)
        rap_b = sum(x["rap"] for x in items_b)
        op_val = abs(val_a - val_b)
        op_rap = abs(rap_a - rap_b)
        ttype = "UPGRADE" if len(items_a) > len(items_b) else ("DOWNGRADE" if len(items_b) > len(items_a) else "SWAP")

        with conn:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    user_a_id, user_a_name, user_b_id, user_b_name,
                    items_a_json, items_b_json, value_a, rap_a, value_b, rap_b,
                    op_value, op_rap, trade_type, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    u_a,
                    resolve_username(conn, u_a),
                    u_b,
                    resolve_username(conn, u_b),
                    json.dumps(items_a),
                    json.dumps(items_b),
                    val_a,
                    rap_a,
                    val_b,
                    rap_b,
                    op_val,
                    op_rap,
                    ttype,
                    now,
                ),
            )
            tid = cur.lastrowid
            for idx, it in enumerate(items_a):
                share = (it["value"] / val_a) if val_a > 0 else (1.0 / len(items_a))
                realized_op = int(round((val_b - val_a) * share))
                conn.execute(
                    """
                    INSERT OR REPLACE INTO trade_items (
                        trade_id, item_id, side, uaid, item_value, item_rap,
                        other_side_value, side_total_value, realized_op, detected_at
                    ) VALUES (?, ?, 'A', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tid, it["item_id"], it["uaid"], it["value"], it["rap"], val_b, val_a, realized_op, now),
                )
            for idx, it in enumerate(items_b):
                share = (it["value"] / val_b) if val_b > 0 else (1.0 / len(items_b))
                realized_op = int(round((val_a - val_b) * share))
                conn.execute(
                    """
                    INSERT OR REPLACE INTO trade_items (
                        trade_id, item_id, side, uaid, item_value, item_rap,
                        other_side_value, side_total_value, realized_op, detected_at
                    ) VALUES (?, ?, 'B', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tid, it["item_id"], it["uaid"], it["value"], it["rap"], val_a, val_b, realized_op, now),
                )
            move_ids = [m["id"] for m in a_to_b + b_to_a]
            ph = ",".join("?" for _ in move_ids)
            conn.execute(f"UPDATE uaid_moves SET matched_trade_id = ? WHERE id IN ({ph})", [tid, *move_ids])
            trades_created += 1
    return trades_created


def main() -> None:
    conn = connect_db()
    start_ts = time.time()
    now = int(start_ts)

    roli = get_json("https://api.rolimons.com/items/v2/itemdetails")
    items_catalog: Dict[int, Dict[str, Any]] = {}
    if roli and roli.get("success"):
        rows = []
        for iid_str, info in (roli.get("items") or {}).items():
            iid = int(iid_str)
            rap = int(info[2]) if info[2] is not None and info[2] >= 0 else 0
            raw_val = int(info[3]) if info[3] is not None else -1
            def_val = int(info[4]) if info[4] is not None and info[4] >= 0 else rap
            val = raw_val if raw_val >= 0 else def_val
            items_catalog[iid] = {
                "item_id": iid,
                "name": str(info[0]),
                "acronym": str(info[1] or ""),
                "rap": rap,
                "value": val,
            }
            rows.append(
                (
                    iid,
                    str(info[0]),
                    str(info[1] or ""),
                    rap,
                    val,
                    def_val,
                    int(info[5]) if info[5] is not None else -1,
                    int(info[6]) if info[6] is not None else -1,
                    1 if info[7] == 1 else 0,
                    1 if info[8] == 1 else 0,
                    1 if info[9] == 1 else 0,
                    "rolimons",
                    now,
                )
            )
        with conn:
            conn.executemany(
                """
                INSERT INTO items (
                    item_id, name, acronym, rap, value, default_value,
                    demand, trend, projected, hyped, rare, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    name=excluded.name, acronym=excluded.acronym, rap=excluded.rap,
                    value=excluded.value, default_value=excluded.default_value,
                    demand=excluded.demand, trend=excluded.trend,
                    projected=excluded.projected, hyped=excluded.hyped, rare=excluded.rare,
                    updated_at=excluded.updated_at
                """,
                rows,
            )

    seed_ads(conn)

    vr_imported = 0
    try:
        from db import Database
        from value_requests import poll_discord_value_requests_channel

        cfg = {}
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "rb") as f:
                cfg = tomllib.load(f)
        disc_cfg = cfg.get("discord", {})
        vr_chan = os.environ.get("VALUE_REQUESTS_CHANNEL_ID") or str(
            disc_cfg.get("value_requests_channel_id", "535250426061258753")
        ).strip()
        vr_tok = os.environ.get("DISCORD_SCANNER_TOKEN") or str(
            disc_cfg.get("scanner_token", "")
        ).strip()
        if vr_chan and vr_tok:
            db_obj = Database(DB_PATH)
            vr_imported = poll_discord_value_requests_channel(
                db_obj, items_catalog, vr_chan, vr_tok, set()
            )
    except Exception:
        pass

    moves_count = 0
    trades_created = 0
    scanned = 0
    priority_users: List[int] = []
    last_ad_seed = time.time()

    while (time.time() - start_ts) < 260 and scanned < 420:
        if time.time() - last_ad_seed >= 25:
            seed_ads(conn)
            last_ad_seed = time.time()

        if priority_users:
            uid = priority_users.pop(0)
        else:
            step_now = int(time.time())
            row = conn.execute(
                """
                SELECT user_id FROM traders
                ORDER BY
                    CASE WHEN last_ad_at >= ? AND last_scanned_at <= ? THEN 0 ELSE 1 END ASC,
                    last_scanned_at ASC,
                    last_ad_at DESC
                LIMIT 1
                """,
                (step_now - 3600, step_now - 45),
            ).fetchone()
            if not row:
                break
            uid = int(row["user_id"])

        scanned += 1
        inv = fetch_player_inv(uid)
        step_now = int(time.time())
        with conn:
            prev_rows = conn.execute(
                "SELECT uaid, item_id FROM owners WHERE user_id = ?", (uid,)
            ).fetchall()
            prev_owned = {int(r["uaid"]): int(r["item_id"]) for r in prev_rows}

            if inv:
                placeholders = ",".join("?" for _ in inv)
                existing = conn.execute(
                    f"SELECT uaid, item_id, user_id FROM owners WHERE uaid IN ({placeholders})",
                    list(inv.keys()),
                ).fetchall()
                existing_map = {int(r["uaid"]): r for r in existing}
                for uaid, item_id in inv.items():
                    prev = existing_map.get(int(uaid))
                    if prev is not None and int(prev["user_id"]) != uid:
                        from_u = int(prev["user_id"])
                        conn.execute(
                            "INSERT INTO uaid_moves (uaid, item_id, from_user_id, to_user_id, detected_at) VALUES (?, ?, ?, ?, ?)",
                            (int(uaid), int(item_id), from_u, uid, step_now),
                        )
                        conn.execute(
                            """
                            INSERT INTO traders (user_id, username, last_ad_at, last_scanned_at, scan_count)
                            VALUES (?, '', ?, 0, 0)
                            ON CONFLICT(user_id) DO UPDATE SET last_ad_at = MAX(traders.last_ad_at, ?)
                            """,
                            (from_u, step_now, step_now),
                        )
                        moves_count += 1
                        if from_u in priority_users:
                            priority_users.remove(from_u)
                        priority_users.insert(0, from_u)

                conn.executemany(
                    """
                    INSERT INTO owners (uaid, item_id, user_id, seen_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(uaid) DO UPDATE SET
                        item_id=excluded.item_id, user_id=excluded.user_id, seen_at=excluded.seen_at
                    """,
                    [(int(u), int(i), uid, step_now) for u, i in inv.items()],
                )

                current_uaids = {int(u) for u in inv.keys()}
                departed = [u for u in prev_owned.keys() if u not in current_uaids]
                if departed:
                    cp_rows = conn.execute(
                        """
                        SELECT DISTINCT from_user_id AS cp FROM uaid_moves
                        WHERE to_user_id = ? AND detected_at >= ? AND matched_trade_id IS NULL
                        UNION
                        SELECT DISTINCT to_user_id AS cp FROM uaid_moves
                        WHERE from_user_id = ? AND detected_at >= ? AND matched_trade_id IS NULL
                        """,
                        (uid, step_now - 600, uid, step_now - 600),
                    ).fetchall()
                    cps = [int(r["cp"]) for r in cp_rows if int(r["cp"]) != uid and int(r["cp"]) > 0]
                    if len(cps) == 1:
                        target_cp = cps[0]
                        for dep_u in departed[:4]:
                            dep_iid = prev_owned[dep_u]
                            already = conn.execute(
                                "SELECT 1 FROM uaid_moves WHERE uaid = ? AND from_user_id = ? AND detected_at >= ?",
                                (dep_u, uid, step_now - 900),
                            ).fetchone()
                            if not already:
                                conn.execute(
                                    "INSERT INTO uaid_moves (uaid, item_id, from_user_id, to_user_id, detected_at) VALUES (?, ?, ?, ?, ?)",
                                    (dep_u, dep_iid, uid, target_cp, step_now),
                                )
                                conn.execute(
                                    "UPDATE owners SET user_id = ?, seen_at = ? WHERE uaid = ?",
                                    (target_cp, step_now, dep_u),
                                )
                                moves_count += 1
            conn.execute(
                "UPDATE traders SET last_scanned_at = ?, scan_count = scan_count + 1 WHERE user_id = ?",
                (step_now, uid),
            )

        if moves_count > 0:
            trades_created += match_pending(conn, items_catalog, step_now)

        time.sleep(0.35)

    total_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    total_owners = conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
    conn.close()
    print(
        f"Batch complete: scanned={scanned}, moves={moves_count}, new_trades={trades_created}, total_trades={total_trades}, tracked_uaids={total_owners}"
    )


if __name__ == "__main__":
    main()
