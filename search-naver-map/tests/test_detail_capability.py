from __future__ import annotations

import json
from pathlib import Path

import pytest

import naver_place.capabilities.detail as detail_module
from naver_place.capabilities.detail import (
    FEED_OPERATION,
    HOME_OPERATION,
    HOURS_OPERATION,
    extract_place_id,
    get_place_detail,
)
from naver_place.contracts import CapabilityError, ErrorCode, RequestBudget


FIXTURES = Path(__file__).parent / "fixtures" / "detail"
FETCHED_AT = "2026-01-01T00:00:00+00:00"


def status_value(result) -> str:
    return getattr(result.status, "value", str(result.status))


def code_value(error) -> str:
    return getattr(error.code, "value", str(error.code))


def home_html() -> str:
    return (FIXTURES / "home.html").read_text(encoding="utf-8")


def feed_html() -> str:
    return (FIXTURES / "feed.html").read_text(encoding="utf-8")


def hours_payload() -> dict:
    return json.loads((FIXTURES / "hours.json").read_text(encoding="utf-8"))


def test_live_html_prefers_utf8_over_requests_latin1_default() -> None:
    class Response:
        content = "플라워 블루헨".encode("utf-8")
        encoding = "ISO-8859-1"
        apparent_encoding = "utf-8"
        text = content.decode("latin-1")

    assert detail_module._response_text(Response()) == "플라워 블루헨"


def test_extract_place_id_accepts_id_and_naver_place_url() -> None:
    assert extract_place_id("1001") == "1001"
    assert extract_place_id("https://m.place.naver.com/place/1001/home") == "1001"


def test_full_offline_detail_replay_records_each_requested_source() -> None:
    result = get_place_detail(
        "1001",
        home_html=home_html(),
        feed_html=feed_html(),
        business_hours_payload=hours_payload(),
        offline=True,
        fetched_at=FETCHED_AT,
    )

    assert status_value(result) == "ok"
    assert result.data["place_id"] == "1001"
    assert result.data["base"]["name"] == "샘플 제주 게스트하우스"
    assert result.data["base"]["visitor_review_count"] == 27
    assert result.data["menus"][0]["name"] == "샘플 객실"
    assert result.data["feeds"][0]["title"] == "샘플 공지"
    assert result.data["business_hours"][0]["hours"][0]["start"] == "09:00"
    assert result.completeness.complete is True
    assert [entry.detail["replays"] for entry in result.provenance] == [
        HOME_OPERATION,
        FEED_OPERATION,
        HOURS_OPERATION,
    ]


def test_disabled_secondaries_are_outside_requested_scope() -> None:
    result = get_place_detail(
        "1001",
        include_feed=False,
        include_hours=False,
        home_html=home_html(),
        offline=True,
        fetched_at=FETCHED_AT,
    )

    assert status_value(result) == "ok"
    assert result.errors == ()
    assert len(result.provenance) == 1
    assert result.provenance[0].detail["replays"] == HOME_OPERATION
    assert result.completeness.complete is True


def test_enabled_but_missing_offline_secondaries_are_explicitly_partial() -> None:
    result = get_place_detail(
        "1001",
        home_html=home_html(),
        offline=True,
        fetched_at=FETCHED_AT,
    )

    assert status_value(result) == "partial"
    assert result.data["base"]["name"] == "샘플 제주 게스트하우스"
    assert {error.operation for error in result.errors} == {FEED_OPERATION, HOURS_OPERATION}
    assert all(code_value(error) == "secondary_not_found" for error in result.errors)
    assert result.completeness.complete is False


def test_secondary_hours_405_preserves_base_and_feed_as_partial(monkeypatch) -> None:
    rejected = CapabilityError(
        code=ErrorCode.UPSTREAM_REJECTED,
        message="HTTP 405",
        operation=HOURS_OPERATION,
        http_status=405,
    )

    class FakeTransportError(Exception):
        def __init__(self, error) -> None:
            super().__init__(error.message)
            self.error = error

    class FakeTransport:
        budget = None

        def __init__(self) -> None:
            self.operations = []

        def request(self, method, url, **kwargs):
            self.operations.append(kwargs["operation"])
            raise FakeTransportError(rejected)

    monkeypatch.setattr(detail_module, "TransportError", FakeTransportError)
    transport = FakeTransport()
    result = get_place_detail(
        "1001",
        home_html=home_html(),
        feed_html=feed_html(),
        include_hours=True,
        transport=transport,
        fetched_at=FETCHED_AT,
    )

    assert transport.operations == [HOURS_OPERATION]
    assert status_value(result) == "partial"
    assert result.data["base"]["name"] == "샘플 제주 게스트하우스"
    assert result.data["feeds"][0]["title"] == "샘플 공지"
    assert len(result.errors) == 1
    assert code_value(result.errors[0]) == "upstream_rejected"
    assert result.errors[0].http_status == 405
    assert result.completeness.complete is False
    assert result.completeness.stop_reason == "upstream_rejected"
    assert result.provenance[-1].detail["outcome"] == "error"


