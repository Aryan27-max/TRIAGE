"""I-1 — the policy table decides the action class. The model never does.

The model is consulted for `RETRY_SCHEDULED` and `SWITCH_RAIL` only. For the other six
classes the treatment arm *is* the baseline arm — it delegates rather than
reimplementing, so the two cannot drift apart as either changes.

The consequence that matters: a model error can never cause an unrecoverable failure to
be retried, because the table forbids it before the model is reachable. Safety is
structural, not learned.

Asserted across **all 110 codes**, not a sample. A scorer that raises on contact proves
the model is not merely ignored on those codes but never called at all.
"""

from __future__ import annotations

import pytest

from src.arms.baseline import BaselineArm
from src.arms.treatment import TreatmentArm
from src.policy.engine import (
    ACTIONS,
    MODEL_ELIGIBLE_ACTIONS,
    RETRYING_ACTIONS,
)
from tests.conftest import NOW
from tests.test_arms import CODE_FOR_ACTION, NoHealth, snapshot


class ExplodingScorer:
    """Any call means the model was consulted where policy had already decided."""

    feature_names: list[str] = []

    def score_batch(self, rows):  # pragma: no cover - must never run
        raise AssertionError("the model was consulted outside MODEL_ELIGIBLE_ACTIONS")

    def score(self, row):  # pragma: no cover - must never run
        raise AssertionError("the model was consulted outside MODEL_ELIGIBLE_ACTIONS")


class ConstantScorer:
    """Returns the same probability for every candidate."""

    def __init__(self, value: float = 0.5) -> None:
        self.value = value
        self.calls = 0

    def score_batch(self, rows):
        self.calls += 1
        return [self.value] * len(rows)


def _treatment(scorer, conn=None) -> TreatmentArm:
    arm = TreatmentArm(scorer)
    if conn is not None:
        arm.bind(conn)
    return arm


# -- the eligible set is exactly two ------------------------------------------


def test_model_eligible_actions_are_exactly_two() -> None:
    assert MODEL_ELIGIBLE_ACTIONS == {"RETRY_SCHEDULED", "SWITCH_RAIL"}


def test_model_eligible_is_a_strict_subset_of_retrying() -> None:
    # The model can only ever rank *within* a class that already permits a retry.
    assert MODEL_ELIGIBLE_ACTIONS < RETRYING_ACTIONS


def test_six_of_the_eight_classes_are_out_of_scope() -> None:
    assert len(set(ACTIONS) - MODEL_ELIGIBLE_ACTIONS) == 6


def test_the_eligible_surface_is_bounded_and_known(engine, entries) -> None:
    """24 of 110. This bounds the achievable uplift before anything is scored."""
    eligible = [e for e in entries if e.action in MODEL_ELIGIBLE_ACTIONS]
    assert len(eligible) == 24
    assert len(entries) == 110


# -- treatment == baseline everywhere the model is not permitted ---------------


@pytest.mark.parametrize(
    "action", sorted(set(ACTIONS) - MODEL_ELIGIBLE_ACTIONS)
)
def test_treatment_matches_baseline_on_ineligible_classes(engine, action) -> None:
    code = CODE_FOR_ACTION[action]
    case = snapshot(code)
    baseline = BaselineArm().next_action(case, engine, NoHealth(), NOW)
    treatment = _treatment(ExplodingScorer()).next_action(
        case, engine, NoHealth(), NOW
    )
    assert treatment == baseline


def test_treatment_matches_baseline_on_every_ineligible_code(engine, entries) -> None:
    """All 110 codes, not one per class.

    The scorer raises on contact, so this also proves the model is never *called* on
    those codes rather than merely being ignored.
    """
    baseline = BaselineArm()
    treatment = _treatment(ExplodingScorer())
    checked = 0
    for entry in entries:
        if entry.action in MODEL_ELIGIBLE_ACTIONS:
            continue
        case = snapshot(entry.code)
        assert treatment.next_action(case, engine, NoHealth(), NOW) == baseline.next_action(
            case, engine, NoHealth(), NOW
        ), entry.code
        checked += 1
    assert checked == 110 - 24


def test_treatment_matches_baseline_across_case_states(engine) -> None:
    """Delegation must hold in every state, not just at the start of a case."""
    from src.executor import state as st

    baseline = BaselineArm()
    treatment = _treatment(ExplodingScorer())
    for code in ("card_expired", "incorrect_pin", "payment_pending", "invalid_amount"):
        for state in (st.RECEIVED, st.ESCALATED, st.AWAITING_STATUS, st.SCHEDULED):
            case = snapshot(code, state=state)
            assert treatment.next_action(
                case, engine, NoHealth(), NOW
            ) == baseline.next_action(case, engine, NoHealth(), NOW), (code, state)


# -- the model never widens the permission set --------------------------------


def test_the_model_never_changes_the_action_class(conn, runner, engine) -> None:
    """Whatever the model says, the class that comes back is the table's class.

    Unlike the delegation tests above this one really does reach the model, so it
    needs a case in the store for the feature builder to read.
    """
    import dataclasses

    from tests.test_no_leakage import seed_case

    scorer = ConstantScorer(0.99)
    for code in ("insufficient_funds", "bank_technical_error"):
        case_row = seed_case(conn, runner, case_id_source=code, code=code)
        arm = _treatment(scorer, conn)
        case = dataclasses.replace(snapshot(code), id=case_row.id)
        decision = arm.next_action(case, engine, NoHealth(), NOW)
        assert decision is not None
        expected = engine.resolve(code).action
        # Either the permitted class, or a STOP — never a different action class.
        assert decision.action in (expected, "STOP"), (code, decision.action)
    assert scorer.calls > 0, "the model was never consulted on an eligible code"


def test_a_confident_model_cannot_resurrect_an_unrecoverable_code(engine) -> None:
    """The safety claim, stated directly.

    A scorer returning 1.0 for everything still cannot make `card_expired` retry,
    because the class is decided before the scorer is reachable.
    """
    arm = _treatment(ConstantScorer(1.0))
    decision = arm.next_action(snapshot("card_expired"), engine, NoHealth(), NOW)
    assert decision is not None
    assert decision.action == "SWITCH_INSTRUMENT"
    assert decision.scheduled_at is None
    assert arm.scorer.calls == 0


@pytest.mark.parametrize(
    "code",
    ["card_expired", "incorrect_pin", "payment_pending", "payment_risk_check_failed",
     "invalid_amount"],
)
def test_treatment_never_schedules_a_retry_on_a_non_retrying_class(engine, code) -> None:
    # I-4 holds for the treatment arm exactly as it does for baseline.
    decision = _treatment(ConstantScorer(1.0)).next_action(
        snapshot(code), engine, NoHealth(), NOW
    )
    assert decision is not None
    assert decision.scheduled_at is None


def test_treatment_delegates_rather_than_reimplementing() -> None:
    """The delegation is an object, not copied logic, so the two cannot diverge."""
    arm = _treatment(ExplodingScorer())
    assert isinstance(arm.baseline, BaselineArm)


def test_treatment_raises_on_an_unknown_code(engine) -> None:
    # I-2 survives the model: no default branch anywhere in the decision path.
    from src.policy.engine import UnknownErrorCodeError

    with pytest.raises(UnknownErrorCodeError):
        _treatment(ConstantScorer()).next_action(
            snapshot("not_a_real_code"), engine, NoHealth(), NOW
        )
