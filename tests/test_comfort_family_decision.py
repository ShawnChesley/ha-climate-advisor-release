"""Tests for the Design D nearest-edge comfort-family leaf (Issue #1066)."""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.climate_advisor.comfort_family_decision import (
    COMFORT_MIN_ARMING_MARGIN_F,
    COMFORT_MIN_USABLE_MARGIN_F,
    ComfortFamilyDecision,
    ComfortFamilyInputs,
    ComfortFamilyOutcome,
    _native_family,
    decide_comfort_family,
    effective_margin,
)

_NOW = datetime(2026, 8, 31, 12, 0, 0)
H = "heating"
C = "cooling"


def _decide(indoor=71.0, current=None, default=C, floor=68.0, ceiling=74.0, margin=2.0, **kw) -> ComfortFamilyDecision:
    params = {
        "current_family": current,
        "default_family": default,
        "indoor": indoor,
        "floor": floor,
        "ceiling": ceiling,
        "margin_f": margin,
        "natural_vent_active": False,
        "whf_owns_hvac": False,
        "now": _NOW,
    }
    params.update(kw)
    return decide_comfort_family(ComfortFamilyInputs(**params))


def _mirror(t: float, floor: float = 68.0, ceiling: float = 74.0) -> float:
    return floor + ceiling - t


def _flip(family: str | None) -> str | None:
    return {H: C, C: H, None: None}[family]


class TestEffectiveMargin:
    def test_constants(self):
        assert COMFORT_MIN_ARMING_MARGIN_F == 2.0
        assert COMFORT_MIN_USABLE_MARGIN_F == 1.0

    @pytest.mark.parametrize("configured", [5.0, 2.0])
    def test_wide_margin_clamped_to_one_third(self, configured):
        assert effective_margin(configured, 68.0, 74.0) == 2.0

    def test_width_six_is_two(self):
        assert effective_margin(2.0, 70.0, 76.0) == 2.0

    def test_small_margin_raised_to_minimum(self):
        assert effective_margin(0.5, 66.0, 76.0) == 2.0

    def test_narrow_band_none(self):
        assert effective_margin(5.0, 70.0, 72.0) is None

    def test_band_four_usable(self):
        assert effective_margin(5.0, 70.0, 74.0) == pytest.approx(4 / 3)

    def test_wide_band_uses_configured(self):
        assert effective_margin(3.0, 60.0, 80.0) == 3.0

    def test_wide_band_caps_at_third(self):
        assert effective_margin(9.0, 60.0, 80.0) == pytest.approx(20 / 3)


class TestArming:
    @pytest.mark.parametrize("indoor", [68.0, 69.0, 70.0])
    def test_heat_arm_inside_margin(self, indoor):
        d = _decide(indoor, current=C)
        assert d.outcome is ComfortFamilyOutcome.SWITCH
        assert d.target_family == H
        assert not d.breach

    @pytest.mark.parametrize("indoor", [72.0, 73.0, 74.0])
    def test_cool_arm_inside_margin(self, indoor):
        d = _decide(indoor, current=H)
        assert d.outcome is ComfortFamilyOutcome.SWITCH
        assert d.target_family == C
        assert not d.breach

    def test_inclusive_boundaries(self):
        assert _decide(70.0, current=C).target_family == H
        assert _decide(72.0, current=H).target_family == C

    def test_just_inside_gap(self):
        assert _decide(70.01, current=C).target_family == C
        assert _decide(71.99, current=H).target_family == H

    @pytest.mark.parametrize("current", [H, C])
    def test_strict_gap_keeps_current(self, current):
        for t in (70.5, 71.0, 71.5):
            d = _decide(t, current=current, default=_flip(current))
            assert d.outcome is ComfortFamilyOutcome.HOLD
            assert d.target_family == current

    @pytest.mark.parametrize("default", [H, C])
    def test_gap_unknown_uses_default(self, default):
        d = _decide(71.0, current=None, default=default)
        assert d.outcome is ComfortFamilyOutcome.HOLD
        assert d.target_family == default

    def test_unknown_current_is_never_switch(self):
        assert _decide(69.0, current=None).outcome is ComfortFamilyOutcome.HOLD
        assert _decide(66.0, current=None).outcome is ComfortFamilyOutcome.HOLD

    def test_hold_when_already_in_armed_family(self):
        assert _decide(69.0, current=H).outcome is ComfortFamilyOutcome.HOLD
        assert _decide(73.0, current=C).outcome is ComfortFamilyOutcome.HOLD

    def test_zone_limits_reported(self):
        d = _decide(71.0, current=H)
        assert d.margin_applied_f == 2.0
        assert d.heat_zone_limit_f == 70.0
        assert d.cool_zone_limit_f == 72.0
        assert d.reason


class TestBreach:
    def test_below_floor_heats_even_when_cooling(self):
        d = _decide(67.9, current=C)
        assert d.outcome is ComfortFamilyOutcome.SWITCH
        assert d.target_family == H
        assert d.breach

    def test_above_ceiling_cools_even_when_heating(self):
        d = _decide(74.1, current=H)
        assert d.outcome is ComfortFamilyOutcome.SWITCH
        assert d.target_family == C
        assert d.breach

    def test_at_edges_is_not_breach(self):
        assert not _decide(68.0, current=H).breach
        assert not _decide(74.0, current=C).breach

    def test_breach_hold_when_family_matches(self):
        d = _decide(60.0, current=H)
        assert d.outcome is ComfortFamilyOutcome.HOLD
        assert d.breach

    def test_breach_overrides_default(self):
        assert _decide(60.0, current=None, default=C).target_family == H
        assert _decide(80.0, current=None, default=H).target_family == C


