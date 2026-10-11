"""Issue #1066: get_debug_state() reports the comfort family from the thermostat's real mode."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.climate_advisor.comfort_family_fsm import ComfortFamilyState

from .test_nat_vent_dashboard_target import _make_nat_vent_coord_stub


def _debug(family, flag, last_switch=None, raises=False):
    coord = _make_nat_vent_coord_stub(config={})
    ae = coord.automation_engine
    ae._comfort_mode_family = flag
    ae._last_comfort_switch = last_switch
    if raises:
        ae._current_thermostat_family = MagicMock(side_effect=RuntimeError("boom"))
    else:
        ae._current_thermostat_family = MagicMock(return_value=family)
    return coord.get_debug_state()


class TestComfortFamilyArmedDebug:
    def test_thermostat_heat_wins_over_stale_cooling_flag(self):
        state = _debug(ComfortFamilyState.HEATING, "cooling")
        assert state["comfort_family_armed"] == "heating"

    def test_thermostat_cool_wins_over_stale_heating_flag(self):
        state = _debug(ComfortFamilyState.COOLING, "heating")
        assert state["comfort_family_armed"] == "cooling"

    def test_unknown_thermostat_mode_is_none(self):
        assert _debug(None, "cooling")["comfort_family_armed"] is None

    def test_error_in_family_lookup_does_not_break_debug_view(self):
        assert _debug(None, "cooling", raises=True)["comfort_family_armed"] is None

    def test_last_switch_passthrough_and_none(self):
        sw = {"ts": "2026-10-11T10:00:00", "from": "cooling", "to": "heating", "reason": "breach"}
        state = _debug(ComfortFamilyState.HEATING, "heating", last_switch=sw)
        assert state["comfort_family_last_switch"] == sw
        assert _debug(None, None)["comfort_family_last_switch"] is None

    def test_last_switch_from_unknown_passes_through_json_safe(self):
        """Issue #1073: a switch from an unknown prior mode (from == "unknown") must not break the row."""
        import json
        from pathlib import Path

        sw = {"ts": "2026-10-11T10:00:00", "from": "unknown", "to": "heating", "reason": "breach"}
        state = _debug(ComfortFamilyState.HEATING, "heating", last_switch=sw)
        assert json.loads(json.dumps(state["comfort_family_last_switch"]))["from"] == "unknown"
        # The dashboard row stringifies and HTML-escapes from/to (with a '?' fallback), never indexes into them.
        html = (
            Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "frontend" / "index.html"
        ).read_text(encoding="utf-8")
        assert "escapeHtml(String(sw.from || '?'))" in html and "escapeHtml(String(sw.to || '?'))" in html


class TestComfortFamilyHoldDebug:
    """Issue #1073: get_debug_state() reports a plain-language settling-guard hold, null when none."""

    @staticmethod
    def _state(hold, family=None):
        coord = _make_nat_vent_coord_stub(config={})
        ae = coord.automation_engine
        ae._comfort_mode_family = None
        ae._last_comfort_switch = None
        ae._last_comfort_hold = hold
        ae._current_thermostat_family = MagicMock(return_value=family)
        return coord.get_debug_state()

    def test_no_hold_is_none(self):
        assert self._state(None)["comfort_family_hold"] is None

    def test_unset_attribute_on_partial_engine_is_none(self):
        # An un-configured MagicMock attribute is not a dict -> must not be rendered.
        coord = _make_nat_vent_coord_stub(config={})
        coord.automation_engine._current_thermostat_family = MagicMock(return_value=None)
        assert coord.get_debug_state()["comfort_family_hold"] is None

    def test_cooling_hold_text_and_until(self):
        hold = {
            "candidate_family": "cooling",
            "depth_f": 1.0,
            "tolerance_f": 2.0,
            "opposite_ran_min_ago": 12.0,
            "window_min": 120.0,
        }
        text = self._state(hold)["comfort_family_hold"]
        assert text.startswith("holding cooling: 1.0°F past ceiling, tolerance 2.0°F, heating ran 12 min ago (until ~")
        assert text.endswith(")")

    def test_heating_hold_mirror(self):
        hold = {
            "candidate_family": "heating",
            "depth_f": 1.5,
            "tolerance_f": 5.0,
            "opposite_ran_min_ago": 130.0,
            "window_min": 120.0,
        }
        text = self._state(hold)["comfort_family_hold"]
        assert text.startswith("holding heating: 1.5°F past floor, tolerance 5.0°F, cooling ran 130 min ago")

    def test_malformed_hold_is_none(self):
        assert self._state({"candidate_family": "cooling", "depth_f": "x"})["comfort_family_hold"] is None


class TestComfortFamilyHoldFrontendContract:
    """API-frontend contract: the Debug tab reads the exact key the coordinator emits, escaped, shown only when set."""

    def test_index_html_reads_key_escaped_and_conditionally(self):
        from pathlib import Path

        html = (
            Path(__file__).parent.parent / "custom_components" / "climate_advisor" / "frontend" / "index.html"
        ).read_text(encoding="utf-8")
        assert "data.comfort_family_hold ?" in html  # conditional row: hidden when null
        assert "escapeHtml(String(data.comfort_family_hold))" in html
        assert "Comfort Switch Hold" in html
