"""The two arms, checked against what each one is supposed to be.

Control's whole value is that it is dumb. If it quietly starts consulting the policy
table — through a helpful default, an inherited method, or the executor re-routing its
outcomes — the measured gap shrinks and nobody notices, because the run still produces
numbers. Several tests here exist only to catch that.

Baseline's job is the opposite: for every one of the eight classes, do the thing the
table says, and never schedule a retry on a class that must not have one. (I-4)
"""

from __future__ import annotations

import pytest

from src.arms.base import ArmDecision, CaseSnapshot
from src.arms.baseline import BaselineArm
from src.arms.control import RETRY_INTERVAL_HOURS, ControlArm
from src.executor import state as st
from src.executor.runner import ALTERNATIVE_RAIL, RETRY_NOW_FLOOR_SECONDS
from src.policy.engine import ACTIONS, MODEL_ELIGIBLE_ACTIONS, RETRYING_ACTIONS
from tests.conftest import NOW

HOUR = 3600

# One representative code per action class.
CODE_FOR_ACTION = {
    "RETRY_NOW": "request_timed_out",
    "RETRY_SCHEDULED": "insufficient_funds",
    "SWITCH_RAIL": "bank_technical_error",
    "SWITCH_INSTRUMENT": "card_expired",
    "NUDGE_CUSTOMER": "incorrect_pin",
    "AWAIT_STATUS": "payment_pending",
    "STOP": "payment_risk_check_failed",
    "MERCHANT_ALERT": "invalid_amount",
}


class NoHealth:
    """A rail-health feed reporting nothing degraded."""

    def severity_at(self, method, at_ts, instruments=()):
        return None


class AllDown:
    """Everything is down, everywhere."""

    def severity_at(self, method, at_ts, instruments=()):
        return "high"


class Exploding:
    """Any read means the arm consulted rail health when it should not have."""

    def severity_at(self, method, at_ts, instruments=()):
        raise AssertionError("the control arm read rail health")


def snapshot(
    code: str,
    *,
    state: str = st.RECEIVED,
    attempt_count: int = 0,
    last_attempt_at: int | None = None,
    method: str = "upi",
    status_resolved_at: int | None = None,
) -> CaseSnapshot:
    return CaseSnapshot(
        id="case_ARM",
        error_code=code,
        error_source="bank",
        method=method,
        rail="@oksbi",
        amount_paise=499000,
        failed_at=NOW,
        state=state,
        max_attempts=4,
        drop_dead_at=NOW + 7 * 86400,
        next_attempt_at=None,
        status_resolved_at=status_resolved_at,
        nudge_sent_at=None,
        attempt_count=attempt_count,
        last_attempt_at=last_attempt_at,
        city_tier=2,
        vpa_handle="@oksbi",
        payer_bank="SBIN",
    )


# -- control: deliberately dumb -----------------------------------------------


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_control_gives_the_same_answer_for_every_class(engine, action) -> None:
    arm = ControlArm()
    decision = arm.next_action(
        snapshot(CODE_FOR_ACTION[action]), engine, NoHealth(), NOW
    )
    assert decision is not None
    assert decision.action == "RETRY_SCHEDULED"
    assert decision.target_rail is None


def test_control_never_reads_the_policy_table(engine) -> None:
    """Passing an engine that raises on any lookup proves it is never consulted."""

    class Exploding_engine:
        def resolve(self, code):  # pragma: no cover - must never run
            raise AssertionError("the control arm consulted the policy table")

    arm = ControlArm()
    for code in CODE_FOR_ACTION.values():
        assert arm.next_action(snapshot(code), Exploding_engine(), NoHealth(), NOW)


def test_control_never_reads_rail_health(engine) -> None:
    arm = ControlArm()
    for code in CODE_FOR_ACTION.values():
        assert arm.next_action(snapshot(code), engine, Exploding(), NOW)


def test_control_schedules_twenty_four_hours_out(engine) -> None:
    decision = ControlArm().next_action(
        snapshot("insufficient_funds"), engine, NoHealth(), NOW
    )
    assert decision is not None
    assert decision.scheduled_at == NOW + RETRY_INTERVAL_HOURS * HOUR


