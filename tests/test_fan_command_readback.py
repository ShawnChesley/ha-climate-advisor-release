"""Issue #1050 (#1048 change 21): fan-on command read-back.

Occupant: a whole-house-fan switch that silently ignores "on" (fan-state feedback off, the default)
no longer leaves Climate Advisor believing the fan runs and the AC suppressed forever. Within two
backstop ticks (~5-10 min) it notices the switch never reacted, releases the AC and retries once
(no grace period, which would block the retry); a second ignore raises ONE warning and falls back
to the normal correction. Remote/IR/momentary fans that cannot report state are left alone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)
from custom_components.climate_advisor.fan_status import fan_entity_on

FAN = "switch.whf"


def _consume(coro):
    coro.close()


def _fan_state(state="off", changed=None, assumed=False):
    s = MagicMock()
    s.state = state
    s.attributes = {"assumed_state": True} if assumed else {}
    s.last_changed = changed
    return s


def _engine(fan_state, *, fan_mode=FAN_MODE_WHOLE_HOUSE, command_ago_s=600.0):
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume)
    hass.states.get.side_effect = lambda eid: fan_state if eid == FAN else MagicMock(attributes={})
    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        CONF_FAN_MODE: fan_mode,
        CONF_FAN_ENTITY: FAN,
    }
    eng = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )
    now = datetime.now().astimezone()
    eng._fan_toggle_command_time = now - timedelta(seconds=command_ago_s)
    eng._fan_active = True
    eng._natural_vent_active = True
    eng._pre_fan_hvac_mode = "cool"
    eng._sensor_check_callback = lambda: True  # window open: nat-vent session is justified
    eng._get_fan_physical_state_callback = MagicMock(return_value=None)  # feedback off
    eng._is_recent_fan_command_callback = MagicMock(return_value=False)
    eng._start_grace_period = MagicMock()
    events: list[tuple[str, dict]] = []
    eng._emit_event_callback = lambda e, d: events.append((e, d))
    return eng, events


def _ignored_state(eng):
    """Entity off, last changed an hour before CA's command: it never reacted."""
    return _fan_state("off", eng._fan_toggle_command_time - timedelta(hours=1))


class TestFanEntityOn:
    def test_on_off(self):
        assert fan_entity_on(_fan_state("on")) is True
        assert fan_entity_on(_fan_state("ON")) is True
        assert fan_entity_on(_fan_state("off")) is False

    def test_unknowable_is_none_not_off(self):
        assert fan_entity_on(None) is None
        assert fan_entity_on(_fan_state("unavailable")) is None
        assert fan_entity_on(_fan_state("unknown")) is None
        assert fan_entity_on(_fan_state("off", assumed=True)) is None


