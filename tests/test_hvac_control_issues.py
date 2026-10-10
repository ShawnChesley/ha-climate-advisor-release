"""Status-tab ``hvac_control_issues`` list (Issue #1049, change 7).

Occupant: Status read "active" while the thermostat ignored the requested mode and the house
stayed cold. The coordinator now lists open mode-alignment / command-failure episodes next to
``invariant_violations``; the entry disappears when the episode recovers.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

_PKG = Path(__file__).parent.parent / "custom_components" / "climate_advisor"


def _coord(*, episode_reason=None, mismatch_since=None, fail_announced=None, fail_since=None, live="off"):
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = MagicMock()
    ae = MagicMock()
    ae.climate_entity = "climate.test"
    ae._hvac_write_record = SimpleNamespace(episode_reason=episode_reason, mismatch_since=mismatch_since)
    ae._hvac_cmd_fail_announced = fail_announced
    ae._hvac_cmd_fail_since = fail_since
    coord.automation_engine = ae
    coord.hass.states.get.return_value = SimpleNamespace(state=live, attributes={})
    coord._compute_hvac_control_issues = types.MethodType(
        mod.ClimateAdvisorCoordinator._compute_hvac_control_issues, coord
    )
    return coord


def _issues(coord, now):
    with patch("custom_components.climate_advisor.coordinator.dt_util") as dt:
        dt.now.return_value = now
        return coord._compute_hvac_control_issues()


class TestComputeHvacControlIssues:
    def test_mode_not_obeyed_listed_with_diagnostic_detail(self):
        now = datetime.now(UTC)
        out = _issues(_coord(episode_reason="heat", mismatch_since=now - timedelta(minutes=12)), now)
        assert [i["issue"] for i in out] == ["mode_not_obeyed"]
        d = out[0]["detail"]
        assert "heat" in d and "'off'" in d and "12 min" in d

    def test_clears_when_episode_recovers(self):
        now = datetime.now(UTC)
        assert _issues(_coord(episode_reason=None), now) == []

    def test_command_failing_listed(self):
        now = datetime.now(UTC)
        out = _issues(_coord(fail_announced="cool", fail_since=now - timedelta(minutes=5)), now)
        assert [i["issue"] for i in out] == ["command_failing"]
        assert "cool" in out[0]["detail"] and "5 min" in out[0]["detail"]

    def test_both_episodes_listed(self):
        now = datetime.now(UTC)
        out = _issues(_coord(episode_reason="heat", mismatch_since=now, fail_announced="heat", fail_since=now), now)
        assert {i["issue"] for i in out} == {"mode_not_obeyed", "command_failing"}

    def test_unconfigured_mock_engine_yields_nothing(self):
        """A bare MagicMock engine (unset attributes are truthy) must not fabricate issues."""
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        coord = MagicMock()
        coord.automation_engine = MagicMock()
        coord._compute_hvac_control_issues = types.MethodType(
            mod.ClimateAdvisorCoordinator._compute_hvac_control_issues, coord
        )
        assert _issues(coord, datetime.now(UTC)) == []

    def test_detail_is_not_an_instruction(self):
        now = datetime.now(UTC)
        d = _issues(_coord(episode_reason="heat", mismatch_since=now), now)[0]["detail"].lower()
        for word in ("please", "you should", "turn ", "check "):
            assert word not in d


class TestStatusPathWiring:
    def test_not_a_string_in_automation_status(self):
        src = (_PKG / "coordinator.py").read_text(encoding="utf-8")
        body = src.split("def _compute_automation_status", 1)[1].split("\n    def ", 1)[0]
        assert "hvac_control_issues" not in body and "_compute_hvac_control_issues" not in body

    def test_coordinator_data_exposes_key(self):
        src = (_PKG / "coordinator.py").read_text(encoding="utf-8")
        assert '"hvac_control_issues": _hvac_control_issues' in src

    def test_frontend_reads_exact_key_with_single_renderer(self):
        html = (_PKG / "frontend" / "index.html").read_text(encoding="utf-8")
        assert html.count("data.hvac_control_issues") == 1
        assert '(data.hvac_control_issues || []).map(v => `<div class="value"' in html
        assert "escapeHtml(v.detail)" in html
