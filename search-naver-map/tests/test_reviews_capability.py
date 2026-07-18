from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from naver_place.capabilities.reviews import get_reviews
from naver_place.contracts import ErrorCode, RequestBudget, RequestPolicy, Status
from naver_place.transport import Transport


FIXTURES = Path(__file__).parent / "fixtures" / "reviews"
FETCHED_AT = "2026-07-19T00:00:00+00:00"
PLACE_ID = "1234567890"
RECOMMENDED_URL = f"https://m.place.naver.com/place/{PLACE_ID}/review/visitor"
LATEST_URL = f"{RECOMMENDED_URL}?reviewSort=recent"


def _code(value: Any) -> str:
    return str(getattr(value, "value", value))


def _latest_html() -> str:
    return (FIXTURES / "latest.html").read_text(encoding="utf-8")


def _recommended_html() -> str:
    return (FIXTURES / "recommended.html").read_text(encoding="utf-8")


def _offline_reviews(**kwargs: Any):
    return get_reviews(
        PLACE_ID,
        latest_html=_latest_html(),
        recommended_html=_recommended_html(),
        fetched_at=FETCHED_AT,
        **kwargs,
    )


def _sample_ids(result: Any, group: str) -> list[str]:
    return list(result.data["samples"][group]["review_ids"])


def _sample_reviews(result: Any, group: str) -> list[dict[str, Any]]:
    by_id = {review["id"]: review for review in result.data["reviews"]}
    return [by_id[review_id] for review_id in _sample_ids(result, group)]


class _Response:
    def __init__(self, url: str, html: str, status_code: int = 200) -> None:
        self.url = url
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.content = html.encode("utf-8")
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"
        self.text = html

    def close(self) -> None:
        return None


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers: dict[str, str] = {}
        self.auth = None
        self.trust_env = True

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs))
        return next(self.responses)


def _transport(
    responses: list[_Response], *, max_requests: int = 40
) -> tuple[Transport, _Session]:
    session = _Session(responses)
    transport = Transport(
        session=session,
        policy=RequestPolicy(max_attempts=1),
        budget=RequestBudget(max_requests=max_requests),
    )
    return transport, session


def test_offline_snapshots_select_exact_roots_and_preserve_each_upstream_order():
    result = _offline_reviews()

    assert result.status == Status.OK
    assert _sample_ids(result, "latest") == [
        "latest-001",
        "latest-002",
        "latest-003",
        "shared-001",
    ]
    assert _sample_ids(result, "recommended") == [
        "recommended-001",
        "recommended-002",
        "recommended-003",
        "shared-001",
    ]
    assert _sample_ids(result, "recommended_keyword_only") == [
        "keyword-001",
        "keyword-002",
    ]
    assert [review["id"] for review in result.data["reviews"]] == [
        "latest-001",
        "latest-002",
        "latest-003",
        "shared-001",
        "recommended-001",
        "recommended-002",
        "recommended-003",
        "keyword-001",
        "keyword-002",
    ]
    shared = next(
        review for review in result.data["reviews"] if review["id"] == "shared-001"
    )
    assert shared["sample_sources"] == ["latest", "recommended"]
    assert shared["sample_ranks"] == {"latest": 4, "recommended": 4}
    assert result.data["samples"]["latest"]["total_available"] == 24
    assert result.data["samples"]["recommended"]["total_available"] == 19
    assert (
        result.data["samples"]["recommended_keyword_only"]["total_available"]
        == 7
    )
    assert result.data["total_available"] == 26
    assert result.data["returned_count"] == 9
    assert result.completeness.complete is True
    assert result.completeness.stop_reason == "snapshot_complete"
    assert result.completeness.pages_fetched == 2
    assert result.completeness.returned_count == 10
    assert result.budget is None
    assert all(provenance.live is False for provenance in result.provenance)