class TestReadback:
    def test_ignored_command_retries_once_without_grace(self):
        eng, events = _engine(None)
        st = _ignored_state(eng)
        eng.hass.states.get.side_effect = lambda eid: st if eid == FAN else MagicMock(attributes={})

        eng._reconcile_fan_physical_drift()  # tick 1: awaiting
        assert eng._fan_active is True
        eng._reconcile_fan_physical_drift()  # tick 2: confirmed

        assert eng._fan_active is False, "belief released"
        assert eng._natural_vent_active is True, "session preserved so the nat-vent re-fire can retry"
        eng._start_grace_period.assert_not_called()  # a grace period would block the retry
        cancel = [d for e, d in events if e == "fan_cancel"]
        assert len(cancel) == 1 and cancel[0]["readback"] is True and cancel[0]["retry"] is True
        assert eng._fan_readback_attempts == 1
        eng.hass.async_create_task.assert_called()  # only the min-runtime restart, never an off-command
        assert eng._fan_command_pending is False

    def test_second_ignore_gives_up_with_grace_and_one_warning(self, caplog):
        eng, events = _engine(None)
        st = _ignored_state(eng)
        eng.hass.states.get.side_effect = lambda eid: st if eid == FAN else MagicMock(attributes={})
        for _ in range(2):
            eng._reconcile_fan_physical_drift()
        # Retry: CA re-activates; the switch ignores it again.
        eng._fan_active = True
        eng._fan_toggle_command_time = datetime.now().astimezone() - timedelta(seconds=600)
        with caplog.at_level(logging.WARNING):
            for _ in range(2):
                eng._reconcile_fan_physical_drift()
        assert eng._start_grace_period.call_count == 1, "second failure uses the normal correction (grace)"
        assert sum("Fan command ignored twice" in r.message for r in caplog.records) == 1
        cancel = [d for e, d in events if e == "fan_cancel"]
        assert [c["retry"] for c in cancel] == [True, False]
        # A third failure does not raise a second warning (same episode).
        caplog.clear()
        eng._fan_active = True
        with caplog.at_level(logging.WARNING):
            for _ in range(2):
                eng._reconcile_fan_physical_drift()
        assert not any("Fan command ignored twice" in r.message for r in caplog.records)

    def test_assumed_state_fan_is_left_alone(self):
        eng, events = _engine(None)
        st = _fan_state("off", eng._fan_toggle_command_time - timedelta(hours=1), assumed=True)
        eng.hass.states.get.side_effect = lambda eid: st if eid == FAN else MagicMock(attributes={})
        for _ in range(3):
            eng._reconcile_fan_physical_drift()
        assert eng._fan_active is True and not events

    def test_unavailable_fan_is_left_alone(self):
        eng, events = _engine(None)
        st = _fan_state("unavailable", eng._fan_toggle_command_time - timedelta(hours=1))
        eng.hass.states.get.side_effect = lambda eid: st if eid == FAN else MagicMock(attributes={})
        for _ in range(3):
            eng._reconcile_fan_physical_drift()
        assert eng._fan_active is True and not events

    def test_off_after_the_command_is_a_manual_change_not_ours(self):
        """Entity changed AFTER CA's command: someone turned it off -- the manual listener owns it."""
        eng, events = _engine(None)
        st = _fan_state("off", eng._fan_toggle_command_time + timedelta(seconds=120))
        eng.hass.states.get.side_effect = lambda eid: st if eid == FAN else MagicMock(attributes={})
        for _ in range(3):
            eng._reconcile_fan_physical_drift()
        assert eng._fan_active is True and not events

    def test_manual_or_remote_session_is_left_alone(self):
        eng, events = _engine(None)
        eng._fan_override_active = True
        st = _ignored_state(eng)
        eng.hass.states.get.side_effect = lambda eid: st if eid == FAN else MagicMock(attributes={})
        for _ in range(3):
            eng._reconcile_fan_physical_drift()
        assert eng._fan_active is True and not events

    def test_hvac_fan_mode_is_a_different_mechanism(self):
        eng, events = _engine(None, fan_mode=FAN_MODE_HVAC)
        st = _ignored_state(eng)
        eng.hass.states.get.side_effect = lambda eid: st if eid == FAN else MagicMock(attributes={"fan_mode": "on"})
        for _ in range(3):
            eng._reconcile_fan_physical_drift()
        assert not any(d.get("readback") for _e, d in events)

    def test_responsive_fan_resets_the_episode(self):
        eng, _events = _engine(None)
        st = _ignored_state(eng)
        eng.hass.states.get.side_effect = lambda eid: st if eid == FAN else MagicMock(attributes={})
        for _ in range(2):
            eng._reconcile_fan_physical_drift()
        assert eng._fan_readback_attempts == 1
        eng._fan_active = True
        on = _fan_state("on", datetime.now().astimezone())
        eng.hass.states.get.side_effect = lambda eid: on if eid == FAN else MagicMock(attributes={})
        eng._reconcile_fan_physical_drift()
        assert eng._fan_readback_attempts == 0 and eng._fan_readback_announced is None

    def test_fsm_builder_physical_on_matches_reconcile(self):
        """Lockstep: _build_fan_fsm_inputs() sees the same read-back ground truth."""
        eng, _events = _engine(None)
        st = _ignored_state(eng)
        eng.hass.states.get.side_effect = lambda eid: st if eid == FAN else MagicMock(attributes={})
        assert eng._build_fan_fsm_inputs().physical_on is False
