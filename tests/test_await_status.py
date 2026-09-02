"""I-6 — AWAIT_STATUS cases cannot enter ATTEMPTING without a resolved status poll.

Razorpay's own documentation notes that pending transactions may authorise late, and
that a deemed transaction's outcome is not known to the acquirer until the following
day. Retrying one of those double-charges the customer. A naive retry loop has no
concept of this state, which makes it the single most demonstrable safety failure in
the whole system.

Five codes carry it: payment_pending, deemed_transaction, record_not_found,
verification_failed, capture_failed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.executor import state as st
from tests.conftest import NOW, open_case

AWAIT_STATUS_CODES = (
    "payment_pending",
    "deemed_transaction",
    "record_not_found",
    "verification_failed",
    "capture_failed",
)


def _open_pending(client: TestClient, code: str = "payment_pending") -> dict:
    return open_case(client, error_code=code, payment_id=f"pay_{code}")


# -- diagnosis ----------------------------------------------------------------


@pytest.mark.parametrize("code", AWAIT_STATUS_CODES)
def test_await_status_codes_land_in_awaiting_status(
    client: TestClient, code: str
) -> None:
    case = _open_pending(client, code)
    assert case["status"] == st.AWAITING_STATUS
    assert case["status_resolved_at"] is None


@pytest.mark.parametrize("code", AWAIT_STATUS_CODES)
def test_await_status_never_schedules_a_retry(client: TestClient, code: str) -> None:
    # I-4: scheduled_at is None for all five non-retrying classes.
    case = _open_pending(client, code)
    assert case["next_attempt_at"] is None
    assert case["decision"]["scheduled_at"] is None
    assert case["decision"]["action"] == "AWAIT_STATUS"
    assert case["decision"]["advice"] == "poll_status_first"


# -- the block ----------------------------------------------------------------


def test_attempt_on_an_unresolved_case_returns_423(client: TestClient) -> None:
    case = _open_pending(client)
    response = client.post(
        f"/v1/recovery/cases/{case['id']}/attempts",
        json={"now": NOW + 3600},
        headers={"Idempotency-Key": "await-1"},
    )
    assert response.status_code == 423, response.text
    error = response.json()["error"]
    assert error["code"] == "AWAITING_STATUS"
    assert error["reason"] == "prior_outcome_unresolved"


def test_a_blocked_attempt_writes_no_attempt_row(client: TestClient) -> None:
    case = _open_pending(client)
    client.post(
        f"/v1/recovery/cases/{case['id']}/attempts",
        json={"now": NOW + 3600},
        headers={"Idempotency-Key": "await-2"},
    )
    detail = client.get(f"/v1/recovery/cases/{case['id']}").json()
    assert detail["attempts"] == []
    assert detail["status"] == st.AWAITING_STATUS


def test_the_state_graph_has_no_direct_edge_to_attempting() -> None:
    # Structural: even a caller who bypasses the runner cannot make this move.
    assert st.ATTEMPTING not in st.LEGAL_TRANSITIONS[st.AWAITING_STATUS]


# -- resolution ---------------------------------------------------------------


def test_poll_resolving_as_failed_unblocks_the_case(client: TestClient) -> None:
    case = _open_pending(client)
    polled = client.post(
        f"/v1/recovery/cases/{case['id']}/status-poll",
        json={"now": NOW + 7200, "resolution": "failed"},
    )
    assert polled.status_code == 200, polled.text
    body = polled.json()
    assert body["status"] == st.SCHEDULED
    assert body["status_resolved_at"] == NOW + 7200
    assert body["next_attempt_at"] is not None


def test_an_attempt_is_allowed_once_the_poll_has_resolved(client: TestClient) -> None:
    case = _open_pending(client)
    polled = client.post(
        f"/v1/recovery/cases/{case['id']}/status-poll",
        json={"now": NOW + 7200, "resolution": "failed"},
    ).json()

    response = client.post(
        f"/v1/recovery/cases/{case['id']}/attempts",
        json={"now": polled["next_attempt_at"]},
        headers={"Idempotency-Key": "await-3"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["attempt"]["attempt_number"] == 1


def test_poll_resolving_as_succeeded_recovers_the_case(client: TestClient) -> None:
    # Late authorisation. There is nothing to recover and nothing to retry.
    case = _open_pending(client)
    body = client.post(
        f"/v1/recovery/cases/{case['id']}/status-poll",
        json={"now": NOW + 7200, "resolution": "succeeded"},
    ).json()
    assert body["status"] == st.RECOVERED
    assert body["recovered_at"] == NOW + 7200
    assert body["recovered_amount_paise"] == body["amount_paise"]
    assert body["attempts"] == []


def test_polling_a_case_that_is_not_awaiting_status_is_422(client: TestClient) -> None:
    case = open_case(client, error_code="insufficient_funds", payment_id="pay_NOTPEND")
    response = client.post(
        f"/v1/recovery/cases/{case['id']}/status-poll",
        json={"now": NOW, "resolution": "failed"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["reason"] == "case_not_awaiting_status"


def test_an_unusable_resolution_is_400(client: TestClient) -> None:
    case = _open_pending(client)
    response = client.post(
        f"/v1/recovery/cases/{case['id']}/status-poll",
        json={"now": NOW, "resolution": "maybe"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["reason"] == "invalid_query_param"


def test_the_audit_trail_records_the_poll(client: TestClient) -> None:
    case = _open_pending(client)
    client.post(
        f"/v1/recovery/cases/{case['id']}/status-poll",
        json={"now": NOW + 7200, "resolution": "failed"},
    )
    audit = client.get(f"/v1/recovery/cases/{case['id']}").json()["audit"]
    poll_rows = [row for row in audit if row["actor"] == "status_poll"]
    assert len(poll_rows) == 1
    assert poll_rows[0]["from_state"] == st.AWAITING_STATUS
    assert poll_rows[0]["to_state"] == st.SCHEDULED


# -- through the runner directly ----------------------------------------------


def test_runner_raises_before_touching_the_world(conn, runner, world) -> None:
    """The refusal happens in check_bounds, before any resolver is consulted."""
    from src.executor.runner import AwaitingStatus
    from src.store import db

    payment = db.Payment(
        id="pay_AWAIT",
        customer_id="cust_AWAIT",
        merchant_id="mch_AWAIT",
        method="upi",
        rail="@oksbi",
        amount_paise=499000,
        created_at=NOW,
        first_outcome="failed",
        first_error_code="payment_pending",
    )
    db.insert_payments(conn, [payment])
    case = runner.build_case(
        payment=payment,
        error_code="payment_pending",
        error_source="bank",
        failed_at=NOW,
    )
    st.open_case(conn, case, now=NOW)
    case = runner.diagnose(conn, case, now=NOW)
    assert case.state == st.AWAITING_STATUS

    class Exploding:
        def attempt(self, *args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("the world was consulted on a blocked case")

    with pytest.raises(AwaitingStatus):
        runner.execute_attempt(conn, case, Exploding(), now=NOW + 3600)