def test_control_spaces_later_retries_from_the_last_attempt(engine) -> None:
    last = NOW + 30 * HOUR
    decision = ControlArm().next_action(
        snapshot("insufficient_funds", attempt_count=1, last_attempt_at=last),
        engine,
        NoHealth(),
        NOW,
    )
    assert decision is not None
    assert decision.scheduled_at == last + RETRY_INTERVAL_HOURS * HOUR


def test_control_stops_after_three_attempts(engine) -> None:
    arm = ControlArm()
    assert arm.next_action(
        snapshot("insufficient_funds", attempt_count=2), engine, NoHealth(), NOW
    ).action == "RETRY_SCHEDULED"
    stopped = arm.next_action(
        snapshot("insufficient_funds", attempt_count=3), engine, NoHealth(), NOW
    )
    assert stopped is not None and stopped.action == "STOP"


def test_control_never_nudges(engine) -> None:
    arm = ControlArm()
    for code in CODE_FOR_ACTION.values():
        for attempts in range(0, 4):
            decision = arm.next_action(
                snapshot(code, attempt_count=attempts), engine, NoHealth(), NOW
            )
            assert decision is None or decision.action != "NUDGE_CUSTOMER"


def test_control_never_switches_rail(engine) -> None:
    arm = ControlArm()
    for code in CODE_FOR_ACTION.values():
        decision = arm.next_action(snapshot(code), engine, NoHealth(), NOW)
        assert decision is not None and decision.action != "SWITCH_RAIL"


def test_control_outcomes_are_not_re_routed_by_policy(engine) -> None:
    # If this flag ever flips, the executor re-diagnoses control's failures against
    # the policy table and control silently becomes the baseline.
    decision = ControlArm().next_action(
        snapshot("card_expired"), engine, NoHealth(), NOW
    )
    assert decision is not None and decision.policy_routed is False


# -- baseline: the policy table, acted on --------------------------------------


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_baseline_produces_the_class_the_table_names(engine, action) -> None:
    decision = BaselineArm().next_action(
        snapshot(CODE_FOR_ACTION[action]), engine, NoHealth(), NOW
    )
    assert decision is not None
    assert decision.action == action


def test_baseline_waits_the_tables_min_wait_hours(engine) -> None:
    decision = BaselineArm().next_action(
        snapshot("insufficient_funds"), engine, NoHealth(), NOW
    )
    entry = engine.resolve("insufficient_funds")
    assert decision is not None
    assert decision.scheduled_at == NOW + entry.min_wait_hours * HOUR
    assert entry.min_wait_hours == 72


def test_baseline_uses_the_sub_hour_floor_for_retry_now(engine) -> None:
    decision = BaselineArm().next_action(
        snapshot("request_timed_out"), engine, NoHealth(), NOW
    )
    assert decision is not None
    assert decision.scheduled_at == NOW + RETRY_NOW_FLOOR_SECONDS


def test_baseline_switches_to_the_alternate_rail(engine) -> None:
    decision = BaselineArm().next_action(
        snapshot("bank_technical_error", method="upi"), engine, NoHealth(), NOW
    )
    assert decision is not None
    assert decision.action == "SWITCH_RAIL"
    assert decision.target_rail == ALTERNATIVE_RAIL["upi"]
    assert decision.scheduled_at == NOW + RETRY_NOW_FLOOR_SECONDS


def test_baseline_waits_rather_than_switching_into_a_second_outage(engine) -> None:
    # research/02 §2.4: high severity means suppress retries on that rail. Switching
    # onto a rail that is itself down spends an attempt to land in the same place.
    decision = BaselineArm().next_action(
        snapshot("bank_technical_error", method="upi"), engine, AllDown(), NOW
    )
    assert decision is not None
    assert decision.reason_code == "both_rails_degraded_waiting"
    assert decision.scheduled_at is not None and decision.scheduled_at > NOW + HOUR


