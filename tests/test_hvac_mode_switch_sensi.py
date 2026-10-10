"""Sensi-like thermostat mode-switch tests (Issue #1048).

Sensi (and HA-core ecobee) thermostats IGNORE the ``hvac_mode`` kwarg of
``climate.set_temperature`` and Sensi rejects ``set_temperature`` while the
thermostat is Off. Climate Advisor historically turned heating/cooling on only by
embedding ``hvac_mode`` in ``set_temperature``, so on such a thermostat the mode
never changed and the (non-blocking) rejection was swallowed by HA.

These tests drive the REAL ``AutomationEngine`` against the harness ``FakeHass``
with the opt-in ``sensi_like`` quirk (tools/sim_harness/fake_hass.py) and assert
on the thermostat's ACTUAL resulting state. The ``TestSensiLikeFake`` class pins
the fake's own contract and passes today; every ``TestEngine...`` test is expected
to FAIL until the explicit ``climate.set_hvac_mode`` switch ships (Issue #1048).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from custom_components.climate_advisor.automation import ComfortBand
from tools.sim_harness.build_engine import build_headless_engine
from tools.sim_harness.fake_hass import QUIRK_SENSI_LIKE, FakeHomeAssistantError

ENTITY = "climate.test_thermostat"
START = datetime(2026, 1, 12, 7, 0, 0, tzinfo=UTC)
CONFIG = {"comfort_heat": 70, "comfort_cool": 75, "setback_heat": 60, "setback_cool": 80, "fan_mode": "disabled"}


def _build(initial_state: str, *, sensi: bool = True, indoor_f: float = 64.0):
    engine, hass, scheduler, event_log = build_headless_engine(
        config=dict(CONFIG),
        climate_entity=ENTITY,
        climate_state=initial_state,
        climate_attributes={"current_temperature": indoor_f},
        start_time=START,
    )
    if sensi:
        hass.set_entity_quirk(ENTITY, QUIRK_SENSI_LIKE)
    return engine, hass, scheduler, event_log


def _run(scheduler, coro):
    with scheduler.installed():
        asyncio.run(coro)


def _mode_calls(hass, mode):
    return [
        a
        for a in hass.action_log
        if a["domain"] == "climate" and a["service"] == "set_hvac_mode" and a["data"].get("hvac_mode") == mode
    ]


class TestSensiLikeFake:
    """The fake's quirk contract (passes today; guards the test foundation itself)."""

    def test_set_temperature_ignores_hvac_mode_kwarg(self):
        _, hass, _, _ = _build("cool")
        asyncio.run(
            hass.services.async_call(
                "climate", "set_temperature", {"entity_id": ENTITY, "hvac_mode": "heat", "temperature": 70.0}
            )
        )
        assert hass.states.get(ENTITY).state == "cool"
        assert hass.states.get(ENTITY).attributes["temperature"] == 70.0

    def test_set_temperature_while_off_nonblocking_is_swallowed(self):
        _, hass, _, _ = _build("off")
        asyncio.run(
            hass.services.async_call(
                "climate", "set_temperature", {"entity_id": ENTITY, "hvac_mode": "heat", "temperature": 70.0}
            )
        )
        assert hass.states.get(ENTITY).state == "off"
        assert "temperature" not in hass.states.get(ENTITY).attributes

    def test_set_temperature_while_off_blocking_raises(self):
        _, hass, _, _ = _build("off")
        with pytest.raises(FakeHomeAssistantError):
            asyncio.run(
                hass.services.async_call(
                    "climate", "set_temperature", {"entity_id": ENTITY, "temperature": 70.0}, blocking=True
                )
            )

    def test_set_hvac_mode_works_and_default_entity_unchanged(self):
        _, hass, _, _ = _build("off")
        asyncio.run(hass.services.async_call("climate", "set_hvac_mode", {"entity_id": ENTITY, "hvac_mode": "heat"}))
        assert hass.states.get(ENTITY).state == "heat"

        _, plain, _, _ = _build("off", sensi=False)
        asyncio.run(
            plain.services.async_call(
                "climate", "set_temperature", {"entity_id": ENTITY, "hvac_mode": "heat", "temperature": 70.0}
            )
        )
        assert plain.states.get(ENTITY).state == "heat"


class TestEngineSwitchesModeOnSensiLikeThermostat:
    """Issue #1048 — EXPECTED TO FAIL on HEAD: CA never issues set_hvac_mode."""

    def test_set_temperature_heat_from_off_switches_thermostat_to_heat(self):
        engine, hass, scheduler, _ = _build("off")
        _run(scheduler, engine._set_temperature(70.0, reason="test", mode="heat"))
        assert _mode_calls(hass, "heat"), "no climate.set_hvac_mode(heat) was issued; Off thermostat never switched on"
        assert hass.states.get(ENTITY).state == "heat"

    def test_set_temperature_cool_from_off_switches_thermostat_to_cool(self):
        engine, hass, scheduler, _ = _build("off", indoor_f=82.0)
        _run(scheduler, engine._set_temperature(75.0, reason="test", mode="cool"))
        assert _mode_calls(hass, "cool"), "no climate.set_hvac_mode(cool) was issued; Off thermostat never switched on"
        assert hass.states.get(ENTITY).state == "cool"

    def test_set_temperature_heat_from_cool_switches_mode(self):
        """Opposite mode (not just Off): a Sensi-like device ignores the kwarg, so it stays Cool."""
        engine, hass, scheduler, _ = _build("cool")
        _run(scheduler, engine._set_temperature(70.0, reason="test", mode="heat"))
        assert hass.states.get(ENTITY).state == "heat"

    def test_comfort_band_floor_from_off_switches_thermostat_to_heat(self):
        engine, hass, scheduler, _ = _build("off")
        band = ComfortBand(floor=70.0, ceiling=75.0, active="floor", reason="test cold morning")
        _run(scheduler, engine._apply_comfort_band(band, reason="test"))
        assert _mode_calls(hass, "heat"), "no climate.set_hvac_mode(heat) was issued for the comfort-band floor"
        assert hass.states.get(ENTITY).state == "heat"
        assert hass.states.get(ENTITY).attributes.get("temperature") == 70.0

    def test_comfort_band_ceiling_from_off_switches_thermostat_to_cool(self):
        engine, hass, scheduler, _ = _build("off", indoor_f=82.0)
        band = ComfortBand(floor=70.0, ceiling=75.0, active="ceiling", reason="test hot afternoon")
        _run(scheduler, engine._apply_comfort_band(band, reason="test"))
        assert hass.states.get(ENTITY).state == "cool"
