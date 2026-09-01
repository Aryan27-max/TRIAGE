"""Endpoint smoke tests for the read-only policy surface.

The two failure shapes matter as much as the success ones: an unknown code on a
lookup is a 404 NOT_FOUND_ERROR, an unusable query parameter is a 400
BAD_REQUEST_ERROR with reason `invalid_query_param`. They are different failures and
must not be reported as the same thing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.policy.engine import ACTIONS

TOTAL_CODES = 110
RECOVERABLE_CODES = 27


def test_app_refuses_to_boot_on_a_broken_table(
    raw_policy: dict[str, Any], tmp_path: Path
) -> None:
    # A table that fails validation is a startup failure, not a wrong decision
    # discovered three stages later.
    doc = json.loads(json.dumps(raw_policy))
    doc["codes"][0]["action"] = "UNMAPPED"
    broken = tmp_path / "broken_policy.json"
    broken.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(RuntimeError):
        with TestClient(create_app(broken)):
            pass


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["policy_codes_loaded"] == TOTAL_CODES
    assert body["policy_version"] == "1.0"


def test_openapi_renders(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


# -- listing ------------------------------------------------------------------


def test_list_returns_all_codes(client: TestClient) -> None:
    body = client.get("/v1/errors").json()
    assert body["entity"] == "collection"
    assert body["count"] == TOTAL_CODES
    assert len(body["items"]) == TOTAL_CODES


def test_filter_by_family(client: TestClient) -> None:
    body = client.get("/v1/errors", params={"family": "B"}).json()
    assert body["count"] == 28
    assert {item["family"] for item in body["items"]} == {"B"}


def test_filter_by_action(client: TestClient) -> None:
    body = client.get("/v1/errors", params={"action": "SWITCH_RAIL"}).json()
    assert body["count"] == 15
    assert {item["action"] for item in body["items"]} == {"SWITCH_RAIL"}


def test_filter_by_recoverable(client: TestClient) -> None:
    body = client.get("/v1/errors", params={"recoverable": "true"}).json()
    assert body["count"] == RECOVERABLE_CODES
    assert all(item["recoverable"] for item in body["items"])


def test_filters_compose(client: TestClient) -> None:
    body = client.get(
        "/v1/errors", params={"family": "B", "action": "SWITCH_RAIL"}
    ).json()
    assert body["count"] > 0
    assert all(
        item["family"] == "B" and item["action"] == "SWITCH_RAIL"
        for item in body["items"]
    )


# -- single lookup ------------------------------------------------------------


def test_lookup_insufficient_funds(client: TestClient) -> None:
    response = client.get("/v1/errors/insufficient_funds")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "insufficient_funds"
    assert body["action"] == "RETRY_SCHEDULED"
    assert body["min_wait_hours"] == 72
    assert body["recoverable"] is True
    assert body["is_model_eligible"] is True
    assert body["razorpay_explanation"]
    assert body["razorpay_next_steps"]


def test_lookup_unrecoverable_code(client: TestClient) -> None:
    body = client.get("/v1/errors/card_expired").json()
    assert body["action"] == "SWITCH_INSTRUMENT"
    assert body["recoverable"] is False
    assert body["is_retrying"] is False
    assert body["min_wait_hours"] == 0


# -- 404: no such code --------------------------------------------------------


def test_unknown_code_is_404_not_found(client: TestClient) -> None:
    response = client.get("/v1/errors/fake")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND_ERROR"
    assert error["reason"] == "error_code_not_found"
    assert error["field"] == "code"
    assert error["source"] == "business"
    assert error["step"] == "error_lookup"
    assert "fake" in error["description"]


@pytest.mark.parametrize(
    "near_miss", ["INSUFFICIENT_FUNDS", "insufficient-funds", "insufficient_fund"]
)
def test_near_miss_codes_are_404_over_http(client: TestClient, near_miss: str) -> None:
    # No fuzzy or case-insensitive matching at the API boundary either. (I-2)
    assert client.get(f"/v1/errors/{near_miss}").status_code == 404


# -- 400: unusable query parameter --------------------------------------------


def test_invalid_action_is_400_invalid_query_param(client: TestClient) -> None:
    response = client.get("/v1/errors", params={"action": "RETRY_MAYBE"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "BAD_REQUEST_ERROR"
    assert error["reason"] == "invalid_query_param"
    assert error["field"] == "action"


def test_invalid_family_is_400(client: TestClient) -> None:
    error = client.get("/v1/errors", params={"family": "Z"}).json()["error"]
    assert error["code"] == "BAD_REQUEST_ERROR"
    assert error["reason"] == "invalid_query_param"
    assert error["field"] == "family"


def test_invalid_recoverable_is_400(client: TestClient) -> None:
    response = client.get("/v1/errors", params={"recoverable": "maybe"})
    assert response.status_code == 400
    assert response.json()["error"]["field"] == "recoverable"


def test_404_and_400_do_not_share_a_shape(client: TestClient) -> None:
    not_found = client.get("/v1/errors/fake").json()["error"]
    bad_request = client.get(
        "/v1/errors", params={"action": "RETRY_MAYBE"}
    ).json()["error"]
    assert not_found["code"] != bad_request["code"]
    assert not_found["reason"] != bad_request["reason"]


def test_every_error_body_carries_the_full_envelope(client: TestClient) -> None:
    expected = {"code", "description", "field", "source", "step", "reason"}
    for response in (
        client.get("/v1/errors/fake"),
        client.get("/v1/errors", params={"action": "nope"}),
    ):
        assert set(response.json()["error"]) == expected


# -- meta ---------------------------------------------------------------------


def test_meta_actions_lists_all_eight(client: TestClient) -> None:
    body = client.get("/v1/errors/meta/actions").json()
    assert body["count"] == 8
    assert [item["action"] for item in body["items"]] == list(ACTIONS)
    assert all(item["description"] for item in body["items"])
    assert sum(item["code_count"] for item in body["items"]) == TOTAL_CODES


def test_meta_actions_marks_model_scope(client: TestClient) -> None:
    items = client.get("/v1/errors/meta/actions").json()["items"]
    eligible = {i["action"] for i in items if i["model_eligible"]}
    assert eligible == {"RETRY_SCHEDULED", "SWITCH_RAIL"}


def test_meta_coverage_reports_27_of_110(client: TestClient) -> None:
    body = client.get("/v1/errors/meta/coverage").json()
    assert body["total_codes"] == TOTAL_CODES
    assert body["recoverable_codes"] == RECOVERABLE_CODES
    assert body["unrecoverable_codes"] == TOTAL_CODES - RECOVERABLE_CODES
    assert body["policy_version"] == "1.0"
    assert "27 of 110" in body["headline"]


def test_meta_coverage_breakdowns_are_complete(client: TestClient) -> None:
    body = client.get("/v1/errors/meta/coverage").json()
    assert [row["action"] for row in body["by_action"]] == list(ACTIONS)
    assert [row["family"] for row in body["by_family"]] == ["A", "B", "S", "X"]
    assert sum(row["count"] for row in body["by_family"]) == TOTAL_CODES
    assert (
        sum(row["recoverable_count"] for row in body["by_family"])
        == RECOVERABLE_CODES
    )


def test_meta_routes_are_not_shadowed_by_the_code_lookup(client: TestClient) -> None:
    # /v1/errors/{code} must not swallow /v1/errors/meta/*.
    assert client.get("/v1/errors/meta/actions").status_code == 200
    assert client.get("/v1/errors/meta/coverage").status_code == 200
    assert client.get("/v1/errors/meta").status_code == 404
