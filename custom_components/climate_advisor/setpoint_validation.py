"""Validate submitted bands against ordering and HA's displayed thermostat bounds."""

from __future__ import annotations

import math
from typing import Any

from .temperature import ha_system_unit, to_fahrenheit

BANDS = ("comfort", "setback", "sleep")


def validate_setpoints(hass: Any, climate_entity: str | None, values: dict, unit: str) -> dict[str, str]:
    """Return field errors; never save or issue a thermostat command.

    HA climate state bounds are expressed in the HA system temperature unit,
    which can differ from CA's chosen display unit. Missing/invalid bounds are
    skipped independently; do not invent a hardware minimum for other devices.
    """
    errors: dict[str, str] = {}
    canonical = {}
    for band in BANDS:
        for side in ("heat", "cool"):
            key = f"{band}_{side}"
            if key not in values:
                continue
            try:
                value = float(values[key])
                if not math.isfinite(value):
                    raise ValueError
                converted = to_fahrenheit(value, unit)
                if not math.isfinite(converted):
                    raise ValueError
                canonical[key] = converted
            except (TypeError, ValueError, OverflowError):
                errors[key] = "invalid_temperature"
        heat, cool = f"{band}_heat", f"{band}_cool"
        if heat in canonical and cool in canonical and canonical[cool] < canonical[heat] - 1e-8:
            errors[cool] = "cool_below_heat"

    state = hass.states.get(climate_entity) if isinstance(climate_entity, str) else None
    attributes = getattr(state, "attributes", {})
    system_unit = ha_system_unit(hass)
    if not isinstance(attributes, dict) or system_unit is None:
        return errors
    for attribute, error, lower in (
        ("min_temp", "below_thermostat_min", True),
        ("max_temp", "above_thermostat_max", False),
    ):
        try:
            bound = float(attributes[attribute])
            if not math.isfinite(bound):
                continue
            bound = to_fahrenheit(bound, system_unit)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        for key, value in canonical.items():
            if (lower and value < bound - 1e-8) or (not lower and value > bound + 1e-8):
                errors[key] = error
    return errors