def test_content_and_keyword_only_samples_remain_labeled_and_distinct():
    result = _offline_reviews()

    for group in ("latest", "recommended"):
        assert all(review["text"].strip() for review in _sample_reviews(result, group))
    keyword_only = _sample_reviews(result, "recommended_keyword_only")
    assert all(not review["text"] for review in keyword_only)
    assert [review["tags"] for review in keyword_only] == [
        ["음식이 맛있어요", "친절해요"],
        ["공간이 편안해요"],
    ]
    assert all(
        review["sample_sources"] == ["recommended_keyword_only"]
        for review in keyword_only
    )


def test_limit_is_per_sample_and_total_available_is_context_only():
    result = _offline_reviews(limit=2)

    assert result.status == Status.OK
    assert {
        name: sample["returned_count"]
        for name, sample in result.data["samples"].items()
    } == {
        "latest": 2,
        "recommended": 2,
        "recommended_keyword_only": 2,
    }
    assert result.completeness.complete is True
    assert result.completeness.stop_reason == "snapshot_complete"
    assert result.errors == ()


def test_limit_zero_is_complete_and_makes_no_live_requests():
    transport, session = _transport([])

    result = get_reviews(PLACE_ID, limit=0, transport=transport)

    assert result.status == Status.EMPTY
    assert result.data["reviews"] == []
    assert all(
        sample["review_ids"] == [] for sample in result.data["samples"].values()
    )
    assert result.completeness.complete is True
    assert result.completeness.stop_reason == "requested_limit"
    assert session.calls == []


@pytest.mark.parametrize("limit", (-1, 11, 500, True))
def test_limit_outside_zero_to_ten_is_invalid(limit: Any):
    result = get_reviews(PLACE_ID, limit=limit)

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.INVALID_INPUT.value


def test_offline_replay_requires_both_html_snapshots():
    result = get_reviews(PLACE_ID, latest_html=_latest_html())

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.INVALID_INPUT.value


def test_standard_view_omits_extended_reviewer_fields():
    result = _offline_reviews(view="standard")

    for review in result.data["reviews"]:
        assert "reviewer_id" not in review
        assert "receipt_info_url" not in review
        assert "cursor" not in review


def test_full_view_retains_extended_public_fields_only_when_requested():
    result = _offline_reviews(view="full")

    latest = _sample_reviews(result, "latest")[0]
    recommended = _sample_reviews(result, "recommended")[0]
    assert latest["reviewer_id"] == "private-latest-object-id-1"
    assert latest["receipt_info_url"].endswith("private-latest-receipt-1")
    assert recommended["reviewer_id"] == "private-recommended-object-id-1"
    assert recommended["receipt_info_url"].endswith(
        "private-recommended-receipt-1"
    )


def test_owner_reply_filter_is_local_to_each_snapshot_and_does_not_fetch_more():
    result = _offline_reviews(owner_reply="only_replied")

    assert _sample_ids(result, "latest") == ["latest-002"]
    assert _sample_ids(result, "recommended") == ["recommended-002"]
    assert _sample_ids(result, "recommended_keyword_only") == []
    assert result.status == Status.OK
    assert result.completeness.complete is True
    assert result.completeness.stop_reason == "snapshot_complete"


def test_live_reviews_use_two_logical_public_html_gets_without_pagination():
    transport, session = _transport(
        [
            _Response(LATEST_URL, _latest_html()),
            _Response(RECOMMENDED_URL, _recommended_html()),
        ]
    )

    result = get_reviews(PLACE_ID, transport=transport, fetched_at=FETCHED_AT)

    assert result.status == Status.OK
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", LATEST_URL),
        ("GET", RECOMMENDED_URL),
    ]
    assert all(
        kwargs["allow_redirects"] is False for _, _, kwargs in session.calls
    )
    assert all(
        not any(
            key.casefold() in {"cookie", "authorization"}
            for key in kwargs.get("headers", {})
        )
        for _, _, kwargs in session.calls
    )
    assert result.budget.requests_used == 2
    assert all(provenance.live is True for provenance in result.provenance)


