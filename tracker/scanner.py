from typing import Any, Dict, List, Optional
from data.roblox import RobloxClient
from data.rolimons import RolimonsClient
from db import Database
from tracker.matcher import match_moves_into_trades


class InventoryScanner:
    def __init__(
        self,
        db: Database,
        rolimons: RolimonsClient,
        roblox: RobloxClient,
        match_window_seconds: int = 7200,
    ):
        self.db = db
        self.rolimons = rolimons
        self.roblox = roblox
        self.match_window_seconds = match_window_seconds
        self._priority_users: List[int] = []

    def fetch_user_inventory(
        self, user_id: int, items_catalog: Optional[Dict[int, Dict[str, Any]]] = None
    ) -> Optional[Dict[int, int]]:
        rap_map: Dict[int, int] = {}
        inv = self.roblox.fetch_collectibles(user_id, rap_out=rap_map)

        if rap_map:
            for iid, rap_val in rap_map.items():
                if items_catalog and iid in items_catalog:
                    it = items_catalog[iid]
                    if it.get("source") != "rolimons":
                        it["rap"] = rap_val
                        if it.get("manual_value") is None:
                            it["value"] = rap_val
                        self.db.update_item_rap_from_scan(iid, rap_val)
        return inv

    def scan_one_step(
        self,
        items_catalog: Dict[int, Dict[str, Any]],
        predict_op_fn=None,
    ) -> Dict[str, Any]:
        user_id: Optional[int] = None
        if self._priority_users:
            user_id = self._priority_users.pop(0)
        else:
            batch = self.db.get_next_traders_to_scan(limit=1)
            if batch:
                user_id = int(batch[0]["user_id"])

        if user_id is None:
            return {"scanned_user": None, "moves": 0, "new_trades": []}

        inv = self.fetch_user_inventory(user_id, items_catalog=items_catalog)
        if inv is None:
            self.db.record_inventory_snapshot(user_id, {})
            return {"scanned_user": user_id, "moves": 0, "new_trades": []}

        moves = self.db.record_inventory_snapshot(user_id, inv)

        for m in moves:
            counterparty = int(m["from_user_id"])
            if counterparty != user_id:
                if counterparty in self._priority_users:
                    self._priority_users.remove(counterparty)
                self._priority_users.insert(0, counterparty)

        new_trades = []
        if moves:
            new_trades = match_moves_into_trades(
                self.db,
                items_catalog,
                match_window_seconds=self.match_window_seconds,
                predict_op_fn=predict_op_fn,
            )

        return {
            "scanned_user": user_id,
            "moves": len(moves),
            "new_trades": new_trades,
        }
