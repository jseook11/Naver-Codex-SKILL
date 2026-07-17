from __future__ import annotations

from dataclasses import replace
import signal
import time

import pytest

from naver_place.contracts import (
    BudgetExceeded,
    CapabilityError,
    CapabilityResult,
    Completeness,
    ErrorCode,
    RequestBudget,
    RequestPolicy,
    Status,
)
from naver_place.serializers import V1Serializer


def test_request_budget_tracks_requests_and_time() -> None:
    now = [10.0]
    budget = RequestBudget(max_requests=2, max_elapsed_seconds=5, _clock=lambda: now[0])
    budget.consume(2)
    assert budget.snapshot() == {
        "requests_used": 2,
        "request_limit": 2,
        "elapsed_seconds": 0.0,
        "elapsed_limit_seconds": 5,
    }
    with pytest.raises(BudgetExceeded) as request_error:
        budget.consume()
    assert request_error.value.code is ErrorCode.REQUEST_BUDGET_EXHAUSTED

    now[0] = 15.0
    with pytest.raises(BudgetExceeded) as time_error:
        budget.check()
    assert time_error.value.code is ErrorCode.TIME_BUDGET_EXHAUSTED


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="Unix hard deadline")
def test_request_budget_interrupts_blocking_work_at_hard_deadline() -> None:
    budget = RequestBudget(max_requests=1, max_elapsed_seconds=0.02)
    with pytest.raises(BudgetExceeded) as caught:
        with budget.deadline():
            time.sleep(0.2)
    assert caught.value.code is ErrorCode.TIME_BUDGET_EXHAUSTED


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        (ErrorCode.INVALID_INPUT, 2),
        (ErrorCode.DEPENDENCY_MISSING, 3),
        (ErrorCode.UPSTREAM_REJECTED, 10),
        (ErrorCode.UPSTREAM_CHANGED, 11),
        (ErrorCode.NOT_FOUND, 12),
        (ErrorCode.INTERNAL_ERROR, 1),
    ],
)
def test_typed_error_exit_codes(code: ErrorCode, exit_code: int) -> None:
    result = CapabilityResult(
        capability="test",
        request={},
        data={},
        status=Status.ERROR,
        errors=(CapabilityError(code=code, message="failed"),),
        completeness=Completeness(complete=False, stop_reason=str(code)),
    )
    assert result.exit_code == exit_code


def test_partial_result_exits_zero_and_preserves_data() -> None:
    result = CapabilityResult(
        capability="place.detail",
        request={"place": "1"},
        data={"place_id": "1", "base": {"name": "Example"}},
        status=Status.PARTIAL,
        errors=(
            CapabilityError(
                code=ErrorCode.UPSTREAM_REJECTED,
                message="hours rejected",
                operation="place.hours_api",
                http_status=405,
            ),
        ),
        completeness=Completeness(complete=False, stop_reason="upstream_rejected"),
    )
    envelope = V1Serializer().serialize(result)
    assert result.exit_code == 0
    assert envelope["status"] == "partial"
    assert envelope["data"]["base"]["name"] == "Example"
    assert envelope["errors"][0]["code"] == "upstream_rejected"


def test_standard_detail_view_omits_large_media_and_descriptions() -> None:
    result = CapabilityResult(
        capability="place.detail",
        request={},
        data={
            "place_id": "1",
            "base": {"name": "Example", "description": "long"},
            "photos": {"place_images": [{"url": "https://example.test/image"}]},
            "menus": [{"name": "Menu", "price": 1000, "description": "long", "images": []}],
        },
    )
    standard = V1Serializer(view="standard").serialize(result)["data"]
    full = V1Serializer(view="full").serialize(result)["data"]
    assert "description" not in standard["base"]
    assert "photos" not in standard
    assert "description" not in standard["menus"][0]
    assert full["photos"]


def test_error_result_requires_typed_explanation() -> None:
    with pytest.raises(ValueError, match="typed error"):
        CapabilityResult(capability="test", request={}, data={}, status=Status.ERROR)


def test_detail_error_serialization_does_not_invent_empty_data_sections() -> None:
    result = CapabilityResult(
        capability="place.detail",
        request={},
        data={},
        status=Status.ERROR,
        errors=(CapabilityError(code=ErrorCode.BLOCKED, message="blocked"),),
        completeness=Completeness(complete=False, stop_reason="blocked"),
    )

    assert V1Serializer().serialize(result)["data"] == {}


@pytest.mark.parametrize("value", (True, 1.5, "2"))
def test_request_budget_requires_an_integer_request_limit(value) -> None:
    with pytest.raises(ValueError, match="integer"):
        RequestBudget(max_requests=value)


@pytest.mark.parametrize("value", (True, 1.5, "2"))
def test_request_policy_requires_an_integer_attempt_limit(value) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RequestPolicy(max_attempts=value)


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_request_budget_rejects_nonfinite_elapsed_limits(value: float) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        RequestBudget(max_elapsed_seconds=value)
