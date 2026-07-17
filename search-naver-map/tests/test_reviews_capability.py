from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

import naver_place.capabilities.reviews as reviews_module
from naver_place.capabilities.reviews import get_reviews
from naver_place.contracts import ErrorCode, RequestBudget, Status


FIXTURES = Path(__file__).parent / "fixtures" / "reviews"


def _code(value):
    return getattr(value, "value", value)


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response.url = "https://api.place.naver.com/graphql"
    return requests.HTTPError(f"HTTP {status}", response=response)


def test_raw_replay_preserves_order_and_uses_standard_privacy_view():
    expected = json.loads((FIXTURES / "expected-standard.json").read_text(encoding="utf-8"))

    result = get_reviews(
        "1234567890",
        limit=3,
        raw_dir=FIXTURES,
        request_delay=0,
        fetched_at="2026-07-13T00:00:00+00:00",
    )

    assert result.status == Status.OK
    assert result.data["place_id"] == expected["place_id"]
    assert [review["id"] for review in result.data["reviews"]] == expected["review_ids"]
    assert [review["has_owner_reply"] for review in result.data["reviews"]] == expected["owner_reply_flags"]
    assert result.completeness.complete is True
    assert result.completeness.pages_fetched == 2
    assert result.budget.requests_used == 0
    assert all(provenance.live is False for provenance in result.provenance)
    for review in result.data["reviews"]:
        assert "reviewer_id" not in review
        assert "receipt_info_url" not in review
        assert "cursor" not in review


def test_full_view_retains_legacy_extended_fields_only_when_requested():
    result = get_reviews(
        "1234567890",
        limit=1,
        raw_dir=FIXTURES,
        request_delay=0,
        view="full",
    )

    review = result.data["reviews"][0]
    assert review["reviewer_id"] == "private-object-id-1"
    assert review["receipt_info_url"].endswith("private-receipt-1")
    assert review["cursor"] == "cursor-001"


def test_owner_reply_filter_is_a_local_view_not_an_upstream_failure():
    result = get_reviews(
        "1234567890",
        limit=3,
        owner_reply="only_replied",
        raw_dir=FIXTURES,
        request_delay=0,
    )

    assert result.status == Status.OK
    assert [review["id"] for review in result.data["reviews"]] == ["review-002"]
    assert result.completeness.complete is True


def test_owner_reply_filter_paginates_until_filtered_limit_is_satisfied():
    result = get_reviews(
        "1234567890",
        limit=1,
        owner_reply="only_replied",
        raw_dir=FIXTURES,
        request_delay=0,
    )

    assert result.status == Status.OK
    assert [review["id"] for review in result.data["reviews"]] == ["review-002"]
    assert result.completeness.complete is True


