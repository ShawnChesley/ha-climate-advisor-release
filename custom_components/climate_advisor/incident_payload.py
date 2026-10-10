"""Shared ``incident_detected`` payload builder and climate-error classifier (Issue #1049).

Before this module the coordinator's ``_emit_incident()`` and four hand-built copies inside
``AutomationEngine`` each assembled the same envelope (``incident_class``, ``incident_id``,
``comfort_heat``, ``comfort_cool``, ``occupancy_mode`` + class-specific extras) independently,
and the copies drifted (one carried the display-unit setpoint under a ``_f`` key). Everything
that emits an incident now builds its payload here; emission itself stays with each owner
(coordinator ``_emit_event`` / engine ``_emit_event_callback``) — there is no second emitter.

All temperature values in a payload are internal Fahrenheit regardless of the user's display
unit (docs/incident-classes.md); renderers convert for display.
"""

from __future__ import annotations

from typing import Any

# Exception class names that HA raises when a climate service call is rejected by its own
# validation (value out of range, hvac mode not offered, bad schema) rather than failing
# at the device/integration level. Matched by name so the check works with the lightweight
# HA stubs used in tests and across HA versions that moved these classes.
_VALIDATION_ERROR_NAMES = frozenset({"ServiceValidationError", "Invalid", "MultipleInvalid", "ServiceNotSupported"})


def build_incident_payload(
    incident_class: str,
    incident_id: str,
    *,
    comfort_heat: float | None,
    comfort_cool: float | None,
    occupancy_mode: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the common ``incident_detected`` payload (temperatures in internal °F)."""
    payload: dict[str, Any] = {
        "incident_class": incident_class,
        "incident_id": incident_id,
        "comfort_cool": comfort_cool,
        "comfort_heat": comfort_heat,
        "occupancy_mode": occupancy_mode,
    }
    if extra:
        payload.update(extra)
    return payload


def is_climate_validation_error(err: BaseException) -> bool:
    """True when ``err`` is HA rejecting the call's *parameters* (not a device failure).

    Out-of-range setpoints and modes the thermostat does not offer are expected outcomes of
    a well-formed command against a specific device — they are logged and skipped, not
    treated as ``hvac_command_failed`` episodes.
    """
    return any(cls.__name__ in _VALIDATION_ERROR_NAMES for cls in type(err).__mro__)
