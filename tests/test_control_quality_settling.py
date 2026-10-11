"""Issue #1073 (S7): settling-guard block of the control-quality report.

Pure-function tests on ``build_control_quality(comfort_holds=...)`` plus an integration test through the
real ``build_diagnostic_snapshot``. Hold fixtures are the engine's ``comfort_holds`` record shape.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.climate_advisor.control_quality import build_control_quality
from custom_components.climate_advisor.diagnostic_snapshot import (
    build_diagnostic_snapshot,
    render_diagnostic_snapshot_text,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def ts(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def hold(start_ago, end_ago=None, reason=None, depth=1.0, conf="none", family="heating", tol=2.0):
    return {
        "start_ts": ts(start_ago),
        "end_ts": ts(end_ago) if end_ago is not None else None,
        "end_reason": reason,
        "candidate_family": family,
        "max_depth_f": depth,
        "tolerance_f": tol,
        "model_confidence": conf,
    }


def change(ago, to_mode="heat", reason="selector"):
    return {
        "ts": ts(ago),
        "from_mode": "cool",
        "to_mode": to_mode,
        "indoor_f": 67.0,
        "floor_f": 68.0,
        "ceiling_f": 74.0,
        "margin_f": 2.0,
        "reason": reason,
    }


def sg(holds=None, changes=(), window="24h"):
    r = build_control_quality(now=NOW, mode_changes=list(changes), chart_entries=[], comfort_holds=holds)
    return r["windows"][window]["settling_guard"]


class TestEpisodesAndOutcomes:
    def test_mixed_outcomes_and_aggregates(self):
        holds = [
            hold(300, 270, "recovered", depth=0.5, conf="none"),  # 30 min
            hold(200, 170, "switched", depth=2.0, conf="low"),  # 30 min
            hold(120, 60, "lapsed", depth=1.5, conf="high"),  # 60 min
            hold(20, None, None, depth=0.8, conf="medium"),  # open, 20 min so far
            hold(30, 10, "recovered", depth=0.2, conf=None),  # 20 min, unknown confidence
        ]
        g = sg(holds)
        assert g["hold_episodes"] == 5
        assert g["outcomes"] == {"recovered": 2, "switched": 1, "lapsed": 1, "open": 1}
        assert g["recovered_share"] == 0.5  # 2 of 4 completed
        assert g["median_hold_minutes"] == 30.0  # completed: 30, 30, 60, 20
        assert g["hold_minutes_total"] == 160.0  # 30+30+60+20+20
        assert g["max_depth_f"] == 2.0
        assert g["holds_by_model_confidence"] == {"none": 1, "low": 1, "medium": 1, "high": 1, "unknown": 1}

    def test_open_episode_only_has_no_recovered_share_or_median(self):
        g = sg([hold(15, None, None)])
        assert g["hold_episodes"] == 1
        assert g["outcomes"]["open"] == 1
        assert g["recovered_share"] is None
        assert g["median_hold_minutes"] is None
        assert g["hold_minutes_total"] == 15.0

    def test_live_engine_with_no_holds_is_a_real_zero(self):
        g = sg([], changes=[change(10)])
        assert g["hold_episodes"] == 0
        assert g["hold_minutes_total"] == 0.0
        assert g["recovered_share"] is None
        assert g["median_hold_minutes"] is None
        assert g["max_depth_f"] is None

    def test_both_directions_are_counted_identically(self):
        heat = sg([hold(60, 30, "recovered", family="heating")])
        cool = sg([hold(60, 30, "recovered", family="cooling")])
        assert heat == cool

    def test_window_slicing_24h_vs_7d(self):
        old = hold(60 * 30, 60 * 29, "lapsed")  # 30 h ago: outside 24h, inside 7d
        recent = hold(60, 30, "recovered")
        assert sg([old, recent], window="24h")["hold_episodes"] == 1
        assert sg([old, recent], window="7d")["hold_episodes"] == 2

    def test_episode_straddling_window_start_is_clipped_in_minutes(self):
        g = sg([hold(24 * 60 + 30, 24 * 60 - 30, "lapsed")])  # 60 min long, 30 of them inside 24h
        assert g["hold_episodes"] == 1
        assert g["hold_minutes_total"] == 30.0
        assert g["median_hold_minutes"] == 60.0  # completed duration is unclipped

    def test_closed_with_unknown_reason_is_skipped(self):
        g = sg([hold(60, 30, "bogus"), hold(60, 30, "recovered")])
        assert g["hold_episodes"] == 1


class TestNullNotZero:
    def test_no_data_source_at_all_is_none(self):
        assert sg(None, changes=[]) is None

    def test_holds_absent_but_ring_buffer_present_nulls_hold_fields_only(self):
        g = sg(None, changes=[change(10, reason="restore")])
        assert g["hold_episodes"] is None
        assert g["outcomes"] is None
        assert g["holds_by_model_confidence"] is None
        assert g["mode_writes"]["by_reason"] == {"restore": 1}

    def test_non_list_holds_treated_as_absent(self):
        r = build_control_quality(now=NOW, mode_changes=[], chart_entries=[], comfort_holds="nope")  # type: ignore[arg-type]
        assert r["windows"]["24h"]["settling_guard"] is None

    def test_malformed_hold_records_are_ignored_not_raised(self):
        g = sg([None, "x", {"start_ts": "garbage"}, {"end_ts": ts(5)}, hold(60, 30, "recovered")])
        assert g["hold_episodes"] == 1


class TestModeWrites:
    def test_counts_by_reason_within_window(self):
        changes = [change(10, reason="selector"), change(20, reason="selector"), change(30, reason="user")]
        changes.append(change(60 * 30, reason="tou"))  # outside 24h
        assert sg([], changes)["mode_writes"]["by_reason"] == {"selector": 2, "user": 1}
        assert sg([], changes, window="7d")["mode_writes"]["by_reason"] == {"selector": 2, "tou": 1, "user": 1}

    def test_free_text_reason_is_bucketed_as_other(self):
        g = sg([], [change(10, reason="Door at climate.hallway closed - restoring")])
        assert g["mode_writes"]["by_reason"] == {"other": 1}

    def test_missing_reason_counts_as_unknown(self):
        rec = change(10)
        del rec["reason"]
        assert sg([], [rec])["mode_writes"]["by_reason"] == {"unknown": 1}

    def test_empty_ring_buffer_gives_null_not_empty_dict(self):
        assert sg([hold(60, 30, "recovered")], changes=[])["mode_writes"]["by_reason"] is None


class TestRedaction:
    def test_block_is_json_and_identifier_free(self):
        holds = [hold(60, 30, "recovered", conf="low")]
        holds[0]["entity_id"] = "climate.secret"  # stray keys on the engine record must not leak
        g = sg(holds, [change(10, reason="selector")])
        blob = json.dumps(g)
        assert "climate." not in blob and "secret" not in blob
        assert not re.search(r"entity|friendly|token", blob, re.I)
        assert set(g) == {
            "hold_episodes",
            "hold_minutes_total",
            "median_hold_minutes",
            "outcomes",
            "recovered_share",
            "max_depth_f",
            "holds_by_model_confidence",
            "mode_writes",
        }


def _live_iso(minutes_ago):
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _coordinator(engine):
    log = MagicMock()
    log.get_entries.return_value = []
    return SimpleNamespace(
        config={"climate_entity": "climate.t", "temp_unit": "fahrenheit"},
        automation_engine=engine,
        _event_log=[],
        _chart_log=log,
        _indoor_temp_history=[],
    )


def _hass():
    hass = MagicMock()
    hass.states.get.return_value = None
    return hass


class TestSnapshotIntegration:
    def test_real_builder_includes_settling_guard_and_text_line(self):
        holds = [
            {
                "start_ts": _live_iso(90),
                "end_ts": _live_iso(60),
                "end_reason": "recovered",
                "candidate_family": "heating",
                "max_depth_f": 1.2,
                "tolerance_f": 2.0,
                "model_confidence": "low",
            }
        ]
        changes = [dict(change(0), ts=_live_iso(100), reason="selector")]
        engine = SimpleNamespace(comfort_mode_changes=changes, comfort_holds=holds)
        snap = build_diagnostic_snapshot(_hass(), _coordinator(engine))
        g = snap["control_quality"]["windows"]["24h"]["settling_guard"]
        assert g["hold_episodes"] == 1
        assert g["recovered_share"] == 1.0
        assert g["mode_writes"]["by_reason"] == {"selector": 1}
        json.dumps(snap["control_quality"])
        text = render_diagnostic_snapshot_text(snap)
        assert "settling_guard (24h / 7d): holds=1 / 1" in text

    def test_engine_without_hold_attribute_still_builds(self):
        engine = SimpleNamespace(comfort_mode_changes=[dict(change(0), ts=_live_iso(5))])
        snap = build_diagnostic_snapshot(_hass(), _coordinator(engine))
        g = snap["control_quality"]["windows"]["24h"]["settling_guard"]
        assert g["hold_episodes"] is None
        assert g["mode_writes"]["by_reason"] == {"selector": 1}

    def test_broken_hold_data_never_breaks_download(self):
        engine = SimpleNamespace(comfort_mode_changes=[], comfort_holds=[{"start_ts": 5}, object()])
        snap = build_diagnostic_snapshot(_hass(), _coordinator(engine))
        assert snap["version"]
        assert snap["control_quality"] is not None

    def test_text_line_shows_none_when_no_engine_data(self):
        coord = _coordinator(None)
        coord._chart_log.get_entries.return_value = []
        text = render_diagnostic_snapshot_text(build_diagnostic_snapshot(_hass(), coord))
        assert "settling_guard (24h / 7d): holds=None / None" in text
