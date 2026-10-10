"""Issue #1050 (#1048 changes 18 + 21, coordinator side).

* Chart band keeps the HELD (setback) band while an arrival-home comfort restore is deferred.
* ``_get_fan_physical_state`` returns None (unknown), never False, for an unavailable command entity.
* Renderers for ``occupancy_restore_deferred`` and the fan read-back ``fan_cancel``.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()


def _coord_mod():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


CFG = {
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "sleep_heat": 62,
    "sleep_cool": 78,
    "wake_time": "06:30",
    "sleep_time": "22:30",
}
NOW = datetime(2026, 10, 10, 14, 0)


def _band(hold: bool, mode: str = "home"):
    stamps = [datetime(2026, 10, 10, h) for h in (12, 13, 14, 15, 16)] + [datetime(2026, 10, 11, 14)]
    out = _coord_mod()._compute_target_band_schedule(stamps, CFG, mode, NOW, restore_hold=hold)
    return {r["ts"][:13]: (r["lower"], r["upper"]) for r in out}


class TestBandRestoreHold:
    def test_without_hold_home_shows_comfort(self):
        assert _band(False)["2026-10-10T14"] == (70.0, 75.0)

    def test_hold_shows_setback_up_to_now_then_normal_schedule(self):
        b = _band(True)
        assert b["2026-10-10T12"] == (60.0, 80.0)
        assert b["2026-10-10T14"] == (60.0, 80.0)
        assert b["2026-10-10T15"] == (70.0, 75.0)  # the hold is expected to clear
        assert b["2026-10-11T14"] == (70.0, 75.0)  # other days untouched

    def test_hold_does_not_change_away_or_vacation(self):
        assert _band(True, "away") == _band(False, "away")
        assert _band(True, "vacation") == _band(False, "vacation")


class TestOccupancyRestoreHeldAccessor:
    def _coord(self, engine):
        c = object.__new__(_coord_mod().ClimateAdvisorCoordinator)
        c.automation_engine = engine
        return c

    def test_reads_engine_accessor(self):
        held = SimpleNamespace(occupancy_restore_hold_reason=lambda: "manual_override")
        free = SimpleNamespace(occupancy_restore_hold_reason=lambda: None)
        assert self._coord(held)._occupancy_restore_held() is True
        assert self._coord(free)._occupancy_restore_held() is False
        assert self._coord(None)._occupancy_restore_held() is False

    def test_mock_engine_is_not_treated_as_held(self):
        assert self._coord(MagicMock())._occupancy_restore_held() is False


def _fan_coord(states: dict, *, state_entity=None, feedback=True):
    cls = _coord_mod().ClimateAdvisorCoordinator
    c = object.__new__(cls)
    c.config = {"fan_entity": "switch.whf"}
    if state_entity:
        c.config["fan_state_entity"] = state_entity
    c.hass = MagicMock()
    c.hass.states.get = lambda eid: states.get(eid)
    c._fan_state_feedback_enabled = lambda: feedback
    c._fan_state_entity_unavailable_warned = False
    c._get_fan_physical_state = types.MethodType(cls._get_fan_physical_state, c)
    return c


def _st(value, **attrs):
    return SimpleNamespace(state=value, attributes=attrs)


class TestFanPhysicalStateUnknown:
    def test_unavailable_command_entity_is_unknown_not_off(self):
        assert _fan_coord({"switch.whf": _st("unavailable")})._get_fan_physical_state() is None

    def test_missing_command_entity_is_unknown_not_off(self):
        assert _fan_coord({})._get_fan_physical_state() is None

    def test_assumed_state_command_entity_is_unknown(self):
        assert _fan_coord({"switch.whf": _st("on", assumed_state=True)})._get_fan_physical_state() is None

    def test_no_fan_entity_configured_is_unknown(self):
        c = _fan_coord({})
        c.config = {}
        assert c._get_fan_physical_state() is None

    def test_real_states_still_read(self):
        assert _fan_coord({"switch.whf": _st("on")})._get_fan_physical_state() is True
        assert _fan_coord({"switch.whf": _st("off")})._get_fan_physical_state() is False

    def test_state_entity_wins_and_unavailable_falls_through(self):
        states = {"switch.state": _st("on"), "switch.whf": _st("off")}
        assert _fan_coord(states, state_entity="switch.state")._get_fan_physical_state() is True
        states["switch.state"] = _st("unavailable")
        assert _fan_coord(states, state_entity="switch.state")._get_fan_physical_state() is False
        states["switch.whf"] = _st("unavailable")
        assert _fan_coord(states, state_entity="switch.state")._get_fan_physical_state() is None

    def test_feedback_disabled_is_unknown(self):
        assert _fan_coord({"switch.whf": _st("on")}, feedback=False)._get_fan_physical_state() is None


class TestRenderers:
    def test_occupancy_restore_deferred(self):
        from custom_components.climate_advisor import ai_skills_context as ctx

        r = ctx.EVENT_RENDERERS["occupancy_restore_deferred"]
        label, settings = r({"reason": "manual_override", "mode": "home"}, "fahrenheit")
        assert "deferred" in label and "manual thermostat change" in label and "home" in settings
        assert "door/window open" in r({"reason": "open_sensor"}, "celsius")[0]
        assert "occupancy_restore_deferred" in ctx._NO_DEDUP

    def test_fan_cancel_readback(self):
        from custom_components.climate_advisor import ai_skills_context as ctx

        r = ctx.EVENT_RENDERERS["fan_cancel"]
        base = {"trigger": "physical_drift_correction", "readback": True}
        assert "retrying once" in r({**base, "retry": True}, "fahrenheit")[0]
        assert "giving up" in r(base, "fahrenheit")[0]
        assert "stale flag cleared" in r({"trigger": "physical_drift_correction"}, "fahrenheit")[0]
