"""Diagnostic snapshot builder for Climate Advisor (Issue #968, extended Issue #1049).

Single source of truth for "what does the configured HVAC/fan entity actually
report right now, what can the thermostat do, what did Climate Advisor last ask
it to do, and what CA version is running" -- the investigator report footer
(`ai_skills_investigator.py`), the "Download Logs"/"Download Full Logs" export and
Activity Record footer (`api.py`), and HA's native "Download diagnostics" file
(`diagnostics.py`) all render this same data rather than each independently
re-deriving it (mirroring the `fan_status.py`/`fan_mode_resolver.py` "tiny,
dependency-free utility module" precedent used in this codebase).

Data exposure (Issue #1049): a thermostat integration's attribute dict is third-party
defined and *may* carry identifying values (serial numbers, MAC/IP addresses,
latitude/longitude, the owner's name in `friendly_name`, picture URLs with tokens).
Two of this snapshot's consumers leave the machine (the AI investigator posts it to
the Claude API; log downloads end up pasted into public GitHub issues), so redaction
happens ONCE, here, inside the builder -- every consumer receives already-redacted
output and none can forget to filter. `entity_id` strings stay (they are already
printed in every investigator report). The redaction is key- and value-based
(`redact_sensitive()`), not HA's exact-key `async_redact_data`, because the attribute
key set is not enumerable; `diagnostics.py` still wraps its whole payload in
`async_redact_data(payload, TO_REDACT)` for config secrets (`notify_service`,
`ai_api_key`) -- two layers for two different threat models, not two competing lists.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .const import VERSION
from .temperature import from_fahrenheit

REDACTED = "**REDACTED**"

# Exact (lower-cased) attribute keys that are always identifying or secret.
_SENSITIVE_KEYS = frozenset(
    {
        "friendly_name",
        "unique_id",
        "device_id",
        "thermostat_id",
        "identifier",
        "id",
        "entity_picture",
    }
)
# Key tokens (split on non-alphanumerics) that mark a value as identifying or secret.
_SENSITIVE_TOKENS = frozenset(
    {
        "serial",
        "mac",
        "bssid",
        "ssid",
        "ip",
        "lat",
        "lon",
        "lng",
        "latitude",
        "longitude",
        "gps",
        "location",
        "address",
        "zip",
        "zipcode",
        "postal",
        "host",
        "hostname",
        "email",
        "phone",
        "imei",
        "uuid",
        "token",
        "password",
        "secret",
        "apikey",
        "username",
    }
)
# Substrings that identify a sensitive key even when glued together (``serialnumber``).
_SENSITIVE_SUBSTRINGS = ("serial", "latitude", "longitude", "password", "secret", "api_key", "macaddress", "token")
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_VERSION_KEY_RE = re.compile(r"version|firmware|fw", re.IGNORECASE)

# ClimateEntityFeature bit values (HA core; stable). Hard-coded so this module stays
# dependency-free; unknown bits are reported only through the raw integer.
_CLIMATE_FEATURES = {
    1: "target_temperature",
    2: "target_temperature_range",
    4: "target_humidity",
    8: "fan_mode",
    16: "preset_mode",
    32: "swing_mode",
    64: "aux_heat",
    128: "turn_off",
    256: "turn_on",
    512: "swing_horizontal_mode",
}

# Event types that explain what Climate Advisor asked the thermostat to do and how it answered.
_HVAC_EVENT_PREFIXES = (
    "hvac_",
    "setpoint_",
    "manual_override",
    "thermostat_",
    "classification_applied",
    "incident_detected",
)


def _key_is_sensitive(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if lowered in _SENSITIVE_KEYS:
        return True
    if any(part in lowered for part in _SENSITIVE_SUBSTRINGS):
        return True
    return any(tok in _SENSITIVE_TOKENS for tok in re.split(r"[^a-z0-9]+", lowered))


def _value_is_sensitive(key: Any, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if _MAC_RE.search(value):
        return True
    return bool(_IPV4_RE.match(value)) and not (isinstance(key, str) and _VERSION_KEY_RE.search(key))


def redact_sensitive(value: Any, _key: Any = None) -> Any:
    """Return a deep copy of ``value`` with identifying/secret entries replaced by ``REDACTED``.

    Dict entries whose key looks identifying (serial/MAC/IP/location/owner name/token...)
    or whose string value looks like a MAC or IPv4 address are replaced; lists and nested
    dicts are walked. Non-container, non-sensitive values pass through unchanged.
    """
    if isinstance(value, dict):
        return {k: (REDACTED if _key_is_sensitive(k) else redact_sensitive(v, k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_sensitive(item, _key) for item in value]
    if _value_is_sensitive(_key, value):
        return REDACTED
    return value


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def decode_supported_features(raw: Any) -> list[str]:
    """Names of the ClimateEntityFeature bits set in ``raw`` (empty when not an int)."""
    if not isinstance(raw, int) or isinstance(raw, bool):
        return []
    return [name for bit, name in _CLIMATE_FEATURES.items() if raw & bit]


def _build_capabilities(attrs: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the thermostat says it supports, parsed from its (unredacted) attributes."""
    if not attrs:
        return None
    modes = attrs.get("hvac_modes")
    modes_list = [m for m in modes if isinstance(m, str)] if isinstance(modes, (list, tuple)) else None
    raw_features = attrs.get("supported_features")
    return {
        "hvac_modes": modes_list,
        "supports_heat": ("heat" in modes_list) if modes_list is not None else None,
        "supports_cool": ("cool" in modes_list) if modes_list is not None else None,
        "supports_heat_cool": ("heat_cool" in modes_list) if modes_list is not None else None,
        "supported_features": raw_features if isinstance(raw_features, int) else None,
        "supported_feature_names": decode_supported_features(raw_features),
        "min_temp": attrs.get("min_temp"),
        "max_temp": attrs.get("max_temp"),
        "target_temp_step": attrs.get("target_temp_step"),
        "fan_modes": attrs.get("fan_modes"),
        "preset_modes": attrs.get("preset_modes"),
        "temperature_unit": attrs.get("temperature_unit"),
    }


