"""Versioned, timezone-aware contracts; all numerical values must be finite."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]


def canonical(value: object) -> str:
    """HEARTH JSON-v1 encoding, not a claim of RFC 8785 compliance."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone offset is required")
    return value.astimezone(UTC)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class Metric(Contract):
    metric: Identifier
    value: Annotated[float, Field(strict=True)] | None
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    quality: Literal["ok", "missing", "fault", "saturated", "uncalibrated"] = "ok"

    @model_validator(mode="after")
    def valid_value(self) -> Metric:
        if self.quality == "ok" and self.value is None:
            raise ValueError("an ok metric needs a value")
        if self.quality == "missing" and self.value is not None:
            raise ValueError("missing values must be null")
        return self


class Reading(Contract):
    schema_version: Literal["1.0"] = "1.0"
    event_id: UUID = Field(default_factory=uuid4)
    boot_id: UUID
    sequence: Annotated[int, Field(ge=0, strict=True)]
    market_id: Identifier
    stall_id: Identifier
    batch_id: Identifier
    device_id: Identifier
    observed_at: datetime
    source: Literal["synthetic", "sensor"]
    metrics: Annotated[tuple[Metric, ...], Field(min_length=1, max_length=32)]

    _utc = field_validator("observed_at")(utc)

    @model_validator(mode="after")
    def unique_metrics(self) -> Reading:
        keys = [m.metric for m in self.metrics]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate metric names")
        return self


class ReadingBatch(Contract):
    readings: Annotated[list[Reading], Field(min_length=1, max_length=256)]


class MetricSpec(Contract):
    unit: str
    minimum: float
    maximum: float

    @model_validator(mode="after")
    def limits(self) -> MetricSpec:
        if self.maximum <= self.minimum:
            raise ValueError("invalid metric limits")
        return self


class Product(Contract):
    q10: Annotated[float, Field(gt=1, le=10)]
    reference_c: Annotated[float, Field(gt=-273.15, le=60)]
    shelf_life_reference_h: Annotated[float, Field(gt=0, le=10000)]
    valid_min_c: float
    valid_max_c: float
    gompertz_lag_eq_h: Annotated[float, Field(ge=0)]
    gompertz_max_rate: Annotated[float, Field(gt=0)]
    log_count_initial: float
    log_count_max: float
    log_count_quality_limit: float
    parameter_status: Literal["illustrative_unvalidated"] = "illustrative_unvalidated"

    @model_validator(mode="after")
    def limits(self) -> Product:
        if not -100 <= self.valid_min_c < self.valid_max_c <= 100:
            raise ValueError("invalid model temperature domain")
        if not self.valid_min_c <= self.reference_c <= self.valid_max_c:
            raise ValueError("reference temperature outside domain")
        if not self.log_count_initial < self.log_count_quality_limit < self.log_count_max:
            raise ValueError("invalid microbial quality limits")
        return self


class Policy(Contract):
    yellow_enter: Annotated[float, Field(gt=0, lt=1)]
    yellow_exit: Annotated[float, Field(gt=0, lt=1)]
    red_enter: Annotated[float, Field(ge=0, lt=1)]
    red_confirmations: Annotated[int, Field(ge=1, le=100, strict=True)]
    warn_hours: Annotated[float, Field(gt=0, le=1000)]
    stale_seconds: Annotated[float, Field(gt=0, le=3600)]
    future_skew_seconds: Annotated[float, Field(ge=0, le=300)]
    max_history_rows: Annotated[int, Field(ge=100, le=10000000, strict=True)]
    max_pending_events: Annotated[int, Field(ge=100, le=10000000, strict=True)]

    @model_validator(mode="after")
    def thresholds(self) -> Policy:
        if not self.red_enter < self.yellow_enter < self.yellow_exit:
            raise ValueError("require red < yellow-enter < yellow-exit")
        return self


class Settings(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["demo", "shadow"] = "demo"
    policy: Policy
    metrics: dict[str, MetricSpec]
    products: dict[str, Product]

    @model_validator(mode="after")
    def required_fields(self) -> Settings:
        if "temperature_c" not in self.metrics or not self.products:
            raise ValueError("temperature metric and products required")
        return self


def load_settings(path: str | Path | None = None) -> Settings:
    source = Path(path) if path else Path(__file__).with_name("defaults.json")
    return Settings.model_validate_json(source.read_text(encoding="utf-8"))


class Batch(Contract):
    batch_id: Identifier
    market_id: Identifier
    stall_id: Identifier
    product_id: Identifier
    device_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=64)]
    created_at: datetime
    expires_at: datetime | None = None
    initial_quality: Annotated[float, Field(ge=0, le=1)] = 1.0

    _utc = field_validator("created_at")(utc)

    @field_validator("expires_at")
    @classmethod
    def expiry_utc(cls, value: datetime | None) -> datetime | None:
        return utc(value) if value else None

    @model_validator(mode="after")
    def valid_batch(self) -> Batch:
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError("duplicate devices")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expiry must follow creation")
        return self


class State(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    CLOSED = "closed"


class CloseRequest(Contract):
    reason: Annotated[str, Field(min_length=3, max_length=500)]


class DemoRequest(Contract):
    scenario: Literal["normal", "cooled", "heatwave", "outage", "sensor_fault"] = "normal"
    hours: Annotated[float, Field(ge=1, le=72)] = 24
    interval_minutes: Annotated[int, Field(ge=1, le=60, strict=True)] = 10
    seed: Annotated[int, Field(ge=0, le=2**32 - 1, strict=True)] = 20260916
