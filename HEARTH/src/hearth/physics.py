"""Equivalent-reference-time model. Quality lifetime is not a safety guarantee."""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from .models import Product

R_GAS = 8.31446261815324  # J mol^-1 K^-1, physical constant


def finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def rate(temperature_c: float, product: Product) -> float:
    finite(temperature_c, "temperature")
    if not product.valid_min_c <= temperature_c <= product.valid_max_c:
        raise ValueError("temperature outside validated/illustrative model domain")
    return product.q10 ** ((temperature_c - product.reference_c) / 10)


def local_activation_energy(product: Product) -> float:
    """Ea matching Q10 exactly between Tref and Tref+10 K; not globally equivalent."""
    kelvin = product.reference_c + 273.15
    return R_GAS * math.log(product.q10) / (1 / kelvin - 1 / (kelvin + 10))


def arrhenius_rate(temperature_c: float, product: Product) -> float:
    rate(temperature_c, product)  # same domain check
    kelvin = temperature_c + 273.15
    ref_kelvin = product.reference_c + 273.15
    return math.exp(local_activation_energy(product) / R_GAS * (1 / ref_kelvin - 1 / kelvin))


def quality(exposure_eq_h: float, product: Product) -> float:
    if finite(exposure_eq_h, "exposure") < 0:
        raise ValueError("negative exposure")
    return max(0.0, min(1.0, 1 - exposure_eq_h / product.shelf_life_reference_h))


def remaining_hours(exposure_eq_h: float, future_c: float, product: Product) -> float:
    """Wall-clock hours conditional on a CONSTANT future product temperature."""
    quality(exposure_eq_h, product)
    return max(0.0, product.shelf_life_reference_h - exposure_eq_h) / rate(future_c, product)


def integrate_samples(times_h: Sequence[float], temperatures_c: Sequence[float], product: Product) -> float:
    """Offline trapezoidal integration of RATE for irregularly sampled histories."""
    if len(times_h) != len(temperatures_c) or not times_h:
        raise ValueError("nonempty, equally sized series required")
    rates = [rate(t, product) for t in temperatures_c]
    for t in times_h:
        finite(t, "time")
    total = 0.0
    for i in range(1, len(times_h)):
        delta = times_h[i] - times_h[i - 1]
        if delta <= 0:
            raise ValueError("timestamps must be strictly increasing")
        total += delta * (rates[i - 1] + rates[i]) / 2
    return finite(total, "integrated exposure")


def integrate_function(temperature: Callable[[float], float], start_h: float, end_h: float,
                       product: Product, steps: int = 256) -> float:
    if steps < 1 or steps > 100000 or end_h <= start_h:
        raise ValueError("invalid integration interval or step budget")
    dt = (end_h - start_h) / steps
    times = [start_h + i * dt for i in range(steps + 1)]
    return integrate_samples(times, [temperature(t) for t in times], product)


def gompertz_log_count(exposure_eq_h: float, product: Product) -> float:
    """Illustrative modified Gompertz growth in log10 CFU/g; separate simulator truth."""
    if finite(exposure_eq_h, "exposure") < 0:
        raise ValueError("negative exposure")
    amplitude = product.log_count_max - product.log_count_initial
    inner = product.gompertz_max_rate * math.e / amplitude * (
        product.gompertz_lag_eq_h - exposure_eq_h
    ) + 1
    # Numerically safe saturation; exp(-exp(x)) is effectively zero for x > 40.
    fraction = 0.0 if inner > 40 else math.exp(-math.exp(inner))
    return product.log_count_initial + amplitude * fraction
