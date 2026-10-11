"""Issue #1066: get_debug_state() reports the comfort family from the thermostat's real mode."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.climate_advisor.comfort_family_fsm import ComfortFamilyState

from .test_nat_vent_dashboard_target import _make_nat_vent_coord_stub


def _debug(family, flag, last_switch=None, raises=False):
    coord = _make_nat_vent_coord_stub(config={})
    ae = coord.automation_engine
    ae._comfort_mode_family = flag
    ae._last_comfort_switch = last_switch
    if raises:
        ae._current_thermostat_family = MagicMock(side_effect=RuntimeError("boom"))
    else:
        ae._current_thermostat_family = MagicMock(return_value=family)
    return coord.get_debug_state()


class TestComfortFamilyArmedDebug:
    def test_thermostat_heat_wins_over_stale_cooling_flag(self):
        state = _debug(ComfortFamilyState.HEATING, "cooling")
        assert state["comfort_family_armed"] == "heating"

    def test_thermostat_cool_wins_over_stale_heating_flag(self):
        state = _debug(ComfortFamilyState.COOLING, "heating")
        assert state["comfort_family_armed"] == "cooling"

    def test_unknown_thermostat_mode_is_none(self):
        assert _debug(None, "cooling")["comfort_family_armed"] is None

    def test_error_in_family_lookup_does_not_break_debug_view(self):
        assert _debug(None, "cooling", raises=True)["comfort_family_armed"] is None

    def test_last_switch_passthrough_and_none(self):
        sw = {"ts": "2026-10-11T10:00:00", "from": "cooling", "to": "heating", "reason": "breach"}
        state = _debug(ComfortFamilyState.HEATING, "heating", last_switch=sw)
        assert state["comfort_family_last_switch"] == sw
        assert _debug(None, None)["comfort_family_last_switch"] is None
