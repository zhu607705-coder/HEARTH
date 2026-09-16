"""Seeded synthetic sensor source; synthetic truth never enters the inference contract."""
from __future__ import annotations

import math
import random
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .engine import Engine
from .models import Batch, DemoRequest, Metric, Reading, Settings, digest
from .physics import gompertz_log_count, rate, remaining_hours
from .store import Store


def simulate(request: DemoRequest, settings: Settings, start: datetime | None = None) -> dict[str, Any]:
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    product_id = next(iter(settings.products))
    product = settings.products[product_id]
    # A compressed simulation has a different sampling cadence from the physical gateway.
    policy = settings.policy.model_copy(update={
        "stale_seconds": min(3600.0, request.interval_minutes * 60 * 1.05)})
    config = Settings.model_validate({**settings.model_dump(), "mode": "demo", "policy": policy.model_dump()})
    dt = request.interval_minutes / 60
    count = math.ceil(request.hours / dt)
    rng = random.Random(request.seed)
    batch = Batch(batch_id="demo-batch", market_id="demo-market", stall_id="demo-stall",
                  product_id=product_id, device_ids=("demo-hook",), created_at=start)
    boot = uuid5(NAMESPACE_URL, f"hearth:{request.seed}:{request.scenario}")
    samples, exposure, previous_h = [], 0.0, 0.0
    temperature_c = {"normal": 30.0, "cooled": 25.0, "heatwave": 36.0,
                     "outage": 30.0, "sensor_fault": 30.0}[request.scenario]
    # These ambient/product-equality and cooling assumptions are illustrative only.
    with tempfile.TemporaryDirectory(prefix="hearth-demo-") as directory:
        store = Store(Path(directory) / "edge.db", digest(config.model_dump(mode="json")))
        engine = Engine(store, config)
        engine.register(batch, start)
        red_at = None
        for index in range(count + 1):
            elapsed = min(request.hours, index * dt)
            now = start + timedelta(hours=elapsed)
            exposure += (elapsed - previous_h) * rate(temperature_c, product)
            previous_h = elapsed
            log_count = gompertz_log_count(exposure, product)
            q_truth = max(0.0, min(1.0, (product.log_count_quality_limit - log_count) /
                                   (product.log_count_quality_limit - product.log_count_initial)))
            concentration = max(0.0, (10 ** (log_count - product.log_count_initial) - 1) * 0.01)
            noisy = max(0.0, round((concentration + elapsed * 0.03 + rng.gauss(0, 0.2)) / 0.1) * 0.1)
            nh3_spec = settings.metrics["nh3_ppm"]
            saturated = not nh3_spec.minimum <= noisy <= nh3_spec.maximum
            noisy = min(nh3_spec.maximum, max(nh3_spec.minimum, noisy))
            offline = request.scenario == "outage" and 0.30 * request.hours <= elapsed < 0.55 * request.hours
            faulty = request.scenario == "sensor_fault" and 0.30 * request.hours <= elapsed < 0.55 * request.hours
            if not offline:
                reading = Reading(event_id=uuid5(boot, str(index)), boot_id=boot, sequence=index,
                                  market_id=batch.market_id, stall_id=batch.stall_id,
                                  batch_id=batch.batch_id, device_id=batch.device_ids[0], observed_at=now,
                                  source="synthetic", metrics=(
                    Metric(metric="temperature_c", unit="degC", value=temperature_c),
                    Metric(metric="relative_humidity_pct", unit="percent", value=80.0),
                    Metric(metric="nh3_ppm", unit="ppm", value=noisy if not faulty else 0.0,
                           quality="saturated" if faulty or saturated else "ok"),
                    Metric(metric="co2_ppm", unit="ppm", value=min(999999.0, 420.0 + 5 * concentration)),
                    Metric(metric="ethylene_ppm", unit="ppm", value=min(99999.0, concentration / 10)),
                ))
                engine.ingest([reading], now)
            state = engine.state(batch.batch_id, now)
            if state["state"] == "red" and red_at is None:
                red_at = elapsed
            samples.append({"elapsed_h": elapsed, "temperature_c": temperature_c,
                            "nh3_ppm": None if offline else noisy, "synthetic_truth_quality": q_truth,
                            "quality_index": state["quality_index"], "remaining_hours": state["remaining_hours"],
                            "state": state["state"], "health": state["health"], "reasons": state["reasons"]})
        integrity = store.verify()
        pending = store.pending_count()
    return {"schema_version": "1.0", "synthetic": True, "safety_certified": False,
            "request": request.model_dump(), "assumed_product_temperature_c": temperature_c,
            "constant_temperature_quality_lifetime_h": remaining_hours(0, temperature_c, product),
            "cooling_lifetime_ratio_25_vs_30": remaining_hours(0, 25, product) / remaining_hours(0, 30, product),
            "first_red_h": red_at, "samples": samples, "ledger": integrity,
            "pending_outbox_events": pending,
            "limits": ["No validated food-safety prediction", "No measured cooling benefit",
                       "No measured waste, revenue or carbon-credit benefit"], "extensions": {}}