def _build_last_command(coordinator: Any, live_state: str | None, unit: str) -> dict[str, Any] | None:
    """What Climate Advisor last asked the thermostat to do, and how it went (Issue #1049).

    Read-only view of engine fields that already exist (``_last_commanded_hvac_*``,
    ``_pending_setpoint_single``, ``_hvac_write_record``, the failure-episode pair); every
    value is type-checked so a partially-initialised engine yields ``None`` fields rather
    than an exception. Setpoints are reported both in internal Fahrenheit and in the
    configured display unit.
    """
    engine = getattr(coordinator, "automation_engine", None)
    if engine is None:
        return None
    record = getattr(engine, "_hvac_write_record", None)
    result = getattr(record, "result", None)
    commanded_mode = _str_or_none(getattr(engine, "_last_commanded_hvac_mode", None))
    setpoint_f = getattr(engine, "_pending_setpoint_single", None)
    if isinstance(setpoint_f, bool) or not isinstance(setpoint_f, (int, float)):
        setpoint_f = None
    return {
        "mode": commanded_mode,
        "mode_time": _iso(getattr(engine, "_last_commanded_hvac_time", None)),
        "setpoint_f": setpoint_f,
        "setpoint": round(from_fahrenheit(setpoint_f, unit), 1) if setpoint_f is not None else None,
        "setpoint_unit": unit,
        "setpoint_time": _iso(getattr(engine, "_temp_command_time", None)),
        "result": _str_or_none(getattr(result, "value", None)),
        "live_state": live_state,
        "live_state_matches_commanded": (
            (live_state == commanded_mode) if commanded_mode is not None and live_state is not None else None
        ),
        "read_back_skip_reason": _str_or_none(getattr(record, "apply_skip_reason", None)),
        "mode_mismatch_since": _iso(getattr(record, "mismatch_since", None)),
        "mode_mismatch_episode_mode": _str_or_none(getattr(record, "episode_reason", None)),
        "command_failure_since": _iso(getattr(engine, "_hvac_cmd_fail_since", None)),
        "command_failure_mode": _str_or_none(getattr(engine, "_hvac_cmd_fail_announced", None)),
    }


def recent_hvac_events(coordinator: Any, limit: int = 40) -> list[dict[str, Any]]:
    """The newest ``limit`` thermostat-related Activity Log events, redacted (what happened)."""
    log = getattr(coordinator, "_event_log", None)
    if not isinstance(log, list):
        return []
    picked = [e for e in log if isinstance(e, dict) and str(e.get("type", "")).startswith(_HVAC_EVENT_PREFIXES)]
    return redact_sensitive(picked[-limit:])


