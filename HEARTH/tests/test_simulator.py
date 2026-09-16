import pytest

from hearth.models import DemoRequest
from hearth.simulator import simulate


@pytest.mark.parametrize("scenario", ["normal", "cooled", "heatwave", "outage", "sensor_fault"])
def test_all_scenarios_are_reproducible_and_auditable(config, scenario):
    request = DemoRequest(scenario=scenario, hours=24, interval_minutes=10)
    report = simulate(request, config)
    assert report == simulate(request, config)
    assert report["ledger"]["valid"]
    assert all(0 <= sample["quality_index"] <= 1 for sample in report["samples"])
    assert report["synthetic"] and not report["safety_certified"]
    assert report["samples"][0]["state"] == "green"
    assert report["samples"][-1]["state"] == "red"
    assert "yellow" in {sample["state"] for sample in report["samples"]}
    assert any(sample["quality_index"] != sample["synthetic_truth_quality"] for sample in report["samples"])
    if scenario in {"outage", "sensor_fault"}:
        assert "degraded" in {sample["health"] for sample in report["samples"]}


def test_cooling_delays_quality_alert_under_explicit_assumptions(config):
    normal = simulate(DemoRequest(scenario="normal"), config)
    cooled = simulate(DemoRequest(scenario="cooled"), config)
    assert cooled["first_red_h"] > normal["first_red_h"]
    assert cooled["cooling_lifetime_ratio_25_vs_30"] == pytest.approx(2.7**0.5)


def test_extreme_duration_and_dense_sampling(config):
    report = simulate(DemoRequest(scenario="heatwave", hours=72, interval_minutes=1), config)
    assert len(report["samples"]) == 4321
    assert report["ledger"]["valid"]
    assert report["samples"][-1]["quality_index"] == 0
