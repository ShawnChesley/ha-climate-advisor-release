"""Sleep temperature offset — pure leaf (no Home Assistant imports).

An optional numeric entity (``CONF_SLEEP_OFFSET_ENTITY``) shifts both sleep
setpoints by the same amount while it is set, e.g. +2 °F when one sleeper is away.

Design: the coordinator's ``config`` dict is shared by reference with the
automation engine and read by many call sites (``config.get("sleep_heat")``).
Rather than threading an offset through every reader, the coordinator writes the
EFFECTIVE value (``base + offset``) into ``sleep_heat`` / ``sleep_cool``.

The BASE values are owned explicitly by the coordinator (``_sleep_offset_base``):
captured once from the config entry at startup and changed only through
``split_base_changes()`` (the coordinator's runtime config-update path). They are
never inferred from the effective values, so a writer that read an offset value
can never fold the offset into the base. Nothing persists ``coordinator.config``
back to the config entry, so the stored entry always keeps the base values.

All temperatures here are internal °F, matching how setpoints are stored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

SLEEP_KEYS: tuple[str, ...] = ("sleep_heat", "sleep_cool")

# Absolute bounds for the EFFECTIVE sleep setpoints, °F — the options-form ranges
# for sleep_heat (55–79) and sleep_cool (55–89). The offset is reduced as needed so
# both stay inside, and because one offset moves both edges together, the
# heat/cool separation is always preserved.
SLEEP_BOUNDS_F: dict[str, tuple[float, float]] = {
    "sleep_heat": (55.0, 79.0),
    "sleep_cool": (55.0, 89.0),
}

_NOT_A_VALUE = {"unavailable", "unknown", "none", ""}


@dataclass(frozen=True)
class OffsetReading:
    """Result of reading the offset entity.

    ``value_f`` is None when the state is unusable (unavailable, unknown,
    non-numeric, non-finite) — the caller holds its last valid value.
    """

    value_f: float | None


def read_offset_f(
    raw_state: str | None,
    entity_unit: str | None,
    display_unit: str,
    max_abs_f: float,
) -> OffsetReading:
    """Convert an entity state to a clamped °F offset.

    Unit: the entity's own ``unit_of_measurement`` wins when it is a temperature
    unit (°C / °F); otherwise (input_number with no unit, a plain number) the
    value is in this zone's display unit. °C is converted as a delta (×1.8).
    """
    if raw_state is None or str(raw_state).strip().lower() in _NOT_A_VALUE:
        return OffsetReading(None)
    try:
        value = float(raw_state)
    except (TypeError, ValueError):
        return OffsetReading(None)
    if not math.isfinite(value):
        return OffsetReading(None)
    unit = (entity_unit or "").strip().upper().replace("°", "")
    if unit in ("C", "CELSIUS"):
        celsius = True
    elif unit in ("F", "FAHRENHEIT"):
        celsius = False
    else:
        celsius = str(display_unit).lower().startswith("c")
    if celsius:
        value *= 1.8
    value = max(-max_abs_f, min(max_abs_f, value))
    return OffsetReading(round(value, 2))


def bounded_offset(base: dict[str, float], offset_f: float) -> float:
    """Shrink ``offset_f`` so every base + offset stays inside SLEEP_BOUNDS_F.

    If any base value is itself outside its range, the offset is 0 in either
    direction — an out-of-range base is never moved (it is the user's explicit
    setting and is left exactly as configured).
    """
    hi = math.inf
    lo = -math.inf
    for key, value in base.items():
        b_lo, b_hi = SLEEP_BOUNDS_F[key]
        if not b_lo <= value <= b_hi:
            return 0.0
        hi = min(hi, b_hi - value)
        lo = max(lo, b_lo - value)
    return max(lo, min(hi, offset_f))


def effective_values(base: dict[str, float], offset_f: float) -> dict[str, float]:
    """The sleep setpoints to write: base + bounded offset, for keys in ``base`` only."""
    off = bounded_offset(base, offset_f)
    return {key: round(value + off, 2) for key, value in base.items()}


def capture_base(config: dict[str, Any]) -> dict[str, float]:
    """Base values from a config that has not been offset yet (startup)."""
    return {key: float(config[key]) for key in SLEEP_KEYS if config.get(key) is not None}


def split_base_changes(changes: dict[str, Any], base: dict[str, float]) -> dict[str, Any]:
    """Route sleep keys in a runtime config update to ``base`` (in place).

    Returns the remaining changes to apply to the config directly. A ``None``
    value removes the explicit sleep setpoint (back to adaptive sleep).
    """
    rest = dict(changes)
    for key in SLEEP_KEYS:
        if key in rest:
            value = rest.pop(key)
            if value is None:
                base.pop(key, None)
            else:
                base[key] = float(value)
    return rest


def write_effective(config: dict[str, Any], base: dict[str, float], offset_f: float) -> bool:
    """Write base + offset into ``config`` in place. Returns True when anything changed.

    Keys absent from ``base`` are removed from ``config`` so an offset never
    creates an explicit sleep setpoint (adaptive sleep stays adaptive).
    """
    changed = False
    target = effective_values(base, offset_f)
    for key in SLEEP_KEYS:
        if key in target:
            if config.get(key) is None or abs(float(config[key]) - target[key]) > 1e-9:
                config[key] = target[key]
                changed = True
        elif key in config:
            del config[key]
            changed = True
    return changed
