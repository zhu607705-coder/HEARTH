import json
import sqlite3
from datetime import timedelta

import pytest

from conftest import START, batch, reading
from hearth.models import canonical


def test_offline_delivery_retry_and_receiver_deduplication(engine):
    engine.register(batch(), START)
    for index in range(30):
        now = START + timedelta(minutes=index)
        engine.ingest([reading(now, sequence=index)], now)
    pending = engine.store.pending_count()
    delivered = {}
    def failed_sender(event):
        raise ConnectionError("simulated WAN outage")
    with pytest.raises(ConnectionError):
        engine.store.deliver(failed_sender)
    assert engine.store.pending_count() == pending
    # Simulate remote receipt followed by a crash before local acknowledgement.
    def crash_after_receipt(event):
        delivered[event["hash"]] = event
        raise ConnectionError("connection dropped after remote commit")
    with pytest.raises(ConnectionError):
        engine.store.deliver(crash_after_receipt)
    assert engine.store.pending_count() == pending
    engine.store.deliver(lambda event: delivered.setdefault(event["hash"], event), limit=1000)
    assert len(delivered) == pending
    assert engine.store.pending_count() == 0
    assert list(event["seq"] for event in delivered.values()) == list(range(1, pending + 1))
    assert engine.store.verify()["valid"]


def test_append_only_trigger(engine):
    engine.register(batch(), START)
    with engine.store.transaction() as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM ledger")


def test_tamper_detection_with_database_admin_attack(engine):
    engine.register(batch(), START)
    with engine.store.transaction() as db:
        db.execute("DROP TRIGGER ledger_no_update")
        raw = db.execute("SELECT body_json FROM ledger WHERE seq=1").fetchone()[0]
        body = json.loads(raw)
        body["payload"]["stall_id"] = "tampered"
        db.execute("UPDATE ledger SET body_json=? WHERE seq=1", (canonical(body),))
    assert not engine.store.verify()["valid"]


def test_tail_truncation_needs_external_checkpoint(engine):
    engine.register(batch(), START)
    engine.ingest([reading()], START)
    checkpoint = engine.store.verify()["head"]
    with engine.store.transaction() as db:
        db.execute("DELETE FROM outbox")
        db.execute("DROP TRIGGER ledger_no_delete")
        db.execute("DELETE FROM ledger WHERE seq=(SELECT MAX(seq) FROM ledger)")
    assert engine.store.verify()["valid"]  # an internally consistent prefix cannot prove completeness
    assert not engine.store.verify(expected_head=checkpoint)["valid"]


def test_paginated_event_export(engine):
    engine.register(batch(), START)
    engine.ingest([reading()], START)
    first = engine.store.events(limit=1)
    rest = engine.store.events(after=first[0]["seq"])
    assert [row["seq"] for row in first + rest] == list(range(1, engine.store.verify()["checked"] + 1))
