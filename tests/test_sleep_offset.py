"""Tests for the sleep temperature offset entity (sleep_offset.py + coordinator glue)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    CONF_SLEEP_OFFSET_ENTITY,
    CONFIG_METADATA,
    SLEEP_OFFSET_MAX_F,
)
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.sleep_offset import (
    bounded_offset,
    capture_base,
    effective_values,
    read_offset_f,
    split_base_changes,
    write_effective,
)

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "climate_advisor"
ENTITY = "input_number.master_sleep_offset"
_AUTOMATION = "custom_components.climate_advisor.automation"
_NIGHT = datetime(2026, 11, 11, 23, 30, 0, tzinfo=UTC)

# ── read_offset_f ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "entity_unit", "display", "expected"),
    [
        ("2", None, "fahrenheit", 2.0),
        ("2.0", "°F", "fahrenheit", 2.0),
        ("-1.5", None, "fahrenheit", -1.5),
        ("0", None, "fahrenheit", 0.0),
        ("1", None, "celsius", 1.8),
        ("1", "°C", "fahrenheit", 1.8),  # entity unit wins over display unit
        ("2", "°F", "celsius", 2.0),
        ("2", "%", "fahrenheit", 2.0),  # non-temperature unit → display unit
        ("9", None, "fahrenheit", SLEEP_OFFSET_MAX_F),
        ("-9", None, "fahrenheit", -SLEEP_OFFSET_MAX_F),
        ("5", "°C", "fahrenheit", SLEEP_OFFSET_MAX_F),
    ],
)
def test_read_valid(raw, entity_unit, display, expected):
    assert read_offset_f(raw, entity_unit, display, SLEEP_OFFSET_MAX_F).value_f == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["unavailable", "unknown", "", None, "abc", "nan", "inf", "-inf", "Infinity"])
def test_read_invalid_is_none(raw):
    assert read_offset_f(raw, None, "fahrenheit", SLEEP_OFFSET_MAX_F).value_f is None


# ── bounds / effective values ───────────────────────────────────────


def test_effective_shifts_both():
    assert effective_values({"sleep_heat": 61.0, "sleep_cool": 63.0}, 2.0) == {"sleep_heat": 63.0, "sleep_cool": 65.0}


def test_offset_bounded_by_ranges_and_preserves_gap():
    base = {"sleep_heat": 78.0, "sleep_cool": 80.0}
    assert bounded_offset(base, 5.0) == 1.0  # heat max 79
    eff = effective_values(base, 5.0)
    assert eff == {"sleep_heat": 79.0, "sleep_cool": 81.0}
    assert eff["sleep_cool"] - eff["sleep_heat"] == 2.0
    low = {"sleep_heat": 56.0, "sleep_cool": 58.0}
    assert bounded_offset(low, -5.0) == -1.0  # both min 55
    assert effective_values(low, -5.0) == {"sleep_heat": 55.0, "sleep_cool": 57.0}


@pytest.mark.parametrize("offset", [2.0, -2.0, 5.0, -5.0])
def test_base_outside_bounds_is_not_moved_either_direction(offset):
    assert bounded_offset({"sleep_heat": 80.0}, offset) == 0.0
    assert bounded_offset({"sleep_heat": 61.0, "sleep_cool": 90.0}, offset) == 0.0
    assert effective_values({"sleep_heat": 80.0}, offset) == {"sleep_heat": 80.0}


# ── write_effective / base ownership ────────────────────────────────


def test_write_does_not_compound_and_restores_exactly():
    cfg = {"sleep_heat": 61.0, "sleep_cool": 63.0}
    base = capture_base(cfg)
    assert write_effective(cfg, base, 2.0) is True
    assert cfg == {"sleep_heat": 63.0, "sleep_cool": 65.0}
    assert write_effective(cfg, base, 2.0) is False
    assert write_effective(cfg, base, 0.0) is True
    assert cfg == {"sleep_heat": 61.0, "sleep_cool": 63.0}


def test_base_change_while_offset_active_is_never_folded_in():
    """ChatGPT review #1: base 61, +2 active, base edited to 62 → 64 now, 62 after offset clears."""
    cfg = {"sleep_heat": 61.0, "sleep_cool": 63.0, "other": 1}
    base = capture_base(cfg)
    write_effective(cfg, base, 2.0)
    rest = split_base_changes({"sleep_heat": 62.0, "other": 2}, base)
    assert rest == {"other": 2}
    cfg.update(rest)
    write_effective(cfg, base, 2.0)
    assert cfg["sleep_heat"] == 64.0
    write_effective(cfg, base, 0.0)
    assert cfg["sleep_heat"] == 62.0
    assert cfg["sleep_cool"] == 63.0


