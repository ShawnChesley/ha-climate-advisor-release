"""Issue #1049 — a thermostat waking from unavailable/unknown is never a manual override.

Occupant outcome: the Sensi thermostat comes back from ``unavailable`` reporting Off.
Climate Advisor used to read that as the occupant switching it off and backed off for an
hour. Now that CA switches the thermostat on itself, that must not feed an override loop.

Uses the real ``_async_thermostat_changed`` via the stub factory from
``test_heat_cool_override.py`` (no mirrored logic).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.const import (
    UNAVAILABLE_STATES,
    is_availability_recovery,
    is_unavailable_state,
)
from tests.test_heat_cool_override import (
    _NOW_BASE,
    _PATCH_CALLBACK,
    _PATCH_DT_UTIL,
    _make_coordinator_stub,
    _make_event,
)


def _run(coord, event) -> None:
    mock_dt = MagicMock()
    mock_dt.now.return_value = _NOW_BASE
    with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
        asyncio.run(coord._async_thermostat_changed(event))


class TestHelpers:
    def test_unavailable_states(self):
        assert frozenset({"unavailable", "unknown"}) == UNAVAILABLE_STATES

    def test_is_unavailable_state(self):
        assert is_unavailable_state("unavailable")
        assert is_unavailable_state("unknown")
        assert not is_unavailable_state("off")
        assert not is_unavailable_state(None)

    def test_is_availability_recovery(self):
        assert is_availability_recovery(MagicMock(state="unavailable"))
        assert is_availability_recovery(MagicMock(state="unknown"))
        assert not is_availability_recovery(MagicMock(state="heat"))
        assert not is_availability_recovery(None)


class TestModeChangeOverride:
    """Branch 3 (plain mode-change override): classification wants cool."""

    @pytest.mark.parametrize("old", ["unavailable", "unknown"])
    @pytest.mark.parametrize("new", ["off", "heat", "heat_cool"])
    def test_recovery_is_not_an_override(self, old, new):
        coord = _make_coordinator_stub()
        _run(coord, _make_event(old_hvac_mode=old, new_hvac_mode=new))
        assert coord.automation_engine.handle_manual_override.call_count == 0

    @pytest.mark.parametrize(("old", "new"), [("heat", "off"), ("off", "heat"), ("cool", "off")])
    def test_real_change_still_overrides(self, old, new):
        coord = _make_coordinator_stub()
        _run(coord, _make_event(old_hvac_mode=old, new_hvac_mode=new))
        assert coord.automation_engine.handle_manual_override.call_count == 1

    def test_unavailable_to_unavailable_is_not_an_override(self):
        coord = _make_coordinator_stub()
        _run(coord, _make_event(old_hvac_mode="unavailable", new_hvac_mode="unknown"))
        assert coord.automation_engine.handle_manual_override.call_count == 0

    def test_old_state_none_is_not_an_override(self):
        coord = _make_coordinator_stub()
        event = _make_event(old_hvac_mode="heat", new_hvac_mode="off")
        event.data["old_state"] = None
        _run(coord, event)
        assert coord.automation_engine.handle_manual_override.call_count == 0


class TestActiveOverrideSupersession:
    """Branch 2 (Fix D): a different mode while an override grace is already running."""

    def test_recovery_does_not_supersede_active_override(self):
        coord = _make_coordinator_stub(manual_override_active=True)
        coord.automation_engine._manual_override_mode = "cool"
        _run(coord, _make_event(old_hvac_mode="unavailable", new_hvac_mode="heat"))
        assert coord.automation_engine.handle_manual_override.call_count == 0

    def test_real_mode_change_still_supersedes(self):
        coord = _make_coordinator_stub(manual_override_active=True)
        coord.automation_engine._manual_override_mode = "cool"
        _run(coord, _make_event(old_hvac_mode="cool", new_hvac_mode="heat"))
        assert coord.automation_engine.handle_manual_override.call_count == 1


class TestPauseOverride:
    """Branch 1: user turns HVAC on during a door/window pause."""

    def test_recovery_during_pause_is_not_an_override(self):
        coord = _make_coordinator_stub()
        coord.automation_engine.is_paused_by_door = True
        _run(coord, _make_event(old_hvac_mode="unavailable", new_hvac_mode="heat"))
        assert coord.automation_engine.handle_manual_override_during_pause.call_count == 0

    def test_real_turn_on_during_pause_still_overrides(self):
        coord = _make_coordinator_stub()
        coord.automation_engine.is_paused_by_door = True
        coord._cancel_all_debounce_timers = MagicMock()
        _run(coord, _make_event(old_hvac_mode="off", new_hvac_mode="heat"))
        assert coord.automation_engine.handle_manual_override_during_pause.call_count == 1


class TestSetpointOverride:
    """Branch 4: setpoint-only override. A reconnecting thermostat has no prior setpoint."""

    def test_recovery_with_no_prior_setpoint_is_not_an_override(self):
        coord = _make_coordinator_stub()
        _run(coord, _make_event(old_hvac_mode="unavailable", new_hvac_mode="cool", new_temp=72.0))
        assert coord.automation_engine.handle_manual_override.call_count == 0
        assert coord._today_record.manual_overrides == 0

    def test_missing_prior_setpoint_without_unavailable_keeps_its_original_override_semantics(self):
        """Only an availability recovery is exempt (the broader ``old_temp is not None`` guard was removed)."""
        coord = _make_coordinator_stub()
        _run(coord, _make_event(old_hvac_mode="cool", new_hvac_mode="cool", new_temp=72.0))
        assert coord.automation_engine.handle_manual_override.call_count == 1

    def test_real_setpoint_change_still_overrides(self):
        coord = _make_coordinator_stub()
        _run(coord, _make_event(old_hvac_mode="cool", new_hvac_mode="cool", old_temp=74.0, new_temp=72.0))
        assert coord.automation_engine.handle_manual_override.call_count == 1
        assert coord._today_record.manual_overrides == 1
