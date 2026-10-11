"""Issue #1075 (spec + test gap, no behavior change): thermostat unavailable -> available re-apply path.

Occupant outcome: the thermostat reconnects reporting Off while the house is cold. How long until
Climate Advisor re-arms heating? Documented answer (docs/comfort-edge-selection-spec.md, G9):

* the reconnect itself carries a ``current_temperature`` the old (unavailable) state lacked, so the
  coordinator runs the reactive temperature checks (unless startup coalescing suppresses the listener);
* with the thermostat family unknown (Off), the reactive check re-arms immediately ONLY on a breach
  (indoor outside the band); a reading in a margin zone waits for the next 30-minute refresh, whose
  ``apply_classification`` -> ``_apply_comfort_band`` path writes the mode.

Real code throughout: the real ``_async_thermostat_changed`` (stub factory from
``test_heat_cool_override``) and the real ``AutomationEngine`` selector/reactive check
(``test_comfort_settling_guard`` helpers). Nothing re-implements production logic.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_comfort_settling_guard import (
    _AUTOMATION,
    _apply,
    _mode,
    _mode_writes,
    _reactive,
    _scene,
)
from tests.test_heat_cool_override import (
    _NOW_BASE,
    _PATCH_CALLBACK,
    _PATCH_DT_UTIL,
    _make_coordinator_stub,
    _make_event,
)


def _reconnect_event(current_temperature: float | None = 66.0):
    """unavailable -> off, the new state carrying a current_temperature the old one never had."""
    event = _make_event(old_hvac_mode="unavailable", new_hvac_mode="off")
    if current_temperature is not None:
        event.data["new_state"].attributes["current_temperature"] = current_temperature
    return event


def _run_listener(coord, event) -> None:
    mock_dt = MagicMock()
    mock_dt.now.return_value = _NOW_BASE
    with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
        asyncio.run(coord._async_thermostat_changed(event))


class TestCoordinatorReconnectRunsReactiveChecks:
    def test_reconnect_with_temperature_runs_reactive_checks(self):
        coord = _make_coordinator_stub()
        coord._async_run_temp_reactive_checks = AsyncMock()
        _run_listener(coord, _reconnect_event(66.0))
        assert coord._async_run_temp_reactive_checks.await_count == 1
        # ...and the reconnect is still not read as an occupant override (#1049)
        assert coord.automation_engine.handle_manual_override.call_count == 0

    def test_reconnect_without_temperature_does_not_run_reactive_checks(self):
        """A reconnect that reports no current_temperature waits for the first reading or the 30-min refresh."""
        coord = _make_coordinator_stub()
        coord._async_run_temp_reactive_checks = AsyncMock()
        _run_listener(coord, _reconnect_event(None))
        assert coord._async_run_temp_reactive_checks.await_count == 0

    def test_reconnect_during_startup_coalescing_is_suppressed(self):
        """Startup coalescing (STARTUP_COALESCE_SECONDS) returns early before the reactive checks."""
        coord = _make_coordinator_stub()
        coord._startup_coalesce_active = True
        coord._async_run_temp_reactive_checks = AsyncMock()
        _run_listener(coord, _reconnect_event(66.0))
        assert coord._async_run_temp_reactive_checks.await_count == 0


def _reconnected_as_off(*, day: str, mode: str, armed_indoor: float):
    """An engine whose band is armed, then the thermostat reconnects as Off (old commands are stale)."""
    engine, clock = _scene(day=day, mode=mode, indoor=armed_indoor, ran=None)
    # Past COMFORT_RECENT_COMMAND_WINDOW_S and COMFORT_REACTIVE_RETRY_S: the live (Off) state is the memory.
    clock.advance(minutes=10)
    engine._test_state.state = "off"
    engine.hass.services.async_call.reset_mock()
    return engine, clock


class TestEngineReapplyAfterReconnectAsOff:
    def test_family_is_unknown_after_reconnect_as_off(self):
        engine, clock = _reconnected_as_off(day="cold", mode="heat", armed_indoor=71.0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            assert engine._current_thermostat_family(clock.now) is None

    def test_breach_reactive_check_rearms_heat(self):
        """Indoor below the floor: the first temperature tick after the reconnect re-arms heat immediately."""
        engine, clock = _reconnected_as_off(day="cold", mode="heat", armed_indoor=71.0)
        _reactive(engine, clock, 66.0)  # floor 68: breach
        assert _mode(engine) == "heat"
        assert "heat" in _mode_writes(engine)

    def test_margin_zone_reactive_check_does_not_write_but_periodic_apply_does(self):
        """Indoor inside the floor's arming margin (68-70): the reactive check stays quiet (documented G9
        behaviour); the 30-minute ``apply_classification`` pass is what restores the mode."""
        engine, clock = _reconnected_as_off(day="cold", mode="heat", armed_indoor=71.0)
        _reactive(engine, clock, 69.5)
        assert _mode(engine) == "off"
        assert _mode_writes(engine) == []

        clock.advance(minutes=20)  # still within the 30-minute refresh interval
        _reactive(engine, clock, 69.5)
        assert _mode(engine) == "off"

        clock.advance(minutes=5)
        _apply(engine, clock, "cold", 69.5)  # the coordinator's periodic refresh
        assert _mode(engine) == "heat"
        assert "heat" in _mode_writes(engine)

    def test_in_band_reading_is_left_off_by_the_reactive_check(self):
        engine, clock = _reconnected_as_off(day="cold", mode="heat", armed_indoor=71.0)
        _reactive(engine, clock, 72.0)  # between the margins
        assert _mode(engine) == "off"
        assert _mode_writes(engine) == []

    def test_cool_side_breach_rearms_cool(self):
        """Mirror: a hot day, thermostat reconnects as Off, indoor above the ceiling."""
        engine, clock = _reconnected_as_off(day="hot", mode="cool", armed_indoor=73.0)
        _reactive(engine, clock, 76.0)  # ceiling 74: breach
        assert _mode(engine) == "cool"
        assert "cool" in _mode_writes(engine)

    def test_cool_side_margin_zone_waits_for_periodic_apply(self):
        engine, clock = _reconnected_as_off(day="hot", mode="cool", armed_indoor=71.0)
        _reactive(engine, clock, 73.0)  # inside the ceiling's margin (72-74), not a breach
        assert _mode(engine) == "off"
        clock.advance(minutes=25)
        _apply(engine, clock, "hot", 73.0)
        assert _mode(engine) == "cool"