def test_base_edit_equal_to_effective_value_is_still_honoured():
    """ChatGPT review #1 ambiguity: base changed to 63 while effective is already 63."""
    cfg = {"sleep_heat": 61.0, "sleep_cool": 63.0}
    base = capture_base(cfg)
    write_effective(cfg, base, 2.0)  # heat now 63
    split_base_changes({"sleep_heat": 63.0}, base)
    write_effective(cfg, base, 2.0)
    assert cfg["sleep_heat"] == 65.0
    write_effective(cfg, base, 0.0)
    assert cfg["sleep_heat"] == 63.0


def test_missing_keys_are_never_created():
    cfg = {"sleep_cool": 63.0}
    base = capture_base(cfg)
    write_effective(cfg, base, 2.0)
    assert "sleep_heat" not in cfg
    assert cfg["sleep_cool"] == 65.0


def test_clearing_base_key_returns_to_adaptive():
    cfg = {"sleep_heat": 61.0, "sleep_cool": 63.0}
    base = capture_base(cfg)
    write_effective(cfg, base, 2.0)
    split_base_changes({"sleep_heat": None}, base)
    write_effective(cfg, base, 2.0)
    assert "sleep_heat" not in cfg


# ── Coordinator glue ────────────────────────────────────────────────


def _coord(state: str | None, config: dict | None = None, unit: str | None = None):
    """Coordinator shell with only the fields the offset glue touches."""
    states = {}
    if state is not None:
        states[ENTITY] = SimpleNamespace(state=state, attributes={"unit_of_measurement": unit} if unit else {})
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    c = object.__new__(ClimateAdvisorCoordinator)
    c.hass = hass
    c.config = (
        config
        if config is not None
        else {"temp_unit": "fahrenheit", "sleep_heat": 61.0, "sleep_cool": 63.0, CONF_SLEEP_OFFSET_ENTITY: ENTITY}
    )
    c._sleep_offset_base = capture_base(c.config)
    c._sleep_offset_last_valid_f = 0.0
    c._sleep_offset_applied_f = 0.0
    c.async_request_refresh = AsyncMock()
    return c, states


def _set(states, value, unit=None):
    states[ENTITY] = SimpleNamespace(state=value, attributes={"unit_of_measurement": unit} if unit else {})


def test_coordinator_applies_offset_from_entity():
    c, _ = _coord("2.0")
    assert c._apply_sleep_offset() is True
    assert (c.config["sleep_heat"], c.config["sleep_cool"]) == (63.0, 65.0)
    assert c._sleep_offset_applied_f == 2.0
    assert c._sleep_offset_base == {"sleep_heat": 61.0, "sleep_cool": 63.0}


def test_coordinator_uses_entity_celsius_unit():
    c, _ = _coord("1", unit="°C")
    c._apply_sleep_offset()
    assert c.config["sleep_heat"] == pytest.approx(62.8)


def test_coordinator_unconfigured_entity_is_noop():
    c, _ = _coord("2.0")
    c.config.pop(CONF_SLEEP_OFFSET_ENTITY)
    assert c._apply_sleep_offset() is False
    assert c.config["sleep_heat"] == 61.0


def test_coordinator_holds_last_valid_through_outage():
    c, states = _coord("2.0")
    c._apply_sleep_offset()
    for bad in ("unavailable", "unknown", "inf", "garbage"):
        _set(states, bad)
        assert c._apply_sleep_offset() is False
        assert (c.config["sleep_heat"], c.config["sleep_cool"]) == (63.0, 65.0)
    _set(states, "0")
    c._apply_sleep_offset()
    assert (c.config["sleep_heat"], c.config["sleep_cool"]) == (61.0, 63.0)


def test_coordinator_entity_removed_reads_zero():
    c, states = _coord("2.0")
    c._apply_sleep_offset()
    states.clear()
    c._apply_sleep_offset()
    assert c.config["sleep_heat"] == 61.0