class TestSymmetry:
    @pytest.mark.parametrize("t", [64.0, 67.9, 68.0, 69.0, 70.0, 70.5, 71.0, 71.5, 72.0, 73.0, 74.0, 74.1, 80.0])
    @pytest.mark.parametrize("current", [H, C, None])
    def test_reflected_cases_mirror(self, t, current):
        a = _decide(t, current=current, default=H)
        b = _decide(_mirror(t), current=_flip(current), default=C)
        assert b.target_family == _flip(a.target_family)
        assert b.breach == a.breach
        assert b.outcome == a.outcome


class TestNarrowBand:
    def test_margin_none_holds_current_inside(self):
        d = _decide(71.0, current=C, floor=70.0, ceiling=72.0)
        assert d.margin_applied_f is None
        assert d.heat_zone_limit_f is None
        assert d.outcome is ComfortFamilyOutcome.HOLD
        assert d.target_family == C
        assert "narrow" in d.reason

    def test_margin_none_unknown_uses_default(self):
        assert _decide(71.0, current=None, default=H, floor=70.0, ceiling=72.0).target_family == H

    def test_edges_do_not_flip_without_breach(self):
        assert _decide(70.0, current=C, floor=70.0, ceiling=72.0).target_family == C
        assert _decide(72.0, current=H, floor=70.0, ceiling=72.0).target_family == H

    def test_breach_still_switches(self):
        d = _decide(69.9, current=C, floor=70.0, ceiling=72.0)
        assert d.outcome is ComfortFamilyOutcome.SWITCH and d.target_family == H and d.breach
        d = _decide(72.1, current=H, floor=70.0, ceiling=72.0)
        assert d.outcome is ComfortFamilyOutcome.SWITCH and d.target_family == C and d.breach


class TestCapabilities:
    def test_heat_only(self):
        d = _decide(73.0, current=None, supports_cool=False)
        assert d.target_family == H
        d = _decide(73.0, current=C, supports_cool=False)
        assert d.outcome is ComfortFamilyOutcome.SWITCH and d.target_family == H

    def test_cool_only(self):
        d = _decide(66.0, current=None, supports_heat=False)
        assert d.target_family == C
        d = _decide(66.0, current=H, supports_heat=False)
        assert d.outcome is ComfortFamilyOutcome.SWITCH and d.target_family == C

    def test_neither(self):
        d = _decide(71.0, current=H, supports_heat=False, supports_cool=False)
        assert d.outcome is ComfortFamilyOutcome.NOT_APPLICABLE


class TestNotApplicable:
    def test_nat_vent(self):
        d = _decide(60.0, current=C, natural_vent_active=True)
        assert d.outcome is ComfortFamilyOutcome.NOT_APPLICABLE
        assert d.target_family == C

    def test_whf(self):
        d = _decide(80.0, current=None, whf_owns_hvac=True)
        assert d.outcome is ComfortFamilyOutcome.NOT_APPLICABLE
        assert d.target_family is None

    @pytest.mark.parametrize("field", ["indoor", "floor", "ceiling"])
    def test_missing_inputs(self, field):
        kw = {"indoor": 71.0}
        kw[field] = None
        d = _decide(current=H, **kw)
        assert d.outcome is ComfortFamilyOutcome.NOT_APPLICABLE


class TestFlickerAndWalk:
    def test_one_degree_flicker_never_cools(self):
        current = H
        for t in (70, 71, 70, 71):
            d = _decide(float(t), current=current)
            assert d.target_family == H
            assert d.outcome is ComfortFamilyOutcome.HOLD
            current = d.target_family

    def test_gap_edge_no_chatter_until_72(self):
        current = H
        for t in (72 - 1, 71, 71, 71):
            current = _decide(float(t), current=current).target_family
            assert current == H
        current = _decide(72.0, current=current).target_family
        assert current == C
        # Back down to 71 stays cooling (hysteresis), heat only at <= 70.
        assert _decide(71.0, current=C).target_family == C
        assert _decide(70.0, current=C).target_family == H

    def test_mixed_day_walk(self):
        temps = [66, 68, 70, 71, 72, 73, 74, 75, 73, 72, 71, 70, 69, 67]
        expected = [H, H, H, H, C, C, C, C, C, C, C, H, H, H]
        current = None
        got = []
        for t in temps:
            d = _decide(float(t), current=current)
            current = d.target_family
            got.append(current)
        assert got == expected


class TestNativeFamilyForStallDetector:
    """``_native_family`` is retained for the comfort-stall detector's per-direction margins."""

    def test_hot_and_warm_favor_cooling(self):
        assert _native_family("hot") == "cooling"
        assert _native_family("warm") == "cooling"

    def test_cool_and_cold_favor_heating(self):
        assert _native_family("cool") == "heating"
        assert _native_family("cold") == "heating"

    def test_mild_and_unknown_have_no_native_family(self):
        assert _native_family("mild") is None
        assert _native_family(None) is None