def build_diagnostic_snapshot(hass: Any, coordinator: Any) -> dict[str, Any]:
    """Return version + live entity snapshot + thermostat capabilities + last command, redacted.

    Every identifying attribute is redacted here (see module docstring) so all consumers
    -- including the AI investigator, which sends this to the Claude API -- get safe output.
    """
    config = getattr(coordinator, "config", None) or {}
    snapshot: dict[str, Any] = {
        "version": VERSION,
        "config_summary": {
            "climate_entity": config.get("climate_entity"),
            "fan_mode_archetype": config.get("fan_mode"),
            "fan_entity": config.get("fan_entity"),
            "fan_state_entity": config.get("fan_state_entity"),
        },
    }

    climate_entity_id = config.get("climate_entity")
    climate_state = hass.states.get(climate_entity_id) if climate_entity_id else None
    raw_attrs = dict(climate_state.attributes) if climate_state is not None else None
    snapshot["climate_entity"] = (
        {
            "entity_id": climate_entity_id,
            "state": climate_state.state,
            "attributes": redact_sensitive(raw_attrs),
            "capabilities": _build_capabilities(raw_attrs),
        }
        if climate_state is not None
        else {"entity_id": climate_entity_id, "state": None, "attributes": None, "capabilities": None}
    )
    snapshot["last_command"] = _build_last_command(
        coordinator,
        climate_state.state if climate_state is not None else None,
        config.get("temp_unit", "fahrenheit"),
    )

    fan_entity_id = config.get("fan_entity") or config.get("fan_state_entity")
    fan_state = hass.states.get(fan_entity_id) if fan_entity_id else None
    snapshot["fan_entity"] = (
        {
            "entity_id": fan_entity_id,
            "state": fan_state.state,
            "attributes": redact_sensitive(dict(fan_state.attributes)),
        }
        if fan_state is not None
        else None
    )

    return snapshot


def render_diagnostic_snapshot_text(snapshot: dict[str, Any]) -> str:
    """Render `build_diagnostic_snapshot()`'s output as the investigator report footer."""
    lines = ["=== DIAGNOSTIC SNAPSHOT ===", f"  climate_advisor_version: {snapshot.get('version')}"]

    cfg = snapshot.get("config_summary") or {}
    lines.append(
        "  config: climate_entity={} fan_mode={} fan_entity={} fan_state_entity={}".format(
            cfg.get("climate_entity"),
            cfg.get("fan_mode_archetype"),
            cfg.get("fan_entity"),
            cfg.get("fan_state_entity"),
        )
    )

    climate = snapshot.get("climate_entity") or {}
    lines.append(f"  climate_entity.state: {climate.get('state')}")
    attrs = climate.get("attributes")
    if attrs:
        for key in sorted(attrs):
            lines.append(f"    {key}: {attrs[key]!r}")
    else:
        lines.append("    (entity unavailable)")

    caps = climate.get("capabilities")
    if caps:
        lines.append("  thermostat_capabilities:")
        for key in sorted(caps):
            lines.append(f"    {key}: {caps[key]!r}")

    last = snapshot.get("last_command")
    if last:
        lines.append("  last_command (what Climate Advisor last asked the thermostat to do):")
        for key in sorted(last):
            lines.append(f"    {key}: {last[key]!r}")

    fan = snapshot.get("fan_entity")
    if fan is not None:
        lines.append(f"  fan_entity.state: {fan.get('state')}")
        fan_attrs = fan.get("attributes") or {}
        for key in sorted(fan_attrs):
            lines.append(f"    {key}: {fan_attrs[key]!r}")

    return "\n".join(lines) + "\n"


def render_diagnostic_snapshot_compact(snapshot: dict[str, Any]) -> str:
    """Render a single-line, human-readable diagnostic footer (Issue #973).

    Compact counterpart to `render_diagnostic_snapshot_text()` for surfaces the
    occupant reads directly (Activity Record) rather than the AI's raw context —
    picks the handful of attributes triage actually needs (hvac_mode, fan_mode,
    hvac_action, temperature_unit) instead of dumping every attribute the entity
    exposes.
    """
    parts = [f"CA v{snapshot.get('version')}"]

    climate = snapshot.get("climate_entity") or {}
    climate_id = climate.get("entity_id")
    if climate_id:
        attrs = climate.get("attributes") or {}
        detail = " ".join(
            f"{key}={attrs[key]}"
            for key in ("fan_mode", "hvac_action", "temperature_unit")
            if attrs.get(key) not in (None, "")
        )
        climate_part = f"{climate_id} hvac_mode={climate.get('state')}"
        if detail:
            climate_part += f" {detail}"
        parts.append(climate_part)
    else:
        parts.append("climate entity: not configured")

    fan = snapshot.get("fan_entity")
    if fan is not None:
        parts.append(f"{fan.get('entity_id')} state={fan.get('state')}")

    return "**System:** " + " · ".join(parts)
