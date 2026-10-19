"""Exercise actual setup/options save paths, including cross-unit hardware limits."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_config_flow import _make_config_flow, _make_options_flow

VALID = dict(comfort_heat=72, comfort_cool=72, setback_heat=61, setback_cool=75, sleep_heat=61, sleep_cool=64)


def flow_for(kind, *, display="fahrenheit", system="°F", bounds=None):
    data = {"climate_entity": "climate.test", "temp_unit": display}
    if kind == "options":
        flow, saved = _make_options_flow(data)
    else:
        flow, saved = _make_config_flow(), {}
        flow._data = data.copy()
        flow.async_step_temperature_sources = AsyncMock(return_value={"step_id": "temperature_sources"})
    flow.hass.config.units.temperature_unit = system
    flow.hass.states.get.return_value = SimpleNamespace(
        attributes=bounds if bounds is not None else {"min_temp": 61, "max_temp": 87}
    )
    return flow, saved


def submit(flow, values):
    return asyncio.run(flow.async_step_setpoints(dict(values)))


@pytest.mark.parametrize("kind", ["setup", "options"])
@pytest.mark.parametrize("band", ["comfort", "setback", "sleep"])
def test_inverted_band_cannot_be_saved(kind, band):
    flow, saved = flow_for(kind)
    values = {**VALID, f"{band}_heat": 74, f"{band}_cool": 73}
    result = submit(flow, values)
    assert result["errors"][f"{band}_cool"] == "cool_below_heat"
    assert "data" not in saved
    if kind == "setup":
        flow.async_step_temperature_sources.assert_not_called()


@pytest.mark.parametrize("kind", ["setup", "options"])
@pytest.mark.parametrize("key", list(VALID))
@pytest.mark.parametrize("value,error", [(60, "below_thermostat_min"), (88, "above_thermostat_max")])
def test_every_target_respects_hardware_limits(kind, key, value, error):
    flow, saved = flow_for(kind)
    result = submit(flow, {**VALID, key: value})
    assert result["errors"][key] == error
    assert "data" not in saved


@pytest.mark.parametrize("kind", ["setup", "options"])
@pytest.mark.parametrize(
    "display,system,bounds",
    [
        ("fahrenheit", "°F", {"min_temp": 61, "max_temp": 87}),
        ("celsius", "°F", {"min_temp": 61, "max_temp": 87}),
        ("fahrenheit", "°C", {"min_temp": (61 - 32) * 5 / 9, "max_temp": (87 - 32) * 5 / 9}),
        ("celsius", "°C", {"min_temp": (61 - 32) * 5 / 9, "max_temp": (87 - 32) * 5 / 9}),
    ],
)
def test_equal_comfort_and_requested_sleep_survive_unit_conversion(kind, display, system, bounds):
    flow, saved = flow_for(kind, display=display, system=system, bounds=bounds)
    values = {k: (v - 32) * 5 / 9 if display == "celsius" else v for k, v in VALID.items()}
    result = submit(flow, values)
    assert not result.get("errors")
    data = saved["data"] if kind == "options" else flow._data
    assert data["sleep_heat"] == pytest.approx(61)
    assert data["sleep_cool"] == pytest.approx(64)
    assert data["comfort_heat"] == data["comfort_cool"] == pytest.approx(72)


@pytest.mark.parametrize(
    "bounds", [{}, {"min_temp": None, "max_temp": "unknown"}, {"min_temp": float("nan"), "max_temp": float("inf")}]
)
def test_missing_or_invalid_bounds_do_not_invent_limits(bounds):
    flow, saved = flow_for("options", bounds=bounds)
    assert not submit(flow, {**VALID, "sleep_heat": 55, "sleep_cool": 60}).get("errors")
    assert saved["data"]["sleep_cool"] == 60


def test_one_missing_bound_does_not_skip_other_bound():
    flow, saved = flow_for("options", bounds={"max_temp": 87})
    assert submit(flow, {**VALID, "sleep_cool": 88})["errors"]["sleep_cool"] == "above_thermostat_max"
    assert "data" not in saved


@pytest.mark.parametrize("value", [float("nan"), float("inf"), None, "invalid"])
def test_non_finite_temperature_is_field_error(value):
    flow, saved = flow_for("options")
    assert submit(flow, {**VALID, "sleep_cool": value})["errors"]["sleep_cool"] == "invalid_temperature"
    assert "data" not in saved