def test_secondary_hours_404_is_not_direct_resource_not_found() -> None:
    missing = CapabilityError(
        code=ErrorCode.NOT_FOUND,
        message="HTTP 404",
        operation=HOURS_OPERATION,
        http_status=404,
    )

    class FakeTransport:
        budget = None

        def request(self, *_args, **_kwargs):
            raise detail_module.TransportError(missing)

    result = get_place_detail(
        "1001",
        home_html=home_html(),
        include_feed=False,
        include_hours=True,
        transport=FakeTransport(),
        fetched_at=FETCHED_AT,
    )

    assert status_value(result) == "partial"
    assert code_value(result.errors[0]) == "secondary_not_found"
    assert result.errors[0].http_status == 404


def test_malformed_optional_feed_is_partial_not_a_base_failure() -> None:
    result = get_place_detail(
        "1001",
        home_html=home_html(),
        feed_html="<html>schema changed</html>",
        include_hours=False,
        offline=True,
        fetched_at=FETCHED_AT,
    )

    assert status_value(result) == "partial"
    assert result.data["base"]["name"] == "샘플 제주 게스트하우스"
    assert code_value(result.errors[0]) == "upstream_changed"
    assert result.errors[0].operation == FEED_OPERATION


def test_malformed_home_payload_is_a_typed_error() -> None:
    result = get_place_detail(
        "1001",
        include_feed=False,
        include_hours=False,
        home_html="<html>schema changed</html>",
        offline=True,
        fetched_at=FETCHED_AT,
    )

    assert status_value(result) == "error"
    assert code_value(result.errors[0]) == "upstream_changed"
    assert result.errors[0].operation == HOME_OPERATION
    assert result.data == {}


def test_malformed_home_stops_before_optional_network_calls() -> None:
    class FakeTransport:
        budget = None

        def __init__(self):
            self.calls = []

        def request(self, *_args, **kwargs):
            self.calls.append(kwargs["operation"])
            raise AssertionError("optional source must not be called")

    transport = FakeTransport()
    result = get_place_detail(
        "1001",
        home_html="<html>schema changed</html>",
        transport=transport,
    )

    assert status_value(result) == "error"
    assert code_value(result.errors[0]) == "upstream_changed"
    assert transport.calls == []


@pytest.mark.parametrize(
    "place",
    (
        "https://example.invalid/place/1001",
        "https://notnaver.com/place/1001/home",
        "https://user@notnaver.com/place/1001/home",
    ),
)
def test_invalid_direct_place_is_invalid_input(place: str) -> None:
    result = get_place_detail(place, include_feed=False, include_hours=False)

    assert status_value(result) == "error"
    assert code_value(result.errors[0]) == "invalid_input"
    assert result.request["include_feed"] is False
    assert result.request["include_hours"] is False


@pytest.mark.parametrize("code", (ErrorCode.BLOCKED, ErrorCode.RATE_LIMITED))
def test_feed_access_rejection_stops_later_optional_live_requests(code) -> None:
    class FakeTransport:
        budget = None

        def __init__(self):
            self.operations = []

        def request(self, *_args, **kwargs):
            self.operations.append(kwargs["operation"])
            raise detail_module.TransportError(
                CapabilityError(
                    code=code,
                    message="public surface rejected the request",
                    operation=kwargs["operation"],
                    http_status=429 if code is ErrorCode.RATE_LIMITED else 403,
                )
            )

    transport = FakeTransport()
    result = get_place_detail(
        "1001",
        home_html=home_html(),
        include_feed=True,
        include_hours=True,
        transport=transport,
        fetched_at=FETCHED_AT,
    )

    assert transport.operations == [FEED_OPERATION]
    assert status_value(result) == "partial"
    assert code_value(result.errors[0]) == code.value


def test_offline_detail_respects_elapsed_budget() -> None:
    now = [0.0]
    budget = RequestBudget(max_requests=1, max_elapsed_seconds=1, _clock=lambda: now[0])
    now[0] = 2.0
    result = get_place_detail(
        "1001",
        include_feed=False,
        include_hours=False,
        home_html=home_html(),
        offline=True,
        budget=budget,
    )
    assert status_value(result) == "error"
    assert code_value(result.errors[0]) == "time_budget_exhausted"


def test_offline_missing_home_preserves_the_supplied_budget() -> None:
    budget = RequestBudget(max_requests=7, max_elapsed_seconds=9)
    result = get_place_detail(
        "1001",
        include_feed=False,
        include_hours=False,
        offline=True,
        budget=budget,
    )

    assert result.budget is budget
    assert result.budget.snapshot()["request_limit"] == 7


def test_detail_fixture_without_capture_metadata_uses_unknown_timestamp() -> None:
    result = get_place_detail(
        "1001",
        include_feed=False,
        include_hours=False,
        home_html=home_html(),
        offline=True,
    )

    assert result.provenance[0].fetched_at == "unknown"
    assert "replayed_at" in result.provenance[0].detail
    assert result.data["fetched_at"] == "unknown"
