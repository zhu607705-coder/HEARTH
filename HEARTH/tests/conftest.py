from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from hearth.engine import Engine
from hearth.models import Batch, Metric, Reading, digest, load_settings
from hearth.store import Store

START = datetime(2026, 1, 1, tzinfo=UTC)
BOOT = UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
def config():
    return load_settings()


@pytest.fixture
def engine(tmp_path, config):
    return Engine(Store(tmp_path / "edge.db", digest(config.model_dump(mode="json"))), config)


def batch(**changes):
    return Batch(**({"batch_id": "batch-1", "market_id": "market-1", "stall_id": "stall-1",
                    "product_id": "demo_meat", "device_ids": ("hook-1",), "created_at": START} | changes))


def reading(at=START, sequence=0, temperature=30.0, **changes):
    return Reading(**({"event_id": uuid4(), "boot_id": BOOT, "sequence": sequence,
                       "market_id": "market-1", "stall_id": "stall-1", "batch_id": "batch-1",
                       "device_id": "hook-1", "observed_at": at, "source": "synthetic",
                       "metrics": (Metric(metric="temperature_c", unit="degC", value=temperature),)} | changes))
