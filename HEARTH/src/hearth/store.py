"""Transactional edge storage and at-least-once delivery. Never overwrite a pending event."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import canonical, digest

GENESIS = "0" * 64


class Store:
    def __init__(self, path: str | Path, config_hash: str):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("use a temporary file: separate SQLite connections must share state")
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(Path(__file__).with_name("edge.sql").read_text())
        with self.transaction() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='config_hash'").fetchone()
            if row and row[0] != config_hash:
                raise ValueError("configuration changed; explicit migration or a new database is required")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('config_hash', ?)", (config_hash,))
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def append(db: sqlite3.Connection, kind: str, batch_id: str, payload: dict[str, Any],
               timestamp: str) -> int:
        previous = db.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev_hash = (previous[0] + 1, previous[1]) if previous else (1, GENESIS)
        body = {"encoding": "hearth-json-v1", "seq": seq, "ts": timestamp,
                "kind": kind, "batch_id": batch_id, "payload": payload, "prev_hash": prev_hash}
        db.execute("INSERT INTO ledger VALUES (?, ?, ?, ?)",
                   (seq, canonical(body), prev_hash, digest(body)))
        db.execute("INSERT INTO outbox(seq) VALUES (?)", (seq,))
        return seq

    def events(self, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid page")
        with self.connection() as db:
            rows = db.execute("SELECT * FROM ledger WHERE seq > ? ORDER BY seq LIMIT ?",
                              (after, limit)).fetchall()
        return [{**json.loads(row["body_json"]), "hash": row["hash"]} for row in rows]

    def verify(self, expected_head: str | None = None) -> dict[str, Any]:
        previous, count = GENESIS, 0
        with self.connection() as db:
            for row in db.execute("SELECT * FROM ledger ORDER BY seq"):
                count += 1
                try:
                    body = json.loads(row["body_json"])
                    valid = (row["seq"] == count and body["seq"] == count
                             and row["prev_hash"] == previous and body["prev_hash"] == previous
                             and digest(body) == row["hash"])
                except (ValueError, KeyError, TypeError):
                    valid = False
                if not valid:
                    return {"valid": False, "first_bad_seq": row["seq"], "checked": count}
                previous = row["hash"]
        return {"valid": expected_head in (None, previous), "checked": count, "head": previous,
                "externally_anchored": expected_head is not None}

    def pending_count(self) -> int:
        with self.connection() as db:
            return db.execute("SELECT count(*) FROM outbox WHERE delivered_at IS NULL").fetchone()[0]

    def deliver(self, sender: Callable[[dict[str, Any]], None], limit: int = 100) -> int:
        """One worker per gateway. Receiver MUST deduplicate on ledger hash.

        Sender must have bounded network timeouts. Crash after remote success and before
        local acknowledgement intentionally causes a retry of the same event.
        """
        if not 1 <= limit <= 1000:
            raise ValueError("invalid delivery budget")
        delivered = 0
        with self.connection() as db:
            rows = db.execute("SELECT l.* FROM ledger l JOIN outbox o USING(seq) "
                              "WHERE o.delivered_at IS NULL ORDER BY seq LIMIT ?", (limit,)).fetchall()
        for row in rows:
            with self.transaction() as db:
                db.execute("UPDATE outbox SET attempts=attempts+1 WHERE seq=?", (row["seq"],))
            sender({**json.loads(row["body_json"]), "hash": row["hash"]})
            with self.transaction() as db:
                db.execute("UPDATE outbox SET delivered_at=? WHERE seq=? AND delivered_at IS NULL",
                           (datetime.now(UTC).isoformat(), row["seq"]))
            delivered += 1
        return delivered