def test_coordinator_adaptive_sleep_reports_zero():
    c, _ = _coord("2.0", config={"temp_unit": "fahrenheit", CONF_SLEEP_OFFSET_ENTITY: ENTITY})
    assert c._apply_sleep_offset() is False
    assert c._sleep_offset_applied_f == 0.0
    assert "sleep_heat" not in c.config


def test_coordinator_state_change_refreshes_only_on_change():
    c, states = _coord("0")
    asyncio.run(c._async_sleep_offset_changed(MagicMock()))
    c.async_request_refresh.assert_not_awaited()
    _set(states, "2")
    asyncio.run(c._async_sleep_offset_changed(MagicMock()))
    c.async_request_refresh.assert_awaited_once()
    assert c.config["sleep_cool"] == 65.0


def test_runtime_changes_route_sleep_keys_to_base():
    c, _ = _coord("2")
    c._apply_sleep_offset()
    c.apply_runtime_config_changes({"sleep_heat": 62.0, "occupancy_setback_minutes": 5})
    assert c.config["occupancy_setback_minutes"] == 5
    assert c._sleep_offset_base["sleep_heat"] == 62.0
    assert c.config["sleep_heat"] == 64.0


def test_coordinator_does_not_touch_entry_data():
    entry_data = {"sleep_heat": 61.0, "sleep_cool": 63.0}
    c, _ = _coord("2.0", config={**dict(entry_data), "temp_unit": "fahrenheit", CONF_SLEEP_OFFSET_ENTITY: ENTITY})
    c._apply_sleep_offset()
    assert entry_data == {"sleep_heat": 61.0, "sleep_cool": 63.0}


def test_partially_instantiated_coordinator_is_safe():
    c = object.__new__(ClimateAdvisorCoordinator)
    assert c._apply_sleep_offset() is False


def test_subscribe_registers_listener_when_configured(monkeypatch):
    import custom_components.climate_advisor.coordinator as mod

    calls = []
    monkeypatch.setattr(mod, "async_track_state_change_event", lambda h, e, cb: calls.append(e) or (lambda: None))
    c, _ = _coord("0")
    c._unsub_occupancy_listeners = []
    c._zone_scoped = lambda cb: cb
    c._subscribe_sleep_offset_listener()
    assert calls == [ENTITY]
    assert len(c._unsub_occupancy_listeners) == 1


# ── Integration: real AutomationEngine re-arms the sleep band ──────
# ChatGPT review #3: a sleeping zone must receive the new range after the helper
# flips mid-night, and return when it flips back, with protections respected.


def _consume(coro):
    coro.close()


def _engine_sharing(config: dict, indoor: float) -> AutomationEngine:
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume)
    tstat = MagicMock()
    tstat.state = "off"
    tstat.attributes = {"hvac_modes": ["off", "heat", "cool"], "supported_features": 1, "current_temperature": indoor}
    hass.states = MagicMock()
    hass.states.get.return_value = tstat

    async def _call(domain, service, data, **_kw):
        if domain == "climate" and service == "set_hvac_mode":
            tstat.state = data["hvac_mode"]
        elif domain == "climate" and service == "set_temperature":
            tstat.state = data.get("hvac_mode", tstat.state)
            tstat.attributes["temperature"] = data["temperature"]

    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=_call)
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.master",
        weather_entity="weather.home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )
    engine._thermal_model = {}
    return engine


def _classification(day_type: str, hvac_mode: str) -> DayClassification:
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = hvac_mode
    obj.trend_direction = "stable"
    obj.trend_magnitude = 1.0
    obj.today_high = 60.0
    obj.today_low = 40.0
    obj.tomorrow_high = 60.0
    obj.tomorrow_low = 40.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _last_setpoint(engine):
    calls = [
        c.args[2]
        for c in engine.hass.services.async_call.call_args_list
        if c.args[0] == "climate" and c.args[1] == "set_temperature"
    ]
    return calls[-1] if calls else None


def _night_cycle(engine, cls):
    with patch(f"{_AUTOMATION}.dt_util.now", return_value=_NIGHT):
        asyncio.run(engine.apply_classification(cls))