def test_later_page_429_returns_typed_partial_data():
    first_page = json.loads((FIXTURES / "page-001.json").read_text(encoding="utf-8"))
    calls = 0

    def fetch_page(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return first_page
        raise _http_error(429)

    result = get_reviews(
        "1234567890",
        limit=3,
        page_fetcher=fetch_page,
        request_delay=0,
    )

    assert result.status == Status.PARTIAL
    assert [review["id"] for review in result.data["reviews"]] == ["review-001", "review-002"]
    assert _code(result.errors[0].code) == ErrorCode.RATE_LIMITED.value
    assert result.completeness.stop_reason == "rate_limited"
    assert result.completeness.complete is False
    assert result.budget.requests_used == 2


def test_initial_405_is_upstream_rejected_error_not_empty():
    def rejected(**_kwargs):
        raise _http_error(405)

    result = get_reviews(
        "1234567890",
        page_fetcher=rejected,
        request_delay=0,
    )

    assert result.status == Status.ERROR
    assert result.data["reviews"] == []
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_REJECTED.value
    assert result.errors[0].http_status == 405


def test_duplicate_cursor_is_schema_partial_instead_of_infinite_loop():
    first_page = json.loads((FIXTURES / "page-001.json").read_text(encoding="utf-8"))

    result = get_reviews(
        "1234567890",
        limit=3,
        pages=[first_page, first_page],
        request_delay=0,
    )

    assert result.status == Status.PARTIAL
    assert len(result.data["reviews"]) == 2
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value
    assert result.completeness.pages_fetched == 2


def test_request_budget_exhaustion_preserves_first_page():
    first_page = json.loads((FIXTURES / "page-001.json").read_text(encoding="utf-8"))

    result = get_reviews(
        "1234567890",
        limit=3,
        page_fetcher=lambda **_kwargs: first_page,
        request_budget=1,
        request_delay=1.5,
        sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )

    assert result.status == Status.PARTIAL
    assert len(result.data["reviews"]) == 2
    assert _code(result.errors[0].code) == ErrorCode.REQUEST_BUDGET_EXHAUSTED.value
    assert result.budget.requests_used == 1


def test_empty_valid_page_is_empty_not_error():
    result = get_reviews(
        "1234567890",
        pages=[{"total": 0, "items": []}],
        request_delay=0,
    )

    assert result.status == Status.EMPTY
    assert result.errors == ()
    assert result.completeness.stop_reason == "exhausted"


def test_malformed_graphql_root_is_upstream_changed():
    result = get_reviews(
        "1234567890",
        pages=[{"data": {"unexpected": {}}}],
        request_delay=0,
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value


def test_elapsed_budget_stops_before_another_page_and_keeps_data():
    first_page = json.loads((FIXTURES / "page-001.json").read_text(encoding="utf-8"))
    second_page = json.loads((FIXTURES / "page-002.json").read_text(encoding="utf-8"))
    now = [0.0]
    budget = RequestBudget(max_requests=10, max_elapsed_seconds=1, _clock=lambda: now[0])
    page_iter = iter((first_page, second_page))

    result = get_reviews(
        "1234567890",
        limit=3,
        page_fetcher=lambda **_kwargs: next(page_iter),
        request_delay=0.5,
        budget=budget,
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds * 2),
    )

    assert result.status == Status.PARTIAL
    assert len(result.data["reviews"]) == 2
    assert _code(result.errors[0].code) == ErrorCode.TIME_BUDGET_EXHAUSTED.value


def test_page_delay_does_not_sleep_past_elapsed_budget():
    first_page = json.loads((FIXTURES / "page-001.json").read_text(encoding="utf-8"))
    budget = RequestBudget(max_requests=10, max_elapsed_seconds=1, _clock=lambda: 0.0)

    result = get_reviews(
        "1234567890",
        limit=3,
        page_fetcher=lambda **_kwargs: first_page,
        request_delay=2,
        budget=budget,
        sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )

    assert result.status == Status.PARTIAL
    assert len(result.data["reviews"]) == 2
    assert _code(result.errors[0].code) == ErrorCode.TIME_BUDGET_EXHAUSTED.value


def test_slow_review_response_finishing_after_deadline_is_not_success():
    first_page = json.loads((FIXTURES / "page-001.json").read_text(encoding="utf-8"))
    now = [0.0]
    seen_timeout = []
    budget = RequestBudget(max_requests=2, max_elapsed_seconds=1, _clock=lambda: now[0])

    def slow_page(**kwargs):
        seen_timeout.append(kwargs["timeout"])
        now[0] = 2.0
        return first_page

    result = get_reviews(
        "1234567890",
        limit=2,
        page_fetcher=slow_page,
        request_delay=0,
        budget=budget,
        timeout=30,
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.TIME_BUDGET_EXHAUSTED.value
    assert seen_timeout[0] <= 0.5


def test_invalid_input_is_typed_without_fetching():
    result = get_reviews("not-a-place", owner_reply="sometimes")

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.INVALID_INPUT.value


def test_missing_cursor_before_reported_total_is_typed_partial() -> None:
    result = get_reviews(
        "1234567890",
        limit=2,
        pages=[{"total": 2, "items": [{"id": "review-1", "body": "one"}]}],
        request_delay=0,
    )

    assert result.status == Status.PARTIAL
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value
    assert result.completeness.complete is False


def test_full_page_without_total_or_cursor_is_not_claimed_exhaustive() -> None:
    result = get_reviews(
        "1234567890",
        limit=20,
        page_size=10,
        pages=[
            {
                "items": [
                    {"id": f"review-{index}", "body": "text", "cursor": ""}
                    for index in range(10)
                ]
            }
        ],
        request_delay=0,
    )

    assert result.status == Status.PARTIAL
    assert len(result.data["reviews"]) == 10
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value
    assert result.completeness.complete is False


def test_expired_budget_wins_over_fixture_diagnostics(tmp_path: Path) -> None:
    now = [2.0]
    budget = RequestBudget(
        max_requests=1,
        max_elapsed_seconds=1,
        _clock=lambda: now[0],
    )
    budget._started_at = 0.0

    result = get_reviews("1234567890", raw_dir=tmp_path / "private", budget=budget)

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.TIME_BUDGET_EXHAUSTED.value
    assert "private" not in result.errors[0].message


def test_authenticated_review_headers_are_invalid_without_fetching() -> None:
    called = False

    def fetch(**_kwargs):
        nonlocal called
        called = True
        return {"total": 0, "items": []}

    result = get_reviews(
        "1234567890",
        headers={"Cookie": "secret"},
        page_fetcher=fetch,
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.INVALID_INPUT.value
    assert called is False


def test_review_fixture_provenance_uses_capture_date_not_replay_time() -> None:
    result = get_reviews(
        "1234567890",
        limit=1,
        raw_dir=FIXTURES,
        request_delay=0,
    )

    assert result.provenance[0].fetched_at == "2026-07-13T00:00:00+00:00"
    assert "replayed_at" in result.provenance[0].detail


def test_live_review_pages_do_not_carry_upstream_cookies(monkeypatch) -> None:
    first_page = json.loads((FIXTURES / "page-001.json").read_text(encoding="utf-8"))
    second_page = json.loads((FIXTURES / "page-002.json").read_text(encoding="utf-8"))

    class FakeSession:
        def __init__(self):
            self.cookies = {}

    session = FakeSession()
    monkeypatch.setattr(reviews_module.requests, "Session", lambda: session)
    calls = 0

    def fetch(active_session, *_args):
        nonlocal calls
        assert active_session.cookies == {}
        active_session.cookies["anonymous"] = "must-not-persist"
        calls += 1
        return first_page if calls == 1 else second_page

    monkeypatch.setattr(reviews_module, "post_review_page", fetch)
    result = get_reviews("1234567890", limit=3, request_delay=0)

    assert result.status == Status.OK
    assert calls == 2
    assert session.cookies == {}


def test_malformed_live_json_is_upstream_changed_not_network_error() -> None:
    def malformed(**_kwargs):
        raise requests.exceptions.JSONDecodeError("bad", "x", 0)

    result = get_reviews(
        "1234567890",
        page_fetcher=malformed,
        request_delay=0,
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value


def test_later_malformed_raw_page_preserves_earlier_reviews(tmp_path: Path) -> None:
    (tmp_path / "page-001.json").write_text(
        (FIXTURES / "page-001.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "page-002.json").write_text("{bad", encoding="utf-8")

    result = get_reviews("1234567890", limit=3, raw_dir=tmp_path)

    assert result.status == Status.PARTIAL
    assert [review["id"] for review in result.data["reviews"]] == [
        "review-001",
        "review-002",
    ]
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value
    assert result.completeness.pages_fetched == 1


def test_unreadable_raw_page_error_does_not_expose_directory_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "page-001.json").mkdir()
    result = get_reviews("1234567890", raw_dir=tmp_path)

    assert result.status == Status.ERROR
    assert str(tmp_path) not in result.errors[0].message
    assert result.errors[0].message == "review fixture page-001.json is unreadable"


def test_offline_replay_never_uses_network_politeness_sleep() -> None:
    first_page = json.loads((FIXTURES / "page-001.json").read_text(encoding="utf-8"))
    second_page = json.loads((FIXTURES / "page-002.json").read_text(encoding="utf-8"))

    result = get_reviews(
        "1234567890",
        limit=3,
        pages=[first_page, second_page],
        sleep=lambda _seconds: (_ for _ in ()).throw(
            AssertionError("offline replay must not sleep")
        ),
    )

    assert result.status == Status.OK
