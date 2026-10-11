"""Tests for the Design D comfort-family FSM (Issue #1066): min-dwell anti-chatter
that never blocks a breach. Leaf math is covered in test_comfort_family_decision.py."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

import pytest

from custom_components.climate_advisor.comfort_family_decision import ComfortFamilyOutcome
from custom_components.climate_advisor.comfort_family_fsm import (
    ComfortFamilyDwellState,
    ComfortFamilyEvent,
    ComfortFamilyEventKind,
    ComfortFamilyFsmInputs,
    ComfortFamilyState,
    transition,
)

_NOW = datetime(2026, 8, 31, 12, 0, 0)
_MIN_DWELL_S = 600.0
H = ComfortFamilyState.HEATING
C = ComfortFamilyState.COOLING


def _event(indoor=71.0, **overrides) -> ComfortFamilyEvent:
    params = {
        "default_family": "cooling",
        "indoor": indoor,
        "floor": 68.0,
        "ceiling": 74.0,
        "margin_f": 2.0,
        "natural_vent_active": False,
        "whf_owns_hvac": False,
        "now": _NOW,
        "min_dwell_seconds": _MIN_DWELL_S,
    }
    params.update(overrides)
    return ComfortFamilyEvent(kind=ComfortFamilyEventKind.TICK, inputs=ComfortFamilyFsmInputs(**params))


class TestHold:
    def test_hold_leaves_dwell_unchanged(self):
        dwell = ComfortFamilyDwellState(dwell_since=_NOW - timedelta(seconds=30))
        t = transition(H, _event(71.0), dwell)
        assert t.decision.outcome is ComfortFamilyOutcome.HOLD
        assert t.to_state == H and not t.changed and not t.locked_out
        assert t.dwell_state == dwell

    def test_default_dwell_state(self):
        t = transition(H, _event(69.0))
        assert t.dwell_state.dwell_since is None
        assert t.at == _NOW


class TestSwitchDwell:
    def test_first_switch_allowed(self):
        t = transition(H, _event(73.0))
        assert t.changed and t.to_state == C and not t.locked_out
        assert t.dwell_state.dwell_since == _NOW

    def test_second_nonbreach_switch_inside_window_locked(self):
        dwell = ComfortFamilyDwellState(dwell_since=_NOW - timedelta(seconds=599))
        t = transition(H, _event(73.0), dwell)
        assert t.locked_out
        assert t.to_state == t.from_state == H
        assert not t.changed
        assert t.dwell_state == dwell
        assert t.decision.target_family == "cooling"

    def test_switch_after_window_allowed_and_advances(self):
        dwell = ComfortFamilyDwellState(dwell_since=_NOW - timedelta(seconds=600))
        t = transition(H, _event(73.0), dwell)
        assert t.changed and t.to_state == C and not t.locked_out
        assert t.dwell_state.dwell_since == _NOW

    def test_breach_inside_window_proceeds(self):
        dwell = ComfortFamilyDwellState(dwell_since=_NOW - timedelta(seconds=5))
        t = transition(C, _event(66.0), dwell)
        assert t.decision.breach
        assert t.changed and t.to_state == H and not t.locked_out
        assert t.dwell_state.dwell_since == _NOW

    def test_ceiling_breach_inside_window_proceeds(self):
        dwell = ComfortFamilyDwellState(dwell_since=_NOW - timedelta(seconds=5))
        t = transition(H, _event(76.0), dwell)
        assert t.to_state == C and not t.locked_out


class TestUnknownCurrent:
    def test_none_in_gap_adopts_default(self):
        t = transition(None, _event(71.0, default_family="heating"))
        assert t.from_state is None
        assert t.to_state == H
        assert t.dwell_state.dwell_since is None

    def test_none_with_arm_adopts_target_without_dwell(self):
        t = transition(None, _event(69.0))
        assert t.decision.outcome is ComfortFamilyOutcome.HOLD
        assert t.to_state == H
        assert t.dwell_state.dwell_since is None

    def test_none_breach_adopts_target(self):
        t = transition(None, _event(80.0))
        assert t.to_state == C and t.decision.breach


class TestNotApplicable:
    def test_never_moves_dwell(self):
        dwell = ComfortFamilyDwellState(dwell_since=_NOW - timedelta(seconds=10))
        t = transition(C, _event(60.0, natural_vent_active=True), dwell)
        assert t.decision.outcome is ComfortFamilyOutcome.NOT_APPLICABLE
        assert t.to_state == C and not t.locked_out
        assert t.dwell_state == dwell

    def test_unknown_stays_unknown(self):
        t = transition(None, _event(None))
        assert t.decision.outcome is ComfortFamilyOutcome.NOT_APPLICABLE
        assert t.from_state is None and t.to_state is None
        assert not t.changed

    def test_whf(self):
        t = transition(H, _event(80.0, whf_owns_hvac=True))
        assert t.to_state == H


class TestCapabilityPassThrough:
    def test_heat_only_switches_to_heat(self):
        t = transition(C, _event(72.0, supports_cool=False))
        assert t.to_state == H and t.changed


class TestImmutability:
    def test_dwell_frozen(self):
        d = ComfortFamilyDwellState()
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.dwell_since = _NOW  # type: ignore[misc]

    def test_inputs_and_transition_frozen(self):
        ev = _event()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ev.inputs.indoor = 1.0  # type: ignore[misc]
        t = transition(H, ev)
        with pytest.raises(dataclasses.FrozenInstanceError):
            t.locked_out = True  # type: ignore[misc]