def test_request_budget_exhaustion_preserves_latest_snapshot():
    transport, session = _transport(
        [_Response(LATEST_URL, _latest_html())],
        max_requests=1,
    )

    result = get_reviews(PLACE_ID, transport=transport, fetched_at=FETCHED_AT)

    assert result.status == Status.PARTIAL
    assert _sample_ids(result, "latest") == [
        "latest-001",
        "latest-002",
        "latest-003",
        "shared-001",
    ]
    assert _sample_ids(result, "recommended") == []
    assert _sample_ids(result, "recommended_keyword_only") == []
    assert (
        _code(result.errors[0].code)
        == ErrorCode.REQUEST_BUDGET_EXHAUSTED.value
    )
    assert result.completeness.complete is False
    assert len(session.calls) == 1
    assert result.budget.requests_used == 1


def test_malformed_recommended_snapshot_preserves_latest_as_typed_partial():
    transport, session = _transport(
        [
            _Response(LATEST_URL, _latest_html()),
            _Response(RECOMMENDED_URL, "<html><body>schema changed</body></html>"),
        ]
    )

    result = get_reviews(PLACE_ID, transport=transport, fetched_at=FETCHED_AT)

    assert result.status == Status.PARTIAL
    assert _sample_ids(result, "latest") == [
        "latest-001",
        "latest-002",
        "latest-003",
        "shared-001",
    ]
    assert _sample_ids(result, "recommended") == []
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value
    assert result.completeness.complete is False
    assert len(session.calls) == 2


@pytest.mark.parametrize("status,code", ((403, "blocked"), (429, "rate_limited")))
def test_access_rejection_stops_before_recommended_snapshot(status: int, code: str):
    transport, session = _transport(
        [_Response(LATEST_URL, "rejected", status_code=status)]
    )

    result = get_reviews(PLACE_ID, transport=transport, fetched_at=FETCHED_AT)

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == code
    assert result.errors[0].http_status == status
    assert result.completeness.complete is False
    assert len(session.calls) == 1


def test_missing_matching_roots_are_upstream_changed_not_empty():
    latest = _latest_html().replace(PLACE_ID, "1111111111")
    recommended = _recommended_html().replace(PLACE_ID, "1111111111")

    result = get_reviews(
        PLACE_ID,
        latest_html=latest,
        recommended_html=recommended,
        fetched_at=FETCHED_AT,
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value
    assert result.completeness.complete is False


def test_missing_recommended_keyword_root_is_upstream_changed_not_zero_reviews():
    recommended = _recommended_html().replace(
        f'\\"businessId\\":\\"{PLACE_ID}\\",\\"includeContent\\":false',
        '\\"businessId\\":\\"1111111111\\",\\"includeContent\\":false',
        1,
    )

    result = get_reviews(
        PLACE_ID,
        latest_html=_latest_html(),
        recommended_html=recommended,
        fetched_at=FETCHED_AT,
    )

    assert result.status == Status.PARTIAL
    assert _sample_ids(result, "latest") == [
        "latest-001",
        "latest-002",
        "latest-003",
        "shared-001",
    ]
    assert _sample_ids(result, "recommended_keyword_only") == []
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value
    assert result.completeness.complete is False


def test_broken_review_reference_is_upstream_changed_not_silently_dropped():
    latest = _latest_html().replace(
        "VisitorReview:latest-001:true",
        "VisitorReview:missing:true",
        1,
    )

    result = get_reviews(
        PLACE_ID,
        latest_html=latest,
        recommended_html=_recommended_html(),
        fetched_at=FETCHED_AT,
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value


def test_authenticated_headers_are_rejected_before_fetching():
    transport, session = _transport([])

    result = get_reviews(
        PLACE_ID,
        headers={"Cookie": "secret"},
        transport=transport,
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.INVALID_INPUT.value
    assert session.calls == []


def test_offline_provenance_uses_capture_time_and_never_claims_live():
    result = _offline_reviews()

    assert [entry.fetched_at for entry in result.provenance] == [
        FETCHED_AT,
        FETCHED_AT,
    ]
    assert all(entry.live is False for entry in result.provenance)
    assert [entry.detail["replays"] for entry in result.provenance] == [
        "place.reviews_latest_html",
        "place.reviews_recommended_html",
    ]


def test_invalid_place_and_owner_reply_are_typed_without_fetching():
    result = get_reviews("not-a-place", owner_reply="sometimes")

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.INVALID_INPUT.value
