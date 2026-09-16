from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from conftest import START, batch, reading
from hearth.engine import DomainError, Engine
from hearth.models import Metric, digest
from hearth.store import Store


def test_unknown_is_yellow_and_green_is_not_a_certificate(engine):
    state = engine.register(batch(), START)
    assert state["state"] == "yellow"
    assert state["remaining_hours"] is None
    engine.ingest([reading()], START)
    state = engine.state("batch-1", START)
    assert state["state"] == "green"
    assert not state["sale_allowed"] and not state["safety_certified"]


def test_deadline_ignores_missing_voc_and_votes(engine):
    engine.register(batch(expires_at=START + timedelta(minutes=5), device_ids=("hook-1", "hook-2", "hook-3")), START)
    result = engine.state("batch-1", START + timedelta(minutes=5))
    assert result["state"] == "red"
    assert result["red_latched"]


def test_staleness_without_any_new_packet(engine):
    engine.register(batch(), START)
    engine.ingest([reading()], START)
    result = engine.state("batch-1", START + timedelta(seconds=91))
    assert result["state"] == "yellow"
    assert result["health"] == "degraded" and result["remaining_hours"] is None
    assert engine.state("batch-1", START + timedelta(days=1))["state"] == "red"


def test_red_latch_survives_restart_close_and_device_reuse(engine, config):
    engine.register(batch(expires_at=START + timedelta(seconds=5)), START)
    engine.ingest([reading()], START)
    later = START + timedelta(seconds=6)
    assert engine.state("batch-1", later)["red_latched"]
    reloaded = Engine(Store(engine.store.path, digest(config.model_dump(mode="json"))), config)
    reloaded.ingest([reading(later, sequence=1, temperature=4)], later)
    assert reloaded.state("batch-1", later)["state"] == "red"
    reloaded.close("batch-1", "operator confirmed removal", later)
    assert reloaded.state("batch-1", later)["state"] == "closed"
    assert reloaded.state("batch-1", later)["red_latched"]
    reloaded.register(batch(batch_id="batch-2", created_at=later), later)
    assert reloaded.state("batch-2", later)["state"] == "yellow"
    reloaded.ingest([reading(later, sequence=2, batch_id="batch-2")], later)
    assert reloaded.state("batch-2", later)["state"] == "green"
    old = reading(later, sequence=3)
    assert reloaded.ingest([old], later)[0]["disposition"] == "history"
    assert reloaded.state("batch-2", later)["state"] == "green"


def test_cannot_reassign_live_device(engine):
    engine.register(batch(), START)
    with pytest.raises(DomainError, match="active batch"):
        engine.register(batch(batch_id="other"), START)
    with pytest.raises(DomainError, match="unknown batch"):
        engine.state("other", START)


def test_missing_node_prevents_green_but_not_hard_stop(engine):
    engine.register(batch(device_ids=("hook-1", "hook-2")), START)
    engine.ingest([reading()], START)
    assert engine.state("batch-1", START)["state"] == "yellow"
    engine.ingest([reading(device_id="hook-2")], START)
    assert engine.state("batch-1", START)["state"] == "green"


def test_duplicates_and_conflicting_retry(engine):
    engine.register(batch(), START)
    item = reading()
    assert engine.ingest([item], START)[0]["disposition"] == "live"
    head = engine.store.verify()
    for _ in range(5):
        assert engine.ingest([item], START)[0]["disposition"] == "duplicate"
    assert engine.store.verify() == head
    with pytest.raises(DomainError, match="collision"):
        engine.ingest([item.model_copy(update={"source": "sensor"})], START)
    with pytest.raises(DomainError, match="collision"):
        engine.ingest([reading()], START)


def test_delayed_fresh_packet_after_watchdog_does_not_rewind(engine):
    engine.register(batch(), START)
    engine.ingest([reading()], START)
    engine.state("batch-1", START + timedelta(seconds=5))
    item = reading(START + timedelta(seconds=4), sequence=1)
    now = START + timedelta(seconds=6)
    assert engine.ingest([item], now)[0]["disposition"] == "live"
    assert engine.state("batch-1", now)["state"] == "green"


