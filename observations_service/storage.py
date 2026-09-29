"""Own SQLite archive and stable forecast associations, separate from model history."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .contract import forecast_curve, measurement, report


def utc_now() -> float:
    return datetime.now(timezone.utc).timestamp()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, first_seen REAL NOT NULL, curve_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forecast_points (
                    batch_id TEXT NOT NULL REFERENCES batches(id), target INTEGER NOT NULL,
                    value REAL NOT NULL, PRIMARY KEY(batch_id, target)
                );
                CREATE INDEX IF NOT EXISTS forecast_target ON forecast_points(target);
                CREATE TABLE IF NOT EXISTS observations (
                    target INTEGER PRIMARY KEY, batch_id TEXT REFERENCES batches(id),
                    input_json TEXT NOT NULL, response_json TEXT NOT NULL, updated_at REAL NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def archive(self, payload: dict, first_seen: float | None = None) -> str:
        curve = forecast_curve(payload)
        encoded = json.dumps(curve, separators=(",", ":"), allow_nan=False)
        batch_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        seen = utc_now() if first_seen is None else first_seen
        if not math.isfinite(seen):
            raise ValueError("first_seen must be finite")
        with self.connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO batches VALUES (?, ?, ?)", (batch_id, seen, encoded)
            ).rowcount
            if inserted:
                connection.executemany("INSERT INTO forecast_points VALUES (?, ?, ?)",
                                       [(batch_id, target, value) for target, value in curve])
        return batch_id

    def receive(self, payload: dict) -> dict:
        item = measurement(payload)
        target = item["slot"]
        with self.connect() as connection:
            # Serialize retries/corrections so that they cannot select different batches.
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT batch_id FROM observations WHERE target=?", (target,)
            ).fetchone()
            match = None
            if previous and previous[0]:
                match = connection.execute(
                    "SELECT batch_id, value FROM forecast_points WHERE batch_id=? AND target=?",
                    (previous[0], target),
                ).fetchone()
            else:
                match = connection.execute("""
                    SELECT p.batch_id, p.value FROM forecast_points p
                    JOIN batches b ON b.id=p.batch_id
                    WHERE p.target=? AND b.first_seen < ?
                    ORDER BY b.first_seen DESC, b.id DESC LIMIT 1
                """, (target, target)).fetchone()
            response = report(item, match[1] if match else None)
            connection.execute("""
                INSERT INTO observations VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(target) DO UPDATE SET batch_id=excluded.batch_id,
                    input_json=excluded.input_json, response_json=excluded.response_json,
                    updated_at=excluded.updated_at
            """, (target, match[0] if match else None,
                  json.dumps({"timestamp": item["timestamp"], "values": item["values"]}, allow_nan=False),
                  json.dumps(response, allow_nan=False), utc_now()))
        return response

    def counts(self) -> dict:
        with self.connect() as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("batches", "observations")}