def test_baseline_stops_on_the_three_terminal_classes(engine) -> None:
    arm = BaselineArm()
    expected = {
        "card_expired": "instrument_permanently_unusable",
        "payment_risk_check_failed": "retry_unsafe_or_penalised",
        "invalid_amount": "merchant_configuration_defect",
    }
    for code, reason in expected.items():
        decision = arm.next_action(snapshot(code), engine, NoHealth(), NOW)
        assert decision is not None
        assert decision.reason_code == reason
        assert decision.scheduled_at is None


def test_baseline_nudges_once_then_waits(engine) -> None:
    arm = BaselineArm()
    first = arm.next_action(snapshot("incorrect_pin"), engine, NoHealth(), NOW)
    assert first is not None and first.action == "NUDGE_CUSTOMER"
    # Already escalated: the arm keeps polling the outstanding nudge, and never
    # schedules anything.
    waiting = arm.next_action(
        snapshot("incorrect_pin", state=st.ESCALATED), engine, NoHealth(), NOW
    )
    assert waiting is not None and waiting.scheduled_at is None
    # Any other state means the runner has already moved on.
    assert arm.next_action(
        snapshot("incorrect_pin", state=st.RECOVERED), engine, NoHealth(), NOW
    ) is None


def test_baseline_polls_an_unresolved_status(engine) -> None:
    decision = BaselineArm().next_action(
        snapshot("payment_pending"), engine, NoHealth(), NOW
    )
    assert decision is not None and decision.action == "AWAIT_STATUS"


def test_baseline_stops_polling_once_resolved(engine) -> None:
    assert BaselineArm().next_action(
        snapshot(
            "payment_pending", state=st.SCHEDULED, status_resolved_at=NOW
        ),
        engine,
        NoHealth(),
        NOW,
    ) is None


def test_baseline_raises_on_an_unknown_code(engine) -> None:
    # I-2. No default branch anywhere in the decision path, arms included.
    from src.policy.engine import UnknownErrorCodeError

    with pytest.raises(UnknownErrorCodeError):
        BaselineArm().next_action(snapshot("not_a_code"), engine, NoHealth(), NOW)


# -- I-4, across every code in the table --------------------------------------


def test_neither_arm_schedules_a_retry_on_a_non_retrying_class(engine, entries) -> None:
    """The invariant, checked over all 110 codes rather than a sample.

    Control is exempt by construction — it is the naive arm and retrying doomed codes
    is the behaviour being measured. Baseline is not exempt, and this is where I-4
    lives for the arms.
    """
    arm = BaselineArm()
    offenders = []
    for entry in entries:
        decision = arm.next_action(snapshot(entry.code), engine, NoHealth(), NOW)
        if decision is None:
            continue
        if entry.action not in RETRYING_ACTIONS and decision.scheduled_at is not None:
            offenders.append((entry.code, entry.action, decision.scheduled_at))
    assert offenders == []


def test_baseline_only_ever_names_the_tables_own_class(engine, entries) -> None:
    arm = BaselineArm()
    for entry in entries:
        decision = arm.next_action(snapshot(entry.code), engine, NoHealth(), NOW)
        assert decision is not None
        assert decision.action == entry.action, entry.code


def test_no_arm_consults_a_model(engine) -> None:
    # Stage 3 has no machine learning in it at all. The model-eligible classes exist
    # in the table but nothing ranks within them yet — that gap is Stage 4's result.
    for action in MODEL_ELIGIBLE_ACTIONS:
        decision = BaselineArm().next_action(
            snapshot(CODE_FOR_ACTION[action]), engine, NoHealth(), NOW
        )
        assert decision is not None
        assert isinstance(decision, ArmDecision)


def test_arms_do_not_import_the_simulator() -> None:
    # Same guarantee as tests/test_hidden_state.py, asserted here too because it is
    # the arms that would most plausibly reach for the world. (I-12)
    import ast
    from pathlib import Path

    arms_dir = Path(__file__).resolve().parents[1] / "src" / "arms"
    for path in sorted(arms_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = (
                node.module
                if isinstance(node, ast.ImportFrom)
                else None
            )
            names = (
                [a.name for a in node.names] if isinstance(node, ast.Import) else []
            )
            for candidate in filter(None, [module, *names]):
                assert not candidate.startswith("src.simulator"), (
                    f"{path.name} imports {candidate}"
                )