def test_history_does_not_change_live_state(engine):
    engine.register(batch(), START)
    engine.ingest([reading()], START)
    now = START + timedelta(seconds=30)
    engine.ingest([reading(now, sequence=2)], now)
    before = engine.state("batch-1", now)
    late = reading(START + timedelta(seconds=15), sequence=1, temperature=4)
    assert engine.ingest([late], now)[0]["disposition"] == "history"
    after = engine.state("batch-1", now)
    assert after["quality_index"] == before["quality_index"]
    assert after["last_temperature_c"] == 30


def test_clock_rollback_rejected_atomically(engine):
    engine.register(batch(), START)
    engine.ingest([reading()], START)
    before = engine.store.verify()
    with pytest.raises(DomainError, match="backwards"):
        engine.tick(START - timedelta(seconds=1))
    assert engine.store.verify() == before


def test_future_small_skew_does_not_poison_gateway_clock(engine):
    engine.register(batch(), START)
    item = reading(START + timedelta(seconds=10))
    engine.ingest([item], START)
    assert engine.state("batch-1", START)["state"] == "green"
    with pytest.raises(DomainError, match="clock skew"):
        engine.ingest([reading(START + timedelta(hours=1), sequence=1)], START)


def test_packet_fault_and_out_of_domain_temperature(engine):
    engine.register(batch(), START)
    engine.ingest([reading()], START)
    now = START + timedelta(seconds=1)
    engine.ingest([reading(now, sequence=1, temperature=-10)], now)
    assert engine.state("batch-1", now)["health"] == "degraded"
    now += timedelta(seconds=1)
    metrics = (Metric(metric="temperature_c", value=30, unit="degC"),
               Metric(metric="nh3_ppm", value=0, unit="ppm", quality="saturated"))
    engine.ingest([reading(now, sequence=2, metrics=metrics)], now)
    assert engine.state("batch-1", now)["state"] == "yellow"


def test_confirmations_require_new_unique_packets_not_watchdog(engine):
    engine.register(batch(initial_quality=0.12), START)
    engine.ingest([reading()], START)
    for _ in range(20):
        assert engine.state("batch-1", START)["state"] == "yellow"
    for index in range(1, 5):
        now = START + timedelta(seconds=index * 10)
        engine.ingest([reading(now, sequence=index)], now)
    assert engine.state("batch-1", now)["state"] == "red"


def test_multi_reading_request_is_atomic(engine):
    engine.register(batch(), START)
    head = engine.store.verify()
    with pytest.raises(DomainError, match="binding"):
        engine.ingest([reading(), reading(sequence=1, stall_id="other")], START)
    assert engine.store.verify() == head
    with engine.store.connection() as db:
        assert db.execute("SELECT count(*) FROM readings").fetchone()[0] == 0


def test_source_separation_and_metric_units(engine):
    engine.register(batch(), START)
    with pytest.raises(DomainError, match="physical streams"):
        engine.ingest([reading(source="sensor")], START)
    with pytest.raises(DomainError, match="unit"):
        engine.ingest([reading(metrics=(Metric(metric="temperature_c", value=30, unit="K"),))], START)
    with pytest.raises(DomainError, match="physical range"):
        engine.ingest([reading(temperature=101)], START)


def test_parallel_identical_retries_have_one_effect(engine):
    engine.register(batch(), START)
    item = reading()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: engine.ingest([item], START)[0]["disposition"], range(40)))
    assert results.count("live") == 1
    assert results.count("duplicate") == 39
    assert engine.store.verify()["valid"]


def test_new_boot_may_reset_sequence(engine):
    engine.register(batch(), START)
    engine.ingest([reading(sequence=100)], START)
    later = START + timedelta(seconds=1)
    assert engine.ingest([reading(later, sequence=0, boot_id=uuid4())], later)[0]["disposition"] == "live"


def test_configuration_change_is_not_silent(engine, config):
    modified = config.model_copy(update={"mode": "shadow"})
    with pytest.raises(ValueError, match="configuration changed"):
        Store(engine.store.path, digest(modified.model_dump(mode="json")))
