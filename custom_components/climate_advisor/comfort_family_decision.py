"""Comfort-family decision leaf — Design D, "nearest-edge arming" (Issue #1066).

A single-setpoint thermostat can only guard ONE edge of the comfort band at a
time: the floor (heat mode) or the ceiling (cool mode). This pure leaf decides
which one, every cycle, from the indoor temperature alone. Supersedes the
#821/#827/#843/#858/#867 against-grain machinery (day-type default edge,
breach + deadband + recency window + sustain + dwell + recovery margin), which
left the thermostat sitting in COOL below the comfort floor on mild/warm days.

Rule (symmetric; all temperatures internal Fahrenheit):

- ``indoor < floor``   -> heating, immediately (breach).
- ``indoor > ceiling`` -> cooling, immediately (breach).
- otherwise, with effective arming margin ``m`` (see ``effective_margin``):
  ``indoor <= floor + m``   -> heating (inclusive);
  ``indoor >= ceiling - m`` -> cooling (inclusive);
  strictly between           -> keep the family the thermostat is currently in
  (hysteresis memory), or ``default_family`` when the current family is unknown.

Capabilities: heat-only thermostat -> heating; cool-only -> cooling; neither ->
NOT_APPLICABLE. Nat-vent or whole-house-fan owning the HVAC, or missing
indoor/floor/ceiling -> NOT_APPLICABLE. Manual override, door/window pause and
occupancy are NOT inputs: callers run before this leaf and skip it.

TOU banking (Issue #1066 F1): an ACTIVE time-of-use precondition (the scheduler's
``PRECONDITIONING`` phase, i.e. banking toward an upcoming high-cost window) also makes the
leaf NOT_APPLICABLE, exactly like nat-vent/WHF. The bank deliberately drives the setpoint
to the comfort edge OPPOSITE the day's reactive one (cool to the floor, or heat to the
ceiling); without the deferral the selector would flip the thermostat back at the first
margin reading and the next TOU write would re-bank it (heat and cool alternating).
Breach during banking is deliberately NOT carved out: the scheduler's banking target IS a
comfort edge (``resolve_comfort_heat``/``resolve_comfort_cool``, sleep-aware), the phase is
bounded by the cost window start, and once the phase ends the selector resumes on the very
next cycle (``docs/scheduler-spec.md`` "Coast Phase": comfort wins after the bank, via the
normal band). The accepted residual risk is only sensor/thermostat overshoot past the bank
target for the duration of the lead window (30-240 min).

The leaf is stateless and has no Home Assistant imports. It never enforces a
dwell lock — ``comfort_family_fsm.py`` owns the anti-chatter timer and lets any
breach decision through regardless of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .const import DAY_TYPE_COLD, DAY_TYPE_COOL, DAY_TYPE_HOT, DAY_TYPE_WARM

_FAMILY_HEATING = "heating"
_FAMILY_COOLING = "cooling"

# The arming margin is never smaller than this, even if the configured margin
# is: sensor noise / 1F thermostat resolution would otherwise flicker the arm.
COMFORT_MIN_ARMING_MARGIN_F = 2.0
# If the clamped margin (capped at one third of the band width) falls below
# this, the band is too narrow to flip safely in its interior: only breaches
# switch the family.
COMFORT_MIN_USABLE_MARGIN_F = 1.0


class ComfortFamilyOutcome(Enum):
    NOT_APPLICABLE = "not_applicable"
    HOLD = "hold"
    SWITCH = "switch"


@dataclass(frozen=True)
class ComfortFamilyDecision:
    outcome: ComfortFamilyOutcome
    target_family: str | None = None
    reason: str = ""
    # indoor is outside [floor, ceiling]
    breach: bool = False
    # The effective margin used; None when not computed or band too narrow.
    margin_applied_f: float | None = None
    heat_zone_limit_f: float | None = None  # floor + m
    cool_zone_limit_f: float | None = None  # ceiling - m


@dataclass(frozen=True)
class ComfortFamilyInputs:
    # Family the thermostat is currently in; None = unknown/off/heat_cool/unavailable.
    current_family: str | None
    # Tie-break inside the hysteresis gap when current_family is None.
    default_family: str
    indoor: float | None
    floor: float | None
    ceiling: float | None
    margin_f: float
    natural_vent_active: bool
    whf_owns_hvac: bool
    now: datetime
    supports_heat: bool = True
    supports_cool: bool = True
    # A TOU precondition (banking toward a high-cost window) owns the thermostat.
    tou_precondition_active: bool = False


def _native_family(day_type: str | None) -> str | None:
    """The day's native comfort family, or ``None`` for a day type with no strong preference.

    Not part of the arming decision (Design D ignores the day type). Kept because the comfort-stall
    detector (``coordinator._check_comfort_stall`` via ``comfort_stall_decision.stall_margins``) still asks
    which direction a day type favors when it scales its per-direction stall margins.
    """
    if day_type in (DAY_TYPE_HOT, DAY_TYPE_WARM):
        return _FAMILY_COOLING
    if day_type in (DAY_TYPE_COOL, DAY_TYPE_COLD):
        return _FAMILY_HEATING
    return None


def effective_margin(margin_f: float, floor: float, ceiling: float) -> float | None:
    """Clamp the configured margin to ``[COMFORT_MIN_ARMING_MARGIN_F, width/3]``.

    Returns None when the result is below ``COMFORT_MIN_USABLE_MARGIN_F`` (band
    too narrow for interior flipping).
    """
    width = ceiling - floor
    m = min(max(margin_f, COMFORT_MIN_ARMING_MARGIN_F), width / 3.0)
    if m < COMFORT_MIN_USABLE_MARGIN_F:
        return None
    return m


def _decision(
    inputs: ComfortFamilyInputs,
    target: str,
    reason: str,
    *,
    breach: bool = False,
    margin: float | None = None,
    heat_limit: float | None = None,
    cool_limit: float | None = None,
) -> ComfortFamilyDecision:
    switch = inputs.current_family is not None and target != inputs.current_family
    return ComfortFamilyDecision(
        outcome=ComfortFamilyOutcome.SWITCH if switch else ComfortFamilyOutcome.HOLD,
        target_family=target,
        reason=reason,
        breach=breach,
        margin_applied_f=margin,
        heat_zone_limit_f=heat_limit,
        cool_zone_limit_f=cool_limit,
    )


def decide_comfort_family(inputs: ComfortFamilyInputs) -> ComfortFamilyDecision:
    """Return which comfort edge the thermostat should guard this cycle."""
    if inputs.natural_vent_active or inputs.whf_owns_hvac:
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.NOT_APPLICABLE,
            target_family=inputs.current_family,
            reason="nat-vent/WHF owns HVAC — comfort family defers",
        )
    if inputs.tou_precondition_active:
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.NOT_APPLICABLE,
            target_family=inputs.current_family,
            reason="TOU precondition owns HVAC — comfort family defers",
        )
    if inputs.indoor is None or inputs.floor is None or inputs.ceiling is None:
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.NOT_APPLICABLE,
            target_family=inputs.current_family,
            reason="missing indoor temperature or comfort band",
        )

    indoor, floor, ceiling = inputs.indoor, inputs.floor, inputs.ceiling
    breach = indoor < floor or indoor > ceiling

    if not inputs.supports_heat and not inputs.supports_cool:
        return ComfortFamilyDecision(
            outcome=ComfortFamilyOutcome.NOT_APPLICABLE,
            target_family=inputs.current_family,
            reason="thermostat supports neither heat nor cool",
            breach=breach,
        )
    if inputs.supports_heat and not inputs.supports_cool:
        return _decision(inputs, _FAMILY_HEATING, "heat-only thermostat", breach=breach)
    if inputs.supports_cool and not inputs.supports_heat:
        return _decision(inputs, _FAMILY_COOLING, "cool-only thermostat", breach=breach)

    m = effective_margin(inputs.margin_f, floor, ceiling)
    heat_limit = floor + m if m is not None else None
    cool_limit = ceiling - m if m is not None else None

    if indoor < floor:
        return _decision(
            inputs,
            _FAMILY_HEATING,
            "floor breach — arming heat immediately",
            breach=True,
            margin=m,
            heat_limit=heat_limit,
            cool_limit=cool_limit,
        )
    if indoor > ceiling:
        return _decision(
            inputs,
            _FAMILY_COOLING,
            "ceiling breach — arming cool immediately",
            breach=True,
            margin=m,
            heat_limit=heat_limit,
            cool_limit=cool_limit,
        )

    if heat_limit is None or cool_limit is None:
        target = inputs.current_family or inputs.default_family
        return _decision(inputs, target, "band too narrow for interior switching", margin=m)

    if indoor <= heat_limit:
        return _decision(
            inputs,
            _FAMILY_HEATING,
            "within heat arming margin",
            margin=m,
            heat_limit=heat_limit,
            cool_limit=cool_limit,
        )
    if indoor >= cool_limit:
        return _decision(
            inputs,
            _FAMILY_COOLING,
            "within cool arming margin",
            margin=m,
            heat_limit=heat_limit,
            cool_limit=cool_limit,
        )

    target = inputs.current_family or inputs.default_family
    return _decision(
        inputs,
        target,
        "in hysteresis gap — keeping current family",
        margin=m,
        heat_limit=heat_limit,
        cool_limit=cool_limit,
    )
