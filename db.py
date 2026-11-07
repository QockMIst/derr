import sqlite3
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DB_PATH = Path(__file__).parent / "void_data.sqlite3"
OWNER_DISCORD_ID = 1388335689229275220

DEMAND_LABELS = {
    -1: "Unassigned",
    0: "Terrible",
    1: "Low",
    2: "Normal",
    3: "High",
    4: "Amazing",
}

TREND_LABELS = {
    -1: "Unassigned",
    0: "Lowering",
    1: "Unstable",
    2: "Stable",
    3: "Raising",
    4: "Fluctuating",
}


class Database:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        now = int(time.time())
        with self._connect() as conn:
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
                CREATE INDEX IF NOT EXISTS idx_trades_detected ON trades(detected_at DESC);

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
                CREATE INDEX IF NOT EXISTS idx_trade_items_item ON trade_items(item_id, detected_at DESC);

                CREATE TABLE IF NOT EXISTS ads_seen (
                    ad_id INTEGER PRIMARY KEY,
                    poster_id INTEGER NOT NULL,
                    poster_name TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    seen_at INTEGER NOT NULL,
                    deal_percent REAL NOT NULL,
                    value_bucket TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ads_seen_time ON ads_seen(seen_at DESC);

                CREATE TABLE IF NOT EXISTS item_sales_cache (
                    item_id INTEGER PRIMARY KEY,
                    avg_price_7d INTEGER NOT NULL DEFAULT 0,
                    avg_price_30d INTEGER NOT NULL DEFAULT 0,
                    daily_volume REAL NOT NULL DEFAULT 0.0,
                    lowest_resale INTEGER NOT NULL DEFAULT 0,
                    recent_points_json TEXT NOT NULL DEFAULT '[]',
                    updated_at INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS live_sales (
                    activity_id INTEGER PRIMARY KEY,
                    item_id INTEGER NOT NULL,
                    old_rap INTEGER NOT NULL,
                    new_rap INTEGER NOT NULL,
                    estimated_sale_price INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_live_sales_item ON live_sales(item_id, timestamp DESC);

                CREATE TABLE IF NOT EXISTS whitelist (
                    discord_user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    added_by INTEGER NOT NULL DEFAULT 0,
                    added_at INTEGER NOT NULL DEFAULT 0,
                    is_owner INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            conn.execute(
                """
                INSERT INTO whitelist (discord_user_id, username, added_by, added_at, is_owner)
                VALUES (?, 'Owner', ?, ?, 1)
                ON CONFLICT(discord_user_id) DO UPDATE SET is_owner = 1
                """,
                (OWNER_DISCORD_ID, OWNER_DISCORD_ID, now),
            )
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
            if "source" not in cols:
                conn.execute("ALTER TABLE items ADD COLUMN source TEXT NOT NULL DEFAULT 'rolimons'")
            if "manual_value" not in cols:
                conn.execute("ALTER TABLE items ADD COLUMN manual_value INTEGER DEFAULT NULL")
            if "collectible_item_id" not in cols:
                conn.execute("ALTER TABLE items ADD COLUMN collectible_item_id TEXT DEFAULT ''")

            fake_ids = [
                r["id"]
                for r in conn.execute(
                    """
                    SELECT id FROM trades
                    WHERE user_a_id IN (1001, 2001, 2002, 2003, 888001)
                       OR items_a_json = items_b_json
                       OR items_a_json LIKE '%"item_id": 0%'
                       OR items_b_json LIKE '%"item_id": 0%'
                    """
                ).fetchall()
            ]
            if fake_ids:
                ph = ",".join("?" for _ in fake_ids)
                conn.execute(f"DELETE FROM trade_items WHERE trade_id IN ({ph})", fake_ids)
                conn.execute(f"DELETE FROM trades WHERE id IN ({ph})", fake_ids)

    def add_whitelist(self, discord_user_id: int, username: str = "", added_by: int = OWNER_DISCORD_ID) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO whitelist (discord_user_id, username, added_by, added_at, is_owner)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(discord_user_id) DO UPDATE SET
                    username = CASE WHEN excluded.username != '' THEN excluded.username ELSE whitelist.username END
                """,
                (
                    int(discord_user_id),
                    str(username),
                    int(added_by),
                    now,
                    1 if int(discord_user_id) == OWNER_DISCORD_ID else 0,
                ),
            )

    def remove_whitelist(self, discord_user_id: int) -> bool:
        if int(discord_user_id) == OWNER_DISCORD_ID:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM whitelist WHERE discord_user_id = ? AND is_owner = 0",
                (int(discord_user_id),),
            )
            return cur.rowcount > 0

    def is_whitelisted(self, discord_user_id: int) -> bool:
        if int(discord_user_id) == OWNER_DISCORD_ID:
            return True
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM whitelist WHERE discord_user_id = ?",
                (int(discord_user_id),),
            ).fetchone()
            return row is not None

    def get_whitelisted_users(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM whitelist ORDER BY is_owner DESC, added_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_items(self, items_dict: Dict[int, Dict[str, Any]]) -> None:
        now = int(time.time())
        rows = [
            (
                iid,
                d["name"],
                d.get("acronym", ""),
                int(d.get("rap", 0)),
                int(d.get("value", 0)),
                int(d.get("default_value", d.get("rap", 0))),
                int(d.get("demand", -1)),
                int(d.get("trend", -1)),
                1 if d.get("projected") else 0,
                1 if d.get("hyped") else 0,
                1 if d.get("rare") else 0,
                d.get("thumbnail_url", ""),
                d.get("source", "rolimons"),
                None,
                d.get("collectible_item_id", ""),
                now,
            )
            for iid, d in items_dict.items()
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO items (
                    item_id, name, acronym, rap, value, default_value,
                    demand, trend, projected, hyped, rare, thumbnail_url,
                    source, manual_value, collectible_item_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    name=excluded.name,
                    acronym=CASE WHEN excluded.acronym != '' THEN excluded.acronym ELSE items.acronym END,
                    rap=CASE WHEN excluded.rap > 0 THEN excluded.rap ELSE items.rap END,
                    value=CASE
                        WHEN excluded.source = 'rolimons' THEN excluded.value
                        WHEN items.source = 'rolimons' THEN items.value
                        WHEN excluded.value > 0 THEN excluded.value
                        ELSE items.value
                    END,
                    default_value=CASE WHEN excluded.source = 'rolimons' THEN excluded.default_value WHEN excluded.default_value > 0 THEN excluded.default_value ELSE items.default_value END,
                    demand=CASE WHEN excluded.source = 'rolimons' THEN excluded.demand ELSE items.demand END,
                    trend=CASE WHEN excluded.source = 'rolimons' THEN excluded.trend ELSE items.trend END,
                    projected=CASE WHEN excluded.source = 'rolimons' THEN excluded.projected ELSE items.projected END,
                    hyped=CASE WHEN excluded.source = 'rolimons' THEN excluded.hyped ELSE items.hyped END,
                    rare=CASE WHEN excluded.source = 'rolimons' THEN excluded.rare ELSE MAX(items.rare, excluded.rare) END,
                    thumbnail_url=CASE WHEN excluded.thumbnail_url != '' THEN excluded.thumbnail_url ELSE items.thumbnail_url END,
                    source=CASE WHEN items.source = 'rolimons' OR excluded.source = 'rolimons' THEN 'rolimons' ELSE excluded.source END,
                    collectible_item_id=CASE WHEN excluded.collectible_item_id != '' THEN excluded.collectible_item_id ELSE items.collectible_item_id END,
                    updated_at=excluded.updated_at
                """,
                rows,
            )

    def update_item_rap_from_scan(self, item_id: int, rap: int) -> None:
        if rap <= 0:
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE items
                SET rap = ?,
                    default_value = CASE WHEN source != 'rolimons' THEN ? ELSE default_value END,
                    value = CASE WHEN source != 'rolimons' THEN ? ELSE value END
                WHERE item_id = ?
                """,
                (rap, rap, rap, item_id),
            )

    def update_thumbnails(self, thumb_map: Dict[int, str]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "UPDATE items SET thumbnail_url = ? WHERE item_id = ?",
                [(url, iid) for iid, url in thumb_map.items() if url],
            )

    def get_all_items(self) -> Dict[int, Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM items")
            res = {}
            for r in cur.fetchall():
                d = dict(r)
                d["projected"] = bool(r["projected"])
                d["hyped"] = bool(r["hyped"])
                d["rare"] = bool(r["rare"])
                d["demand_label"] = DEMAND_LABELS.get(int(d.get("demand", -1)), "Unassigned")
                d["trend_label"] = TREND_LABELS.get(int(d.get("trend", -1)), "Unassigned")
                d["is_valued"] = d.get("source") == "rolimons" and (
                    d["value"] != d["rap"] or int(d.get("demand", -1)) >= 0
                )
                res[r["item_id"]] = d
            return res

    def upsert_trader(self, user_id: int, username: str = "", ad_time: int = 0) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO traders (user_id, username, last_ad_at, last_scanned_at, scan_count)
                VALUES (?, ?, ?, 0, 0)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = CASE WHEN excluded.username != '' THEN excluded.username ELSE traders.username END,
                    last_ad_at = MAX(traders.last_ad_at, excluded.last_ad_at)
                """,
                (int(user_id), str(username), int(ad_time)),
            )

    def get_next_traders_to_scan(self, limit: int = 10) -> List[Dict[str, Any]]:
        now = int(time.time())
        hot_cutoff = now - 3600
        rescan_hot_after = now - 45
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT * FROM traders
                ORDER BY
                    CASE WHEN last_ad_at >= ? AND last_scanned_at <= ? THEN 0 ELSE 1 END ASC,
                    last_scanned_at ASC,
                    last_ad_at DESC
                LIMIT ?
                """,
                (hot_cutoff, rescan_hot_after, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_trader_name(self, user_id: int) -> str:
        uid = int(user_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username FROM traders WHERE user_id = ?", (uid,)
            ).fetchone()
            if row and row["username"] and not str(row["username"]).startswith("User_"):
                return str(row["username"])
        return f"User_{uid}"

    def resolve_missing_trader_names(self, user_ids: List[int]) -> Dict[int, str]:
        out: Dict[int, str] = {}
        to_fetch = []
        with self._connect() as conn:
            for uid in set(int(x) for x in user_ids if int(x) > 0):
                row = conn.execute(
                    "SELECT username FROM traders WHERE user_id = ?", (uid,)
                ).fetchone()
                if row and row["username"] and not str(row["username"]).startswith("User_"):
                    out[uid] = str(row["username"])
                else:
                    to_fetch.append(uid)
        for uid in to_fetch[:12]:
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"https://users.roblox.com/v1/users/{uid}",
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    uname = str(data.get("name") or "")
                    if uname:
                        self.upsert_trader(uid, uname, 0)
                        out[uid] = uname
            except Exception:
                pass
        return out

    def record_inventory_snapshot(
        self, user_id: int, uaid_to_item: Dict[int, int], scanned_at: Optional[int] = None
    ) -> List[Dict[str, int]]:
        now = scanned_at if scanned_at is not None else int(time.time())
        moves: List[Dict[str, int]] = []
        with self._connect() as conn:
            existing_rows = conn.execute(
                "SELECT uaid, item_id, user_id, seen_at FROM owners WHERE user_id = ?",
                (int(user_id),),
            ).fetchall()
            prev_owned = {int(r["uaid"]): r for r in existing_rows}

            if uaid_to_item:
                placeholders = ",".join("?" for _ in uaid_to_item)
                global_rows = conn.execute(
                    f"SELECT uaid, item_id, user_id, seen_at FROM owners WHERE uaid IN ({placeholders})",
                    list(uaid_to_item.keys()),
                ).fetchall()
                existing_map = {int(r["uaid"]): r for r in global_rows}

                for uaid, item_id in uaid_to_item.items():
                    prev = existing_map.get(int(uaid))
                    if prev is not None and int(prev["user_id"]) != int(user_id):
                        from_uid = int(prev["user_id"])
                        move = {
                            "uaid": int(uaid),
                            "item_id": int(item_id),
                            "from_user_id": from_uid,
                            "to_user_id": int(user_id),
                            "detected_at": now,
                        }
                        cur = conn.execute(
                            """
                            INSERT INTO uaid_moves (uaid, item_id, from_user_id, to_user_id, detected_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (int(uaid), int(item_id), from_uid, int(user_id), now),
                        )
                        move["id"] = cur.lastrowid
                        moves.append(move)
                        conn.execute(
                            """
                            INSERT INTO traders (user_id, username, last_ad_at, last_scanned_at, scan_count)
                            VALUES (?, '', ?, 0, 0)
                            ON CONFLICT(user_id) DO UPDATE SET last_ad_at = MAX(traders.last_ad_at, ?)
                            """,
                            (from_uid, now, now),
                        )

                conn.executemany(
                    """
                    INSERT INTO owners (uaid, item_id, user_id, seen_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(uaid) DO UPDATE SET
                        item_id = excluded.item_id,
                        user_id = excluded.user_id,
                        seen_at = excluded.seen_at
                    """,
                    [(int(uaid), int(item_id), int(user_id), now) for uaid, item_id in uaid_to_item.items()],
                )

            incoming_counterparties = {int(m["from_user_id"]) for m in moves if int(m["from_user_id"]) > 0}
            if len(incoming_counterparties) != 1:
                recent_in = conn.execute(
                    """
                    SELECT DISTINCT from_user_id FROM uaid_moves
                    WHERE to_user_id = ? AND detected_at >= ? AND matched_trade_id IS NULL AND from_user_id > 0
                    """,
                    (int(user_id), now - 600),
                ).fetchall()
                for r in recent_in:
                    incoming_counterparties.add(int(r["from_user_id"]))

            recent_out_recipients = conn.execute(
                """
                SELECT DISTINCT to_user_id FROM uaid_moves
                WHERE from_user_id = ? AND detected_at >= ? AND matched_trade_id IS NULL AND to_user_id > 0
                """,
                (int(user_id), now - 600),
            ).fetchall()
            for r in recent_out_recipients:
                incoming_counterparties.add(int(r["to_user_id"]))

            current_uaids = {int(u) for u in (uaid_to_item or {}).keys()}
            departed_uaids = [u for u in prev_owned.keys() if u not in current_uaids]
            if departed_uaids and len(incoming_counterparties) == 1:
                target_cp = next(iter(incoming_counterparties))
                for dep_uaid in departed_uaids[:4]:
                    dep_row = prev_owned[dep_uaid]
                    dep_iid = int(dep_row["item_id"])
                    already = conn.execute(
                        """
                        SELECT 1 FROM uaid_moves
                        WHERE uaid = ? AND from_user_id = ? AND detected_at >= ?
                        """,
                        (dep_uaid, int(user_id), now - 900),
                    ).fetchone()
                    if not already:
                        cur = conn.execute(
                            """
                            INSERT INTO uaid_moves (uaid, item_id, from_user_id, to_user_id, detected_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (dep_uaid, dep_iid, int(user_id), target_cp, now),
                        )
                        moves.append(
                            {
                                "id": cur.lastrowid,
                                "uaid": dep_uaid,
                                "item_id": dep_iid,
                                "from_user_id": int(user_id),
                                "to_user_id": target_cp,
                                "detected_at": now,
                            }
                        )
                        conn.execute(
                            "UPDATE owners SET user_id = ?, seen_at = ? WHERE uaid = ?",
                            (target_cp, now, dep_uaid),
                        )

            conn.execute(
                """
                UPDATE traders
                SET last_scanned_at = ?,
                    scan_count = scan_count + 1,
                    last_ad_at = CASE WHEN ? > 0 THEN ? ELSE last_ad_at END
                WHERE user_id = ?
                """,
                (now, len(moves), now, int(user_id)),
            )
        return moves

    def get_unmatched_moves(self, since_ts: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT * FROM uaid_moves
                WHERE matched_trade_id IS NULL AND detected_at >= ?
                ORDER BY detected_at ASC
                """,
                (since_ts,),
            )
            return [dict(r) for r in cur.fetchall()]

    def insert_trade(
        self,
        user_a_id: int,
        user_a_name: str,
        user_b_id: int,
        user_b_name: str,
        items_a: List[Dict[str, Any]],
        items_b: List[Dict[str, Any]],
        value_a: int,
        rap_a: int,
        value_b: int,
        rap_b: int,
        op_value: int,
        op_rap: int,
        trade_type: str = "SWAP",
        detected_at: Optional[int] = None,
        move_ids: Optional[List[int]] = None,
        predicted_op: Optional[int] = None,
    ) -> int:
        now = detected_at if detected_at is not None else int(time.time())
        with self._connect() as conn:
            dup = conn.execute(
                """
                SELECT id FROM trades
                WHERE user_a_name = ? AND user_b_name = ?
                  AND value_a = ? AND value_b = ?
                  AND ABS(detected_at - ?) <= 86400
                LIMIT 1
                """,
                (user_a_name, user_b_name, value_a, value_b, now),
            ).fetchone()
            if dup:
                return int(dup["id"])
            cur = conn.execute(
                """
                INSERT INTO trades (
                    user_a_id, user_a_name, user_b_id, user_b_name,
                    items_a_json, items_b_json, value_a, rap_a, value_b, rap_b,
                    op_value, op_rap, trade_type, detected_at, predicted_op
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_a_id,
                    user_a_name,
                    user_b_id,
                    user_b_name,
                    json.dumps(items_a),
                    json.dumps(items_b),
                    value_a,
                    rap_a,
                    value_b,
                    rap_b,
                    op_value,
                    op_rap,
                    trade_type,
                    now,
                    predicted_op,
                ),
            )
            trade_id = cur.lastrowid

            for idx, it in enumerate(items_a):
                iid_a = int(it.get("item_id", 0))
                if iid_a <= 0:
                    continue
                share = (it["value"] / value_a) if value_a > 0 else (1.0 / len(items_a))
                realized_op = int(round((value_b - value_a) * share))
                conn.execute(
                    """
                    INSERT OR REPLACE INTO trade_items (
                        trade_id, item_id, side, uaid, item_value, item_rap,
                        other_side_value, side_total_value, realized_op, detected_at
                    ) VALUES (?, ?, 'A', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id,
                        iid_a,
                        it.get("uaid", idx),
                        it["value"],
                        it["rap"],
                        value_b,
                        value_a,
                        realized_op,
                        now,
                    ),
                )

            for idx, it in enumerate(items_b):
                iid_b = int(it.get("item_id", 0))
                if iid_b <= 0:
                    continue
                share = (it["value"] / value_b) if value_b > 0 else (1.0 / len(items_b))
                realized_op = int(round((value_a - value_b) * share))
                conn.execute(
                    """
                    INSERT OR REPLACE INTO trade_items (
                        trade_id, item_id, side, uaid, item_value, item_rap,
                        other_side_value, side_total_value, realized_op, detected_at
                    ) VALUES (?, ?, 'B', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id,
                        iid_b,
                        it.get("uaid", idx),
                        it["value"],
                        it["rap"],
                        value_a,
                        value_b,
                        realized_op,
                        now,
                    ),
                )

            if move_ids:
                placeholders = ",".join("?" for _ in move_ids)
                conn.execute(
                    f"UPDATE uaid_moves SET matched_trade_id = ? WHERE id IN ({placeholders})",
                    [trade_id, *move_ids],
                )

            return trade_id

    def get_proofs_for_item(self, item_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*, ti.side, ti.realized_op, ti.uaid AS matched_uaid
                FROM trade_items ti
                JOIN trades t ON t.id = ti.trade_id
                WHERE ti.item_id = ?
                ORDER BY t.detected_at DESC, t.id DESC
                LIMIT ?
                """,
                (item_id, limit),
            ).fetchall()
            results = []
            seen_trades = set()
            for r in rows:
                if r["id"] in seen_trades:
                    continue
                seen_trades.add(r["id"])
                d = dict(r)
                d["items_a"] = json.loads(d["items_a_json"])
                d["items_b"] = json.loads(d["items_b_json"])
                if (
                    not d["items_a"]
                    or not d["items_b"]
                    or any(int(x.get("item_id", 0)) <= 0 for x in d["items_a"] + d["items_b"])
                ):
                    continue
                if not d.get("user_a_name") or str(d["user_a_name"]).startswith("User_"):
                    d["user_a_name"] = self.get_trader_name(int(d["user_a_id"]))
                if not d.get("user_b_name") or str(d["user_b_name"]).startswith("User_"):
                    d["user_b_name"] = self.get_trader_name(int(d["user_b_id"]))
                results.append(d)
            return results

    def get_recent_trades(self, limit: int = 25, offset: int = 0) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY detected_at DESC, id DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            ).fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["items_a"] = json.loads(d["items_a_json"])
                d["items_b"] = json.loads(d["items_b_json"])
                if (
                    not d["items_a"]
                    or not d["items_b"]
                    or any(int(x.get("item_id", 0)) <= 0 for x in d["items_a"] + d["items_b"])
                ):
                    continue
                if not d.get("user_a_name") or str(d["user_a_name"]).startswith("User_"):
                    d["user_a_name"] = self.get_trader_name(int(d["user_a_id"]))
                if not d.get("user_b_name") or str(d["user_b_name"]).startswith("User_"):
                    d["user_b_name"] = self.get_trader_name(int(d["user_b_id"]))
                res.append(d)
            return res

    def get_total_trades_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])

    def get_item_hoard_stats(self, item_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            total_tracked = int(
                conn.execute(
                    "SELECT COUNT(*) FROM owners WHERE item_id = ?", (int(item_id),)
                ).fetchone()[0]
            )
            if total_tracked <= 0:
                return {
                    "total_tracked": 0,
                    "hoarder_count": 0,
                    "hoarded_copies": 0,
                    "hoard_percent": 0.0,
                    "top_hoard_count": 0,
                }
            rows = conn.execute(
                """
                SELECT user_id, COUNT(*) AS cnt
                FROM owners
                WHERE item_id = ?
                GROUP BY user_id
                HAVING cnt >= 3
                ORDER BY cnt DESC
                """,
                (int(item_id),),
            ).fetchall()
            hoarder_count = len(rows)
            hoarded_copies = sum(int(r["cnt"]) for r in rows)
            top_hoard_count = int(rows[0]["cnt"]) if rows else 0
            hoard_pct = round((hoarded_copies / max(1, total_tracked)) * 100.0, 1)
            return {
                "total_tracked": total_tracked,
                "hoarder_count": hoarder_count,
                "hoarded_copies": hoarded_copies,
                "hoard_percent": hoard_pct,
                "top_hoard_count": top_hoard_count,
            }

    def get_sales_cache(self, item_id: int, max_age_seconds: int = 1800) -> Optional[Dict[str, Any]]:
        now = int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM item_sales_cache WHERE item_id = ?", (int(item_id),)
            ).fetchone()
            if not row or (now - int(row["updated_at"])) > max_age_seconds:
                return None
            d = dict(row)
            d["recent_points"] = json.loads(d["recent_points_json"])
            return d

    def upsert_sales_cache(
        self,
        item_id: int,
        avg_price_7d: int,
        avg_price_30d: int,
        daily_volume: float,
        lowest_resale: int,
        recent_points: List[Dict[str, Any]],
    ) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO item_sales_cache (
                    item_id, avg_price_7d, avg_price_30d, daily_volume,
                    lowest_resale, recent_points_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    avg_price_7d=excluded.avg_price_7d,
                    avg_price_30d=excluded.avg_price_30d,
                    daily_volume=excluded.daily_volume,
                    lowest_resale=excluded.lowest_resale,
                    recent_points_json=excluded.recent_points_json,
                    updated_at=excluded.updated_at
                """,
                (
                    int(item_id),
                    int(avg_price_7d),
                    int(avg_price_30d),
                    float(daily_volume),
                    int(lowest_resale),
                    json.dumps(recent_points),
                    now,
                ),
            )

    def record_live_sales(self, activities: List[Dict[str, Any]]) -> int:
        if not activities:
            return 0
        rows = []
        for a in activities:
            old_r = int(a["old_rap"])
            new_r = int(a["new_rap"])
            est_price = max(1, (new_r * 10) - (old_r * 9))
            rows.append(
                (
                    int(a["activity_id"]),
                    int(a["item_id"]),
                    old_r,
                    new_r,
                    est_price,
                    int(a["timestamp"]),
                )
            )
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO live_sales (
                    activity_id, item_id, old_rap, new_rap, estimated_sale_price, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def get_live_sales_for_item(self, item_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM live_sales WHERE item_id = ? ORDER BY timestamp DESC LIMIT ?",
                (int(item_id), int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> Dict[str, int]:
        with self._connect() as conn:
            items_cnt = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            roli_cnt = conn.execute(
                "SELECT COUNT(*) FROM items WHERE source = 'rolimons'"
            ).fetchone()[0]
            rblx_only_cnt = conn.execute(
                "SELECT COUNT(*) FROM items WHERE source != 'rolimons'"
            ).fetchone()[0]
            traders_cnt = conn.execute("SELECT COUNT(*) FROM traders").fetchone()[0]
            scanned_cnt = conn.execute(
                "SELECT COUNT(*) FROM traders WHERE scan_count > 0"
            ).fetchone()[0]
            owners_cnt = conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
            trades_cnt = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            return {
                "items": items_cnt,
                "rolimons_items": roli_cnt,
                "roblox_only_items": rblx_only_cnt,
                "traders": traders_cnt,
                "scanned_traders": scanned_cnt,
                "tracked_uaids": owners_cnt,
                "proofs": trades_cnt,
            }