def _shared_setup(indoor: float):
    config = {
        "comfort_heat": 70.0,
        "comfort_cool": 74.0,
        "setback_heat": 60.0,
        "setback_cool": 80.0,
        "sleep_time": "21:30",
        "wake_time": "07:00",
        "sleep_heat": 61.0,
        "sleep_cool": 63.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
        CONF_SLEEP_OFFSET_ENTITY: ENTITY,
    }
    coord, states = _coord("0", config=config)
    engine = _engine_sharing(config, indoor)  # same dict, as in production
    return coord, states, engine


@pytest.mark.parametrize(
    ("day", "mode", "indoor", "home", "away"),
    [
        ("mild", "cool", 66.0, 63.0, 65.0),  # mild night → cool to the sleep ceiling
        ("cold", "heat", 58.0, 61.0, 63.0),  # cold night → heat to the sleep floor
    ],
)
def test_midnight_flip_recommands_sleeping_zone(day, mode, indoor, home, away):
    coord, states, engine = _shared_setup(indoor)
    cls = _classification(day, mode)
    coord._apply_sleep_offset()
    _night_cycle(engine, cls)
    first = _last_setpoint(engine)
    assert first is not None and first["temperature"] == home

    _set(states, "2")  # Shawn leaves → helper +2
    assert coord._apply_sleep_offset() is True
    engine._last_hvac_command_time = None  # outside the recent-command settle window
    _night_cycle(engine, cls)
    assert _last_setpoint(engine)["temperature"] == away

    _set(states, "0")  # Shawn returns
    coord._apply_sleep_offset()
    engine._last_hvac_command_time = None
    _night_cycle(engine, cls)
    assert _last_setpoint(engine)["temperature"] == home


@pytest.mark.parametrize("override", [True, False])
def test_midnight_flip_manual_override(override):
    """Identical scenario with and without a manual override; only the override changes the outcome."""
    coord, states, engine = _shared_setup(66.0)
    cls = _classification("mild", "cool")
    coord._apply_sleep_offset()
    _night_cycle(engine, cls)
    assert _last_setpoint(engine)["temperature"] == 63.0
    before = len(engine.hass.services.async_call.call_args_list)
    engine._manual_override_active = override
    _set(states, "2")
    coord._apply_sleep_offset()
    engine._last_hvac_command_time = None  # same settle-guard bypass as the flip tests
    _night_cycle(engine, cls)
    sets = [
        c.args[2]
        for c in engine.hass.services.async_call.call_args_list[before:]
        if c.args[0] == "climate" and c.args[1] in ("set_temperature", "set_hvac_mode")
    ]
    if override:
        assert sets == []
    else:
        assert sets and sets[-1]["temperature"] == 65.0


def test_listener_refresh_engine_chain():
    """Helper state change → listener → coordinator refresh → engine command, end to end.

    The coordinator's refresh is wired to the engine's regular-cycle call
    (apply_classification), standing in for DataUpdateCoordinator's refresh.
    """
    coord, states, engine = _shared_setup(66.0)
    cls = _classification("mild", "cool")

    async def _refresh():
        engine._last_hvac_command_time = None
        await engine.apply_classification(cls)

    coord.async_request_refresh = AsyncMock(side_effect=_refresh)
    coord._apply_sleep_offset()
    _night_cycle(engine, cls)
    assert _last_setpoint(engine)["temperature"] == 63.0
    with patch(f"{_AUTOMATION}.dt_util.now", return_value=_NIGHT):
        _set(states, "2")
        asyncio.run(coord._async_sleep_offset_changed(MagicMock()))
        assert _last_setpoint(engine)["temperature"] == 65.0
        _set(states, "0")
        asyncio.run(coord._async_sleep_offset_changed(MagicMock()))
        assert _last_setpoint(engine)["temperature"] == 63.0
    assert coord.async_request_refresh.await_count == 2


# ── Metadata / strings ──────────────────────────────────────────────


def test_metadata_entry_present():
    meta = CONFIG_METADATA[CONF_SLEEP_OFFSET_ENTITY]
    assert meta["category"] == "occupancy"
    assert "display_transform" not in meta


@pytest.mark.parametrize("path", ["strings.json", "translations/en.json"])
def test_options_strings_present(path):
    data = json.loads((ROOT / path).read_text())
    step = data["options"]["step"]["occupancy"]
    assert CONF_SLEEP_OFFSET_ENTITY in step["data"]
    assert CONF_SLEEP_OFFSET_ENTITY in step["data_description"]
