"""
calibrate.py — No fake proofs are ever seeded into the production database.
Reference test cases live strictly inside `tests/`.
"""
from typing import Any, Dict
from db import Database


def seed_reference_calibration_data(db: Database, items_catalog: Dict[int, Dict[str, Any]]) -> None:
    # Intentionally a no-op in production so the database only contains real proofs.
    return None
