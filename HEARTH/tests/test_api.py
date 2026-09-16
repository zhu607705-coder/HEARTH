import json
import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from conftest import START, batch, reading
from hearth.api import MAX_BODY_BYTES, create_app
from hearth.models import Batch, Metric, Reading, load_settings


@pytest.fixture
def api(tmp_path, config):
    keys = {role: secrets.token_urlsafe(32) for role in ("viewer", "ingest", "operator")}
    app = create_app(tmp_path / "api.db", config, keys, clock=lambda: START, watchdog=False)
    with TestClient(app) as client:
        yield app, client, keys


def test_roles_and_protected_schema(api):
    app, client, keys = api
    payload = batch().model_dump(mode="json")
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/openapi.json").status_code == 401
    for role in ("viewer", "ingest"):
        assert client.post("/v1/batches", json=payload, headers={"X-API-Key": keys[role]}).status_code == 401
    assert client.post("/v1/batches", json=payload, headers={"X-API-Key": keys["operator"]}).status_code == 201
    assert client.get("/v1/openapi.json", headers={"X-API-Key": keys["viewer"]}).status_code == 200
    payload = {"readings": [reading().model_dump(mode="json")]}
    assert client.post("/v1/readings:batch", json=payload, headers={"X-API-Key": keys["viewer"]}).status_code == 401
    assert client.post("/v1/readings:batch", json=payload, headers={"X-API-Key": keys["ingest"]}).status_code == 200
    response = client.get("/v1/stalls/stall-1/state", headers={"X-API-Key": keys["viewer"]})
    assert response.json()["data"][0]["state"] == "green"
    assert not response.json()["data"][0]["sale_allowed"]
    wrong = client.get("/v1/stalls/other/batches/batch-1/shelf-life", headers={"X-API-Key": keys["viewer"]})
    assert wrong.status_code == 404


def test_oversized_and_invalid_json(api):
    _, client, keys = api
    headers = {"X-API-Key": keys["operator"], "Content-Type": "application/json"}
    assert client.post("/v1/readings:batch", content=b"x" * (MAX_BODY_BYTES + 1), headers=headers).status_code == 413
    assert client.post("/v1/readings:batch", content=b"{broken", headers=headers).status_code == 422
    payload = {"readings": [reading().model_dump(mode="json")]}
    payload["readings"][0]["metrics"][0]["value"] = float("nan")
    response = client.post("/v1/readings:batch", content=json.dumps(payload), headers=headers)
    assert response.status_code == 422
    assert "NaN" not in response.text
    payload["readings"][0]["metrics"][0]["value"] = float("inf")
    assert client.post("/v1/readings:batch", content=json.dumps(payload), headers=headers).status_code == 422


def test_chunked_oversized_body_is_bounded(api):
    _, client, keys = api
    def chunks():
        for _ in range(17):
            yield b"x" * 32768
    response = client.post("/v1/readings:batch", content=chunks(),
                           headers={"X-API-Key": keys["ingest"], "Content-Type": "application/json"})
    assert response.status_code == 413


def test_security_headers_and_asset_allowlist(api):
    _, client, _ = api
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "No food-safety certification" in response.text
    assert client.get("/assets/defaults.json").status_code == 404
    assert client.get("/assets/dashboard.js").status_code == 200
    assert "unsafe-inline" not in response.headers["content-security-policy"]


def test_shadow_cannot_run_demo(tmp_path, config):
    shadow = config.model_copy(update={"mode": "shadow"})
    keys = {role: secrets.token_urlsafe(32) for role in ("viewer", "ingest", "operator")}
    app = create_app(tmp_path / "shadow.db", shadow, keys, clock=lambda: START, watchdog=False)
    with TestClient(app) as client:
        response = client.post("/v1/demo/run", json={"hours": 1}, headers={"X-API-Key": keys["operator"]})
        assert response.status_code == 403


def test_demo_uses_isolated_store(api):
    app, client, keys = api
    response = client.post("/v1/demo/run", json={"hours": 2, "scenario": "outage"},
                           headers={"X-API-Key": keys["operator"]})
    assert response.status_code == 200
    assert response.json()["synthetic"] and response.json()["ledger"]["valid"]
    assert app.state.store.verify()["checked"] == 0


def test_contract_rejects_invalid_and_ambiguous_values():
    with pytest.raises(ValidationError):
        reading(at=START.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        reading(sequence=True)
    with pytest.raises(ValidationError):
        Metric(metric="temperature_c", value=None, unit="degC")
    with pytest.raises(ValidationError):
        Metric(metric="temperature_c", value=10, unit="degC", quality="missing")
    item = reading()
    with pytest.raises(ValidationError):
        Reading.model_validate(item.model_dump() | {"metrics": [item.metrics[0], item.metrics[0]]})
    with pytest.raises(ValidationError):
        Batch.model_validate(batch().model_dump() | {"device_ids": ["hook-1", "hook-1"]})
    with pytest.raises(ValidationError):
        batch(expires_at=START - timedelta(seconds=1))


def test_contract_export_matches_runtime(api):
    from pathlib import Path
    root = Path(__file__).parents[1]
    schema = json.loads((root / "contracts/readings.schema.json").read_text())
    assert schema == Reading.model_json_schema()
    Draft202012Validator(schema).validate(reading().model_dump(mode="json"))
    app, _, _ = api
    exported = json.loads((root / "contracts/openapi.json").read_text())
    assert exported == app.openapi()
    assert exported["openapi"].startswith("3.1")


def test_invalid_policy_and_weak_keys(tmp_path):
    config = load_settings()
    with pytest.raises(ValueError):
        create_app(tmp_path / "keys.db", config, {"viewer": "weak"})
    with pytest.raises(ValueError):
        create_app(tmp_path / "keys.db", config, {role: "x" * 32 for role in ("viewer", "ingest", "operator")})
    data = config.model_dump()
    data["policy"]["red_enter"] = 0.9
    with pytest.raises(ValidationError):
        type(config).model_validate(data)


@pytest.mark.parametrize("value", [True, False, "30", "nan"])
def test_metric_does_not_coerce_booleans_or_strings(value):
    with pytest.raises(ValidationError):
        Metric(metric="temperature_c", value=value, unit="degC")
