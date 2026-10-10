"""Issue #1050 (#1048 change 20): handle_bedtime()'s recent-write guard is sleep-window aware.

Occupant: a setpoint written seconds before bedtime no longer delays the sleep band by up to
30 minutes. The #299 startup double-write guard still protects a write made INSIDE the sleep
period (and everywhere when sleep_time/wake_time are unset).

AWAY occupancy is the observable "bedtime proceeded" signal: the proceeding path emits
``bedtime_setback_skipped``; the guarded path returns before emitting anything.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import OCCUPANCY_AWAY

_MOD = "custom_components.climate_advisor.automation"


def _consume(coro):
    coro.close()


def _engine(config_overrides: dict | None = None) -> AutomationEngine:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume)
    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
        "sleep_time": "22:30",
        "wake_time": "06:30",
    }
    config.update(config_overrides or {})
    eng = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )
    eng.set_occupancy_mode(OCCUPANCY_AWAY)
    return eng


def _run(eng: AutomationEngine, now: datetime, write_at: datetime) -> list[str]:
    emitted: list[str] = []
    eng._emit_event_callback = lambda event, data: emitted.append(event)
    eng._temp_command_time = write_at
    with patch(f"{_MOD}.dt_util.now", return_value=now):
        asyncio.run(eng.handle_bedtime())
    return emitted


class TestBedtimeRecentWriteGuard:
    def test_write_seconds_before_bedtime_does_not_swallow_bedtime(self, caplog):
        """Write at 22:29:50 (daytime band), bedtime at 22:30:05 -> bedtime runs; INFO logged."""
        eng = _engine()
        now = datetime(2026, 10, 9, 22, 30, 5).astimezone()
        with caplog.at_level(logging.INFO):
            emitted = _run(eng, now, now - timedelta(seconds=15))
        assert "bedtime_setback_skipped" in emitted
        assert any("proceeding" in r.message for r in caplog.records)

    def test_write_inside_sleep_window_still_skips(self, caplog):
        """#299 protection kept: startup write at 23:00:00, bedtime at 23:00:10 -> skipped, INFO logged."""
        eng = _engine()
        now = datetime(2026, 10, 9, 23, 0, 10).astimezone()
        with caplog.at_level(logging.INFO):
            emitted = _run(eng, now, now - timedelta(seconds=10))
        assert emitted == []
        assert any("skipping" in r.message for r in caplog.records)

    def test_sleep_time_unset_keeps_prior_skip_behavior(self):
        """No sleep_time: _in_sleep_window() cannot answer, so the #299 guard is unchanged."""
        eng = _engine({"sleep_time": None})
        now = datetime(2026, 10, 9, 22, 30, 5).astimezone()
        assert _run(eng, now, now - timedelta(seconds=15)) == []

    def test_write_older_than_30s_never_skips(self):
        eng = _engine()
        now = datetime(2026, 10, 9, 23, 0, 40).astimezone()
        assert "bedtime_setback_skipped" in _run(eng, now, now - timedelta(seconds=40))
