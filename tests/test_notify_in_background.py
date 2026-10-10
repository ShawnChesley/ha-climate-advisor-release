"""Issue #1050 change 26: reminder_due() and the coordinator's _notify_in_background()."""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import types
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.temperature import reminder_due

T0 = dt.datetime(2026, 1, 1, 12, 0, 0)


class TestReminderDue:
    def test_never_sent_is_due(self):
        assert reminder_due(None, T0, 3600) is True

    def test_within_interval_not_due(self):
        assert reminder_due(T0, T0 + dt.timedelta(seconds=3599), 3600) is False

    def test_exactly_at_interval_is_due(self):
        assert reminder_due(T0, T0 + dt.timedelta(seconds=3600), 3600) is True


def _coord(engine):
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord.hass = MagicMock()
    tasks: list = []
    coord.hass.async_create_task = MagicMock(side_effect=tasks.append)
    coord.automation_engine = engine
    coord._notify_in_background = types.MethodType(mod.ClimateAdvisorCoordinator._notify_in_background, coord)
    return coord, tasks


class TestNotifyInBackground:
    def test_default_sends_push_and_email_channels(self):
        engine = MagicMock()
        engine._notify = AsyncMock()
        coord, tasks = _coord(engine)
        coord._notify_in_background("Title", "Msg", "entity_health")
        asyncio.run(tasks[0])
        engine._notify.assert_awaited_once_with("Msg", "Title", notification_type="entity_health")

    def test_email_false_forwarded(self):
        engine = MagicMock()
        engine._notify = AsyncMock()
        coord, tasks = _coord(engine)
        coord._notify_in_background("Title", "Msg", "x", email=False)
        asyncio.run(tasks[0])
        engine._notify.assert_awaited_once_with("Msg", "Title", notification_type="x", email=False)

    def test_notify_failure_is_swallowed_and_logged(self, caplog):
        engine = MagicMock()
        engine._notify = AsyncMock(side_effect=RuntimeError("boom"))
        coord, tasks = _coord(engine)
        coord._notify_in_background("Title", "Msg", "x")
        asyncio.run(tasks[0])  # must not raise
        assert "Background notification failed" in caplog.text

    def test_no_engine_is_noop(self):
        coord, tasks = _coord(None)
        coord._notify_in_background("Title", "Msg", "x")
        assert tasks == []
