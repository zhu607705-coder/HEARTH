"""Single-authority, edge-local decisions with transactional state and audit events."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .models import Batch, Reading, Settings, State, canonical, digest, utc
from .physics import quality, rate, remaining_hours
from .store import Store


class DomainError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class Engine:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings

    def register(self, batch: Batch, now: datetime) -> dict[str, Any]:
        now = utc(now)
        if batch.product_id not in self.settings.products:
            raise DomainError("unknown product configuration")
        if batch.created_at > now:
            raise DomainError("batch creation cannot be in the future")
        product = self.settings.products[batch.product_id]
        snapshot = {"state": State.YELLOW.value, "health": "unknown", "red_latched": False,
                    "reasons": ["no_usable_sensor"], "closed": False, "version": 0,
                    "exposure_eq_h": (1 - batch.initial_quality) * product.shelf_life_reference_h,
                    "last_at": batch.created_at.isoformat(), "last_temperature_c": None,
                    "last_temperature_at": None, "red_count": 0, "last_confirmation_at": None,
                    "quality_index": batch.initial_quality, "remaining_hours": None,
                    "sale_allowed": False, "safety_certified": False, "mode": self.settings.mode}
        try:
            with self.store.transaction() as db:
                db.execute("INSERT INTO batches VALUES (?, ?, ?, ?, ?)",
                           (batch.batch_id, batch.market_id, batch.stall_id,
                            canonical(batch.model_dump(mode="json")), canonical(snapshot)))
                for device_id in batch.device_ids:
                    old = db.execute("SELECT batch_id FROM devices WHERE device_id=?", (device_id,)).fetchone()
                    if old is not None:
                        _, old_state = self._load(db, old[0])
                        if not old_state["closed"]:
                            raise DomainError("device still belongs to an active batch", 409)
                        db.execute("UPDATE devices SET batch_id=?,latest_json=NULL WHERE device_id=?",
                                   (batch.batch_id, device_id))
                    else:
                        db.execute("INSERT INTO devices(device_id,batch_id) VALUES (?, ?)",
                                   (device_id, batch.batch_id))
                self.store.append(db, "batch.registered", batch.batch_id,
                                  batch.model_dump(mode="json"), now.isoformat())
                self._decide(db, batch, snapshot, now, confirmation=False)
        except sqlite3.IntegrityError as exc:
            raise DomainError("batch or device already registered", 409) from exc
        return snapshot

    @staticmethod
    def _load(db: sqlite3.Connection, batch_id: str) -> tuple[Batch, dict[str, Any]]:
        row = db.execute("SELECT config_json,state_json FROM batches WHERE batch_id=?",
                         (batch_id,)).fetchone()
        if row is None:
            raise DomainError("unknown batch", 404)
        return Batch.model_validate_json(row[0]), json.loads(row[1])

    def _advance(self, batch: Batch, snapshot: dict[str, Any], now: datetime) -> None:
        previous = datetime.fromisoformat(snapshot["last_at"])
        if now < previous:
            raise DomainError("clock moved backwards; state will not be rewound", 409)
        if snapshot["closed"]:
            return
        product = self.settings.products[batch.product_id]
        # Causal sample-and-hold until staleness; unseen intervals use the configured
        # model-domain upper temperature and remain flagged as uncertain.
        known_h = 0.0
        if snapshot["health"] == "ok" and snapshot["last_temperature_at"] is not None:
            expires = datetime.fromisoformat(snapshot["last_temperature_at"]) + timedelta(
                seconds=self.settings.policy.stale_seconds)
            known_h = max(0.0, (min(now, expires) - previous).total_seconds() / 3600)
        total_h = (now - previous).total_seconds() / 3600
        exposure = (known_h * rate(snapshot["last_temperature_c"], product) if known_h else 0.0)
        exposure += max(0.0, total_h - known_h) * rate(product.valid_max_c, product)
        snapshot["exposure_eq_h"] += exposure
        snapshot["last_at"] = now.isoformat()

    def _decide(self, db: sqlite3.Connection, batch: Batch, snapshot: dict[str, Any],
                now: datetime, confirmation: bool) -> None:
        before = (snapshot["state"], snapshot["health"], tuple(snapshot["reasons"]))
        self._advance(batch, snapshot, now)
        product, policy = self.settings.products[batch.product_id], self.settings.policy
        nodes = [json.loads(row[0]) if row[0] else None for row in db.execute(
            "SELECT latest_json FROM devices WHERE batch_id=?", (batch.batch_id,))]
        healthy = [node for node in nodes if node and node["usable"] and
                   0 <= (now - datetime.fromisoformat(node["at"])).total_seconds() <= policy.stale_seconds]
        complete = len(healthy) == len(batch.device_ids)
        q = quality(snapshot["exposure_eq_h"], product)
        temperature = max((node["temperature_c"] for node in healthy), default=None)
        if temperature is not None:
            snapshot["last_temperature_c"] = temperature
            # Never refresh a sensor's freshness merely because a watchdog tick occurred.
            snapshot["last_temperature_at"] = min(node["at"] for node in healthy)
        rul = remaining_hours(snapshot["exposure_eq_h"], temperature, product) if complete else None
        reasons: list[str] = []
        if snapshot["closed"]:
            state, health, reasons = State.CLOSED, "closed", ["batch_closed"]
        else:
            expired = batch.expires_at is not None and now >= batch.expires_at
            if expired or q <= 0:
                snapshot["red_latched"] = True
                reasons.append("deadline_reached" if expired else "quality_budget_exhausted")
            if complete and q <= policy.red_enter:
                if confirmation and snapshot["last_confirmation_at"] != now.isoformat():
                    snapshot["red_count"] += 1
                    snapshot["last_confirmation_at"] = now.isoformat()
                if snapshot["red_count"] >= policy.red_confirmations:
                    snapshot["red_latched"] = True
                    reasons.append("persistent_low_quality")
            else:
                snapshot["red_count"] = 0
            health = "ok" if complete else "degraded"
            if not complete:
                reasons.append("missing_stale_or_faulty_sensor")
            if snapshot["red_latched"]:
                state = State.RED
                reasons.append("red_latched")
            elif not complete:
                state = State.YELLOW
            elif q <= policy.yellow_enter or (rul is not None and rul <= policy.warn_hours):
                state = State.YELLOW
                reasons.append("quality_warning")
            elif snapshot["state"] == State.YELLOW and q < policy.yellow_exit:
                state = State.YELLOW
                reasons.append("hysteresis")
            else:
                state = State.GREEN
                reasons.append("model_quality_only")
        snapshot.update(state=state.value, health=health, reasons=sorted(set(reasons)),
                        quality_index=q, remaining_hours=rul, version=snapshot["version"] + 1)
        db.execute("UPDATE batches SET state_json=? WHERE batch_id=?",
                   (canonical(snapshot), batch.batch_id))
        if before != (snapshot["state"], health, tuple(snapshot["reasons"])):
            self.store.append(db, "state.changed", batch.batch_id, dict(snapshot), now.isoformat())

    def ingest(self, readings: list[Reading], now: datetime) -> list[dict[str, str]]:
        if not 1 <= len(readings) <= 256:
            raise DomainError("request must contain 1..256 readings")
        now = utc(now)
        results = []
        with self.store.transaction() as db:
            for reading in readings:
                results.append(self._ingest_one(db, reading, now))
        return results

    def _ingest_one(self, db: sqlite3.Connection, reading: Reading, now: datetime) -> dict[str, str]:
        body = reading.model_dump(mode="json")
        content_hash = digest(body)
        existing = db.execute("SELECT event_id, content_hash FROM readings WHERE event_id=? OR "
                              "(device_id=? AND boot_id=? AND sequence=?)",
                              (str(reading.event_id), reading.device_id, str(reading.boot_id),
                               reading.sequence)).fetchall()
        if existing:
            if len(existing) != 1 or existing[0][0] != str(reading.event_id) or existing[0][1] != content_hash:
                raise DomainError("event/sequence collision with different content", 409)
            return {"event_id": str(reading.event_id), "disposition": "duplicate"}
        count = db.execute("SELECT count(*) FROM readings").fetchone()[0]
        pending = db.execute("SELECT count(*) FROM outbox WHERE delivered_at IS NULL").fetchone()[0]
        if count >= self.settings.policy.max_history_rows or pending >= self.settings.policy.max_pending_events:
            raise DomainError("edge storage quota reached; export/retention intervention required", 503)
        batch, snapshot = self._load(db, reading.batch_id)
        device = db.execute("SELECT batch_id,latest_json FROM devices WHERE device_id=?", (reading.device_id,)).fetchone()
        if (not device or reading.device_id not in batch.device_ids or reading.market_id != batch.market_id
                or reading.stall_id != batch.stall_id):
            raise DomainError("device/market/stall/batch binding mismatch", 403)
        expected_source = "synthetic" if self.settings.mode == "demo" else "sensor"
        if reading.source != expected_source:
            raise DomainError("synthetic and physical streams cannot share this deployment", 403)
        age = (now - reading.observed_at).total_seconds()
        if age < -self.settings.policy.future_skew_seconds:
            raise DomainError("timestamp exceeds allowed clock skew")
        if reading.observed_at < batch.created_at:
            raise DomainError("reading predates batch")
        for metric in reading.metrics:
            spec = self.settings.metrics.get(metric.metric)
            if spec is None or metric.unit != spec.unit:
                raise DomainError("unregistered metric or wrong unit")
            if metric.value is not None and not spec.minimum <= metric.value <= spec.maximum:
                raise DomainError("metric outside configured physical range")
        latest_sequence = db.execute("SELECT MAX(sequence) FROM readings WHERE device_id=? AND boot_id=?",
                                     (reading.device_id, str(reading.boot_id))).fetchone()[0]
        device_state = json.loads(device[1]) if device[1] else None
        is_history = (snapshot["closed"] or age > self.settings.policy.stale_seconds
                      or (device_state is not None and reading.observed_at < datetime.fromisoformat(device_state["at"]))
                      or (latest_sequence is not None and reading.sequence <= latest_sequence))
        disposition = "history" if is_history else "live"
        db.execute("INSERT INTO readings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (str(reading.event_id), reading.device_id, str(reading.boot_id), reading.sequence,
                    reading.observed_at.isoformat(), now.isoformat(), content_hash, canonical(body), disposition))
        self.store.append(db, "reading." + disposition, batch.batch_id, body, now.isoformat())
        if not is_history:
            effective_at = min(reading.observed_at, now)  # small positive device skew never advances gateway time
            self._advance(batch, snapshot, now)
            temperature = next((m for m in reading.metrics if m.metric == "temperature_c"), None)
            product = self.settings.products[batch.product_id]
            usable = (temperature is not None and temperature.quality == "ok" and temperature.value is not None
                      and product.valid_min_c <= temperature.value <= product.valid_max_c
                      and all(m.quality == "ok" for m in reading.metrics))
            node = {"at": effective_at.isoformat(), "usable": usable,
                    "temperature_c": temperature.value if temperature else None}
            db.execute("UPDATE devices SET latest_json=? WHERE device_id=?", (canonical(node), reading.device_id))
            self._decide(db, batch, snapshot, now, confirmation=True)
        return {"event_id": str(reading.event_id), "disposition": disposition}

    def tick(self, now: datetime, batch_id: str | None = None) -> list[dict[str, Any]]:
        now = utc(now)
        output = []
        with self.store.transaction() as db:
            ids = [batch_id] if batch_id else [row[0] for row in db.execute("SELECT batch_id FROM batches")]
            for bid in ids:
                batch, snapshot = self._load(db, bid)
                self._decide(db, batch, snapshot, now, confirmation=False)
                output.append({"batch_id": bid, "stall_id": batch.stall_id, **snapshot})
        return output

    def state(self, batch_id: str, now: datetime) -> dict[str, Any]:
        return self.tick(now, batch_id)[0]

    def close(self, batch_id: str, reason: str, now: datetime) -> dict[str, Any]:
        now = utc(now)
        with self.store.transaction() as db:
            batch, snapshot = self._load(db, batch_id)
            if not snapshot["closed"]:
                self._advance(batch, snapshot, now)
                snapshot["closed"] = True
                self.store.append(db, "batch.closed", batch_id, {"reason": reason}, now.isoformat())
                self._decide(db, batch, snapshot, now, confirmation=False)
        return snapshot
