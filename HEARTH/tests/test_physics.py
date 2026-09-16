import math
import random

import pytest

from hearth.physics import (arrhenius_rate, gompertz_log_count, integrate_function,
                            integrate_samples, quality, rate, remaining_hours)


def test_units_and_cooling_ratio(config):
    product = config.products["demo_meat"]
    assert rate(4, product) == 1
    assert remaining_hours(0, 4, product) == 120
    assert remaining_hours(0, 30, product) == pytest.approx(120 / 2.7**2.6)
    assert remaining_hours(0, 25, product) / remaining_hours(0, 30, product) == pytest.approx(math.sqrt(2.7))
    assert remaining_hours(121, 30, product) == 0
    assert quality(9999, product) == 0


def test_arrhenius_local_match_not_global_equivalence(config):
    product = config.products["demo_meat"]
    assert arrhenius_rate(4, product) == 1
    assert arrhenius_rate(14, product) == pytest.approx(product.q10)
    assert arrhenius_rate(30, product) != pytest.approx(rate(30, product))


def test_irregular_integration(config):
    product = config.products["demo_meat"]
    assert integrate_samples([0, 0.1, 1, 3], [4, 4, 4, 4], product) == 3
    assert integrate_function(lambda _: 4, 0, 3, product) == pytest.approx(3)
    # Integrate rates, not rate(mean temperature).
    assert integrate_samples([0, 1], [4, 14], product) == pytest.approx((1 + 2.7) / 2)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -100, 100])
def test_invalid_temperature(config, value):
    with pytest.raises(ValueError):
        rate(value, config.products["demo_meat"])


@pytest.mark.parametrize("times,temps", [([], []), ([0, 0], [4, 4]), ([1, 0], [4, 4]),
                                        ([0, 1], [4]), ([0, float("inf")], [4, 4])])
def test_invalid_histories(config, times, temps):
    with pytest.raises(ValueError):
        integrate_samples(times, temps, config.products["demo_meat"])


def test_randomized_physical_invariants(config):
    rng = random.Random(731)
    product = config.products["demo_meat"]
    for _ in range(3000):
        low, high = sorted([rng.uniform(0, 45), rng.uniform(0, 45)])
        before = rng.uniform(0, 150)
        after = before + rng.uniform(0, 30)
        assert rate(low, product) <= rate(high, product)
        assert remaining_hours(before, low, product) >= remaining_hours(before, high, product)
        assert 0 <= quality(after, product) <= quality(before, product) <= 1
        assert gompertz_log_count(before, product) <= gompertz_log_count(after, product)
    assert math.isfinite(gompertz_log_count(1e300, product))


def test_invalid_exposure(config):
    for exposure in [-1, float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            remaining_hours(exposure, 30, config.products["demo_meat"])
