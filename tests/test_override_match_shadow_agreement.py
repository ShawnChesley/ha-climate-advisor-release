"""Issue #1073 (S6): the override/grace FSM's match input agrees with production on a warm day.

Occupant framing: on a warm day the classification mode is "off" while the comfort-edge selector arms
cooling on the ceiling. The occupant who cools by hand to the ceiling must have that override ADOPTED
(no pointless grace period). The FSM mirror used to compare against classification.hvac_mode and the
flat band, so it disagreed with production exactly there.

Real ``AutomationEngine``; the pure ``decide_override_matches_decision`` is called as-is.
"""

from __future__ import annotations

from unittest.mock import patch

from custom_components.climate_advisor.override_match import decide_override_matches_decision

from .test_mode_write_events import _AUTOMATION, _classification, _Clock, _make_engine


def _shadow_match(engine, inputs) -> bool:
    return decide_override_matches_decision(
        manual_override_active=inputs.manual_override_active,
        manual_override_mode=inputs.manual_override_mode,
        manual_override_source=inputs.manual_override_source,
        classification_mode=inputs.decision_mode if inputs.decision_mode_resolved else inputs.classification_mode,
        current_setpoint_f=inputs.current_setpoint_f,
        target_setpoint_f=inputs.target_setpoint_f,
        tolerance_f=inputs.tolerance_f,
    )


def _scene(setpoint: float):
    engine = _make_engine(indoor=76.0, mode="cool")
    engine._test_state.attributes["temperature"] = setpoint
    c = _classification("warm")  # classification.hvac_mode == "off"
    engine._current_classification = c
    engine._manual_override_active = True
    engine._manual_override_mode = "cool"
    engine._manual_override_source = "normal"
    return engine, c


def test_warm_day_cool_override_at_ceiling_is_adopted_by_production_and_shadow():
    engine, c = _scene(74.0)
    assert c.hvac_mode == "off"
    with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock()):
        production = engine._override_matches_current_decision(c)
        inputs = engine._build_override_grace_fsm_inputs()
    assert inputs.classification_mode == "off", "the confirm-path input is unchanged"
    assert inputs.decision_mode == "cool" and inputs.target_setpoint_f == 74.0
    assert production is True
    assert _shadow_match(engine, inputs) is production


def test_warm_day_cool_override_at_a_different_setpoint_is_not_adopted_by_either():
    engine, c = _scene(70.0)
    with patch(f"{_AUTOMATION}.dt_util.now", side_effect=_Clock()):
        production = engine._override_matches_current_decision(c)
        inputs = engine._build_override_grace_fsm_inputs()
    assert production is False
    assert _shadow_match(engine, inputs) is production
