"""Issue #1068 / #1054: comfort re-arm after a nat-vent exit with the window still open.

Occupant framing (#1068): the house cooled to the comfort floor with a window open and nat-vent
ended with the thermostat Off. Closing the window left heat off until a breach tick or the 30-min
cycle. Now closing the window re-arms comfort immediately. A thermostat the occupant left Off is
never force-armed. Real ``AutomationEngine`` handlers; nothing re-implements production logic.

#1054: engine-emitted incidents carry the band in force (sleep vs day), not the flat day numbers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.door_window_fsm import DoorWindowFsmEventKind
from tests.test_restore_resolved_band import (
    _AUTOMATION,
    _DAY,
    _NIGHT,
    _SLEEP_CFG,
    _classification,
    _make_engine,
    _setpoints,
)


def _paused_off_engine(*, indoor: float, entity: str, **cfg) -> AutomationEngine:
    engine = _make_engine(indoor=indoor, mode="off", **cfg)
    engine._current_classification = _classification("hot")
    engine._pre_pause_mode = None
    engine._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.NAT_VENT_EXITED_SENSOR_STILL_OPEN)
    engine._paused_entity = entity
    assert engine._paused_by_door
    return engine


def _close_window(engine: AutomationEngine, now: datetime = _DAY) -> None:
    with (
        patch(f"{_AUTOMATION}.dt_util.now", return_value=now),
        patch.object(engine, "_any_monitored_sensor_open", return_value=False),
    ):
        asyncio.run(engine.handle_all_doors_windows_closed())


class TestNatVentExitRearmOnClose:
    def test_cold_floor_exit_rearms_heat_on_close(self, caplog):
        engine = _paused_off_engine(indoor=67.0, entity="nat-vent-exit")
        with caplog.at_level("INFO", logger=_AUTOMATION):
            _close_window(engine)
        assert _setpoints(engine)[-1] == ("heat", 68.0)
        assert engine._grace_active
        assert any(
            "comfort restore dispatched" in r.getMessage() and "rearmed_from_off=True" in r.getMessage()
            for r in caplog.records
        )
        assert engine._pre_pause_mode is None
        assert not engine._paused_by_door

    def test_hot_ceiling_exit_rearms_cool_on_close(self):
        engine = _paused_off_engine(indoor=75.0, entity="nat-vent-exit")
        _close_window(engine)
        assert _setpoints(engine)[-1] == ("cool", 74.0)
        assert engine._grace_active

    def test_user_off_thermostat_is_not_force_armed(self):
        engine = _paused_off_engine(indoor=67.0, entity="binary_sensor.window")
        _close_window(engine)
        assert _setpoints(engine) == []
        assert not engine._grace_active
        assert not engine._paused_by_door

    def test_pre_pause_mode_path_still_restores_mode(self):
        engine = _make_engine(indoor=69.0, mode="off")
        engine._current_classification = _classification("cold")
        engine._pre_pause_mode = "heat"
        engine._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.SENSOR_OPENED)
        engine._paused_entity = "binary_sensor.window"
        _close_window(engine)
        assert engine._test_state.state == "heat"
        assert engine._grace_active


class TestEngineIncidentCarriesActiveBand:
    def _emit(self, engine: AutomationEngine, now: datetime) -> dict:
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda t, p: events.append((t, p))
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=now):
            engine._emit_engine_incident("hvac_command_failed", {"hvac_mode": "heat"})
        assert events[-1][0] == "incident_detected"
        return events[-1][1]

    def test_sleep_window_incident_carries_sleep_band(self):
        engine = _make_engine(indoor=65.0, **_SLEEP_CFG)
        engine._current_classification = _classification("cold")
        payload = self._emit(engine, _NIGHT)
        assert (payload["comfort_heat"], payload["comfort_cool"]) == (64.0, 70.0)

    def test_daytime_incident_carries_day_band(self):
        engine = _make_engine(indoor=65.0, **_SLEEP_CFG)
        engine._current_classification = _classification("cold")
        payload = self._emit(engine, _DAY)
        assert (payload["comfort_heat"], payload["comfort_cool"]) == (68.0, 74.0)

    def test_no_classification_falls_back_to_config(self):
        engine = _make_engine(indoor=65.0, **_SLEEP_CFG)
        engine._current_classification = None
        payload = self._emit(engine, _NIGHT)
        assert (payload["comfort_heat"], payload["comfort_cool"]) == (68.0, 74.0)
