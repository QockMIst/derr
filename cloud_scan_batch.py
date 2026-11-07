import asyncio
import json
import os
import sqlite3
import subprocess
import threading
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

        ts_groups_a = defaultdict(list)
        for m in clean_a:
            ts_groups_a[m["detected_at"] // 60].append(m)

        ts_groups_b = defaultdict(list)
        for m in clean_b:
            ts_groups_b[m["detected_at"] // 60].append(m)

        common_buckets = set(ts_groups_a.keys()) & set(ts_groups_b.keys())
        if not common_buckets:
            continue

        best_bucket = max(common_buckets)
        side_a = ts_groups_a[best_bucket][:4]
        side_b = ts_groups_b[best_bucket][:4]

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
        ttype = "UPGRADE" if len(items_a) > len(items_b) else ("DOWNGRADE" if len(items_b) > len(items_a) else "SWAP")

        with conn:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    user_a_id, user_a_name, user_b_id, user_b_name,
                    items_a_json, items_b_json, value_a, rap_a, value_b, rap_b,
                    op_value, op_rap, trade_type, detected_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scanner')
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


def push_db_checkpoint() -> None:
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            check=False,
        )
        subprocess.run(["git", "add", "void_data.sqlite3"], check=False)
        res = subprocess.run(["git", "commit", "-m", "Auto-update trade DB"], capture_output=True)
        if res.returncode == 0:
            subprocess.run(["git", "pull", "--rebase", "-X", "ours"], check=False)
            subprocess.run(["git", "push"], check=False)
    except Exception:
        pass


def trigger_next_github_workflow() -> None:
    gh_tok = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "QockMlst/derr").strip()
    wf_name = os.environ.get("GITHUB_WORKFLOW_FILE", "scanner.yml").strip()
    if not gh_tok or not repo:
        return
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{wf_name}/dispatches"
    body = json.dumps({"ref": "main"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {gh_tok}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "CarbonScanner",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def start_discord_bot_background(bot_token: str) -> None:
    if not bot_token:
        return

    def _runner():
        try:
            from bot import client as discord_client
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(discord_client.start(bot_token))
        except Exception as exc:
            print(f"[Carbon Cloud Bot] Error: {exc}")

    t = threading.Thread(target=_runner, daemon=True)
    t.start()


def main() -> None:
    conn = connect_db()
    start_ts = time.time()
    now = int(start_ts)
    max_run_seconds = int(os.environ.get("MAX_RUN_SECONDS", "19800"))

    cfg = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "rb") as f:
                cfg = tomllib.load(f)
        except Exception:
            cfg = {}
    disc_cfg = cfg.get("discord", {})

    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip() or str(
        disc_cfg.get("bot_token", "")
    ).strip()
    vr_chan = os.environ.get("VALUE_REQUESTS_CHANNEL_ID", "").strip() or str(
        disc_cfg.get("value_requests_channel_id", "535250426061258753")
    ).strip()
    vr_tok = os.environ.get("DISCORD_SCANNER_TOKEN", "").strip() or str(
        disc_cfg.get("scanner_token", "")
    ).strip()

    if bot_token:
        start_discord_bot_background(bot_token)

    def refresh_roli_items() -> Dict[int, Dict[str, Any]]:
        roli = get_json("https://api.rolimons.com/items/v2/itemdetails")
        cat: Dict[int, Dict[str, Any]] = {}
        if roli and roli.get("success"):
            rows = []
            ts_now = int(time.time())
            for iid_str, info in (roli.get("items") or {}).items():
                iid = int(iid_str)
                rap = int(info[2]) if info[2] is not None and info[2] >= 0 else 0
                raw_val = int(info[3]) if info[3] is not None else -1
                def_val = int(info[4]) if info[4] is not None and info[4] >= 0 else rap
                val = raw_val if raw_val >= 0 else def_val
                cat[iid] = {
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
                        ts_now,
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
        return cat

    items_catalog = refresh_roli_items()
    seed_ads(conn)

    moves_count = 0
    trades_created = 0
    scanned = 0
    priority_users: List[int] = []
    seen_vr_msgs: set[str] = set()
    last_ad_seed = time.time()
    last_vr_poll = 0.0
    last_roli_refresh = time.time()
    last_git_push = time.time()

    while (time.time() - start_ts) < max_run_seconds:
        now_f = time.time()

        if now_f - last_roli_refresh >= 300:
            fresh_cat = refresh_roli_items()
            if fresh_cat:
                items_catalog = fresh_cat
            last_roli_refresh = now_f

        if now_f - last_ad_seed >= 25:
            seed_ads(conn)
            last_ad_seed = now_f

        if vr_chan and vr_tok and (now_f - last_vr_poll >= 25):
            last_vr_poll = now_f
            try:
                from db import Database
                from value_requests import poll_discord_value_requests_channel

                db_obj = Database(DB_PATH)
                trades_created += poll_discord_value_requests_channel(
                    db_obj, items_catalog, vr_chan, vr_tok, seen_vr_msgs
                )
            except Exception:
                pass

        if now_f - last_git_push >= 600:
            last_git_push = now_f
            push_db_checkpoint()

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
                time.sleep(2.0)
                continue
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

                # NEVER guess counterparties for departed UAIDs.
                # Only record UAID movements when the new owner's inventory is directly observed with that UAID.
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
    push_db_checkpoint()
    trigger_next_github_workflow()
    print(
        f"Batch complete: scanned={scanned}, moves={moves_count}, new_trades={trades_created}, total_trades={total_trades}, tracked_uaids={total_owners}"
    )


if __name__ == "__main__":
    main()
