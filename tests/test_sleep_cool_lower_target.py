"""Regression coverage for independent cooler sleep targets."""

import pytest

from custom_components.climate_advisor.config_flow import _display_default, setpoint_slider_ranges
from custom_components.climate_advisor.const import DEFAULT_SLEEP_COOL


@pytest.mark.parametrize("target", [61, 64, 65, 69, 72])
def test_fahrenheit_sleep_cooling_accepts_bedroom_targets(target):
    lo, hi, default, step = setpoint_slider_ranges(False)["sleep_cool"]
    assert lo <= target <= hi
    assert _display_default(target, False, lo, hi) == target
    assert default == DEFAULT_SLEEP_COOL
    assert step == 1


@pytest.mark.parametrize("target", [61, 64, 65, 69, 72])
def test_celsius_roundtrip_preserves_stored_fahrenheit(target):
    lo, hi, default, step = setpoint_slider_ranges(True)["sleep_cool"]
    shown = _display_default(target, True, lo, hi)
    assert lo <= shown <= hi
    assert abs((shown * 9 / 5 + 32) - target) < 0.001
    assert step == 0.5


@pytest.mark.parametrize("celsius", [False, True])
def test_only_sleep_cooling_lower_bound_is_expanded(celsius):
    ranges = setpoint_slider_ranges(celsius)
    # Matching sleep-heat minimum keeps the two independent sleep controls consistent.
    assert ranges["sleep_cool"][0] == ranges["sleep_heat"][0]
    assert ranges["sleep_cool"][1] == (32 if celsius else 89)
    assert ranges["comfort_heat"][0] == (12.5 if celsius else 55)
    assert ranges["comfort_cool"][0] == (20 if celsius else 68)


@pytest.mark.parametrize("sleep_cool", [64, 65])
@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_sleep_band_and_morning_restore_remain_independent(sleep_cool, mode):
    from types import SimpleNamespace

    from custom_components.climate_advisor.automation import select_comfort_band

    config = {
        "comfort_heat": 69,
        "comfort_cool": 71,
        "sleep_heat": 61,
        "sleep_cool": sleep_cool,
        "setback_heat": 61,
        "setback_cool": 75,
    }
    classification = SimpleNamespace(hvac_mode=mode, day_type="cold" if mode == "heat" else "hot")
    night = select_comfort_band(
        classification, config, occupancy_mode="home", in_sleep_window=True, aggressive_savings=False
    )
    morning = select_comfort_band(
        classification, config, occupancy_mode="home", in_sleep_window=False, aggressive_savings=False
    )
    assert (night.floor, night.ceiling) == (61, sleep_cool)
    assert (morning.floor, morning.ceiling) == (69, 71)
    assert night.active == ("floor" if mode == "heat" else "ceiling")
