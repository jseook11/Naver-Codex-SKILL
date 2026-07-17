from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

import naver_place.capabilities.booking as booking_module
from naver_place.capabilities.booking import get_booking_availability
from naver_place.contracts import (
    CapabilityResult,
    Completeness,
    ErrorCode,
    RequestBudget,
    Status,
)
from scripts.naver_booking_client import BookingGraphQLClient, BookingTooManyRequests


FIXTURES = Path(__file__).parent / "fixtures" / "booking"


def _code(value):
    return getattr(value, "value", value)


def _items(result):
    return result.data["places"][0]["booking"]["items"]


def test_accommodation_raw_replay_has_conservative_unknown_and_small_standard_view():
    expected = json.loads(
        (FIXTURES / "expected-accommodation-standard.json").read_text(encoding="utf-8")
    )

    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        guests=2,
        raw_dir=FIXTURES,
        fetched_at="2026-07-13T00:00:00+00:00",
    )

    assert result.status == Status.OK
    items = _items(result)
    assert [item["biz_item_id"] for item in items] == expected["item_ids"]
    assert [item["is_available"] for item in items] == expected["availability"]
    assert "unknown_capacity" in items[1]["reasons"]
    assert "unknown_inventory" in items[1]["reasons"]
    for item in items:
        for omitted in expected["standard_omits"]:
            assert omitted not in item
    assert result.budget.requests_used == 0
    assert result.provenance
    assert all(provenance.live is False for provenance in result.provenance)


def test_full_view_keeps_description_images_and_options():
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        raw_dir=FIXTURES,
        view="full",
    )

    assert result.status == Status.OK
    first = _items(result)[0]
    assert first["description"]
    assert first["images"]
    assert "options" in first


def test_time_booking_raw_replay_preserves_slot_semantics():
    expected = json.loads(
        (FIXTURES / "expected-time-standard.json").read_text(encoding="utf-8")
    )

    result = get_booking_availability(
        business_id="7001",
        business_type_id=5,
        booking_date="2026-07-20",
        guests=2,
        raw_dir=FIXTURES,
    )

    assert result.status == Status.OK
    items = _items(result)
    assert [item["biz_item_id"] for item in items] == expected["item_ids"]
    assert [item["is_available"] for item in items] == expected["availability"]
    assert [slot["slot_id"] for slot in items[0]["available_slots"]] == expected["slot_ids"]
    for omitted in expected["standard_omits"]:
        assert omitted not in items[0]


def test_time_filters_validate_format_order_and_booking_kind():
    base = {
        "business_id": "7001",
        "business_type_id": 5,
        "booking_date": "2026-07-20",
        "raw_dir": FIXTURES,
    }
    malformed = get_booking_availability(**base, time_from="9:00")
    reversed_range = get_booking_availability(
        **base, time_from="10:00", time_to="09:00"
    )
    accommodation_filter = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        time_from="09:00",
        raw_dir=FIXTURES,
    )

    for result in (malformed, reversed_range, accommodation_filter):
        assert result.status == Status.ERROR
        assert _code(result.errors[0].code) == ErrorCode.INVALID_INPUT.value


def test_capacity_failure_is_false_while_missing_capacity_stays_unknown():
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        guests=5,
        raw_dir=FIXTURES,
    )

    items = _items(result)
    assert items[0]["is_available"] is False
    assert "capacity_lt_guests" in items[0]["reasons"]
    assert items[1]["is_available"] is None
    assert "unknown_capacity" in items[1]["reasons"]


def test_option_filter_mismatch_is_definitively_unavailable():
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        option_include_text=["존재하지 않는 옵션"],
        raw_dir=FIXTURES,
    )

    assert result.status == Status.OK
    assert all(item["is_available"] is False for item in _items(result))
    assert all("option_filter_mismatch" in item["reasons"] for item in _items(result))


class _OneRoomClient:
    rate_limit_stopped = False

    def search_biz_items(self, *_args):
        payload = json.loads(
            (FIXTURES / "5001_search_2026-07-20_2026-07-22.json").read_text(encoding="utf-8")
        )
        return payload["data"]["searchBizItem"]["bizItems"][:1]

    def daily_schedule(self, *_args):
        raise requests.Timeout("schedule timed out")


def test_per_item_schedule_failure_is_partial_and_availability_is_null():
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        guests=2,
        client=_OneRoomClient(),
    )

    assert result.status == Status.PARTIAL
    assert _items(result)[0]["is_available"] is None
    assert "schedule_incomplete" in _items(result)[0]["reasons"]
    assert _code(result.errors[0].code) == ErrorCode.NETWORK_ERROR.value


class _RateLimitedClient:
    rate_limit_stopped = True

    def search_biz_items(self, *_args):
        raise BookingTooManyRequests("BookingAPITooManyRequests")


def test_initial_booking_rate_limit_is_typed_error():
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        client=_RateLimitedClient(),
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.RATE_LIMITED.value


class _BatchRateLimitedClient:
    rate_limit_stopped = False

    def search_biz_items(self, business_id, *_args):
        if business_id == "9002":
            self.rate_limit_stopped = True
            raise BookingTooManyRequests("BookingAPITooManyRequests")
        return [
            {
                "bizItemId": "batch-room",
                "name": "배치 객실",
                "priceByDates": {"2026-07-20": 50000},
                "typeValues": [{"code": "CD129", "codeValue": "2"}],
            }
        ]

    def daily_schedule(self, *_args):
        return {
            "data": {
                "schedule": {
                    "bizItemSchedule": {
                        "daily": [
                            {
                                "date": "2026-07-20",
                                "summary": {
                                    "dateKey": "2026-07-20",
                                    "stock": 2,
                                    "bookingCount": 0,
                                    "isBusinessDay": True,
                                    "isSaleDay": True,
                                    "prices": [{"price": 50000}],
                                },
                            }
                        ]
                    }
                }
            }
        }


def test_batch_rate_limit_after_one_business_preserves_first_place():
    result = get_booking_availability(
        query="합성 숙소",
        check_in="2026-07-20",
        check_out="2026-07-21",
        candidates=[
            {"place_id": "p1", "reservation_url": "https://booking.naver.com/booking/3/bizes/9001"},
            {"place_id": "p2", "reservation_url": "https://booking.naver.com/booking/3/bizes/9002"},
        ],
        client=_BatchRateLimitedClient(),
    )

    assert result.status == Status.PARTIAL
    assert result.data["places"][0]["booking"]["items"][0]["is_available"] is True
    assert _code(result.errors[-1].code) == ErrorCode.RATE_LIMITED.value


class _BudgetClient(_OneRoomClient):
    def daily_schedule(self, *_args):
        raise AssertionError("budget must stop before this call")


def test_budget_exhaustion_preserves_discovered_item_as_partial_unknown():
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        guests=2,
        client=_BudgetClient(),
        request_budget=1,
    )

    assert result.status == Status.PARTIAL
    assert _items(result)[0]["is_available"] is None
    assert _code(result.errors[0].code) == ErrorCode.REQUEST_BUDGET_EXHAUSTED.value
    assert result.budget.requests_used == 1


def test_slow_booking_response_finishing_after_deadline_is_not_success():
    now = [0.0]

    class SlowClient:
        rate_limit_stopped = False
        timeout = 30
        seen_timeout = None

        def search_biz_items(self, *_args):
            self.seen_timeout = self.timeout
            now[0] = 2.0
            return []

    client = SlowClient()
    budget = RequestBudget(max_requests=2, max_elapsed_seconds=1, _clock=lambda: now[0])
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        client=client,
        budget=budget,
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.TIME_BUDGET_EXHAUSTED.value
    assert client.seen_timeout <= 0.5
    assert client.timeout == 30


def test_query_candidate_without_reservation_url_is_typed_partial():
    result = get_booking_availability(
        query="합성 검색",
        check_in="2026-07-20",
        check_out="2026-07-22",
        candidates=[
            {
                "place_id": "place-no-booking",
                "place_name": "예약 링크 없는 장소",
                "category": "테스트",
                "address": "서울",
                "reservation_url": "",
            }
        ],
    )

    assert result.status == Status.PARTIAL
    assert _code(result.errors[0].code) == ErrorCode.SECONDARY_NOT_FOUND.value
    assert result.completeness.complete is False
    assert result.data["places"][0]["booking"]["status"] == "no_reservation_url"


class _EmptyClient:
    rate_limit_stopped = False

    def search_biz_items(self, *_args):
        return []


def test_direct_empty_business_is_not_found_not_empty_search():
    result = get_booking_availability(
        business_id="4040",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        client=_EmptyClient(),
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.NOT_FOUND.value


def test_invalid_source_and_dates_are_typed_without_client_calls():
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-22",
        check_out="2026-07-20",
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.INVALID_INPUT.value


def test_malformed_direct_booking_url_is_invalid_input() -> None:
    result = get_booking_availability(
        booking_url="http://booking.naver.com/booking/3/bizes/5001",
        check_in="2026-07-20",
        check_out="2026-07-22",
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.INVALID_INPUT.value
    assert result.budget is None


def test_available_only_filtered_empty_is_not_mislabeled_not_found() -> None:
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        option_include_text=["존재하지 않는 옵션"],
        available_only=True,
        raw_dir=FIXTURES,
    )

    assert result.status == Status.EMPTY
    assert result.errors == ()
    assert result.data["item_count"] == 0
    assert result.data["observed_item_count"] == 2
    assert result.completeness.stop_reason == "filtered"


class _MissingSpecificItemClient:
    rate_limit_stopped = False

    def search_biz_items(self, *_args):
        return []

    def biz_items(self, *_args):
        return []

    def biz_item(self, *_args):
        return {}


@pytest.mark.parametrize(
    ("booking_url", "schedule"),
    [
        (
            "https://booking.naver.com/booking/3/bizes/4040/items/9999",
            {"check_in": "2026-07-20", "check_out": "2026-07-22"},
        ),
        (
            "https://booking.naver.com/booking/5/bizes/4040/items/9999",
            {"booking_date": "2026-07-20"},
        ),
    ],
)
def test_nonexistent_specific_item_is_not_fabricated_as_unknown(
    booking_url: str, schedule: dict
) -> None:
    result = get_booking_availability(
        booking_url=booking_url,
        client=_MissingSpecificItemClient(),
        **schedule,
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.NOT_FOUND.value
    assert result.data["places"][0]["booking"]["items"] == []


def test_candidate_provider_failure_has_error_provenance() -> None:
    def fail(**_kwargs):
        raise requests.Timeout("map provider timed out")

    result = get_booking_availability(
        query="샘플 숙소",
        check_in="2026-07-20",
        check_out="2026-07-22",
        candidate_provider=fail,
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.NETWORK_ERROR.value
    assert result.provenance[0].detail["outcome"] == "error"


class _MalformedScheduleClient(_OneRoomClient):
    def daily_schedule(self, *_args):
        return {}


def test_missing_schedule_shape_is_upstream_changed_not_complete_unknown() -> None:
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        client=_MalformedScheduleClient(),
    )

    assert result.status == Status.PARTIAL
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value
    assert result.completeness.complete is False


def test_booking_fixture_provenance_uses_capture_date_not_replay_time() -> None:
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        raw_dir=FIXTURES,
    )

    assert result.provenance[0].fetched_at == "2026-07-13T00:00:00+00:00"
    assert "replayed_at" in result.provenance[0].detail


class _ScalarListClient:
    rate_limit_stopped = False

    def search_biz_items(self, *_args):
        return ["schema-v2"]


def test_non_object_booking_list_item_is_typed_schema_drift() -> None:
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        client=_ScalarListClient(),
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value


def test_booking_client_is_stateless_and_rejects_redirects(monkeypatch) -> None:
    client = BookingGraphQLClient()
    seen = {}

    class Response:
        status_code = 302
        headers = {"Location": "https://booking.naver.com/other"}

    def post(*_args, **kwargs):
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr(client.session, "post", post)
    with pytest.raises(requests.HTTPError):
        client._post_graphql("test", "query Test { test }", {}, None)

    assert client.session.trust_env is False
    assert seen["allow_redirects"] is False


class _MissingNestedScheduleClient:
    rate_limit_stopped = False

    def biz_items(self, *_args):
        return [{"bizItemId": "time-1", "name": "시간 상품", "maxBookingCount": 4}]

    def hourly_schedule(self, *_args):
        return {"data": {"schedule": {}}}


def test_missing_nested_schedule_is_partial_unknown_not_no_slots() -> None:
    result = get_booking_availability(
        business_id="7001",
        business_type_id=5,
        booking_date="2026-07-20",
        client=_MissingNestedScheduleClient(),
    )

    assert result.status == Status.PARTIAL
    item = _items(result)[0]
    assert item["is_available"] is None
    assert "schedule_incomplete" in item["reasons"]
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value


class _MalformedJsonClient:
    rate_limit_stopped = False

    def search_biz_items(self, *_args):
        raise requests.exceptions.JSONDecodeError("bad", "x", 0)


def test_malformed_booking_json_is_upstream_changed_not_network_error() -> None:
    result = get_booking_availability(
        business_id="5001",
        business_type_id=3,
        check_in="2026-07-20",
        check_out="2026-07-22",
        client=_MalformedJsonClient(),
    )

    assert result.status == Status.ERROR
    assert _code(result.errors[0].code) == ErrorCode.UPSTREAM_CHANGED.value


def test_partial_map_discovery_cannot_be_promoted_to_complete_booking(
    monkeypatch,
) -> None:
    def partial_map(*_args, **kwargs):
        return CapabilityResult(
            capability="map.search",
            request={},
            data={
                "places": [
                    {
                        "place_id": "p1",
                        "name": "샘플 숙소",
                        "category": "숙박",
                        "address": "서울",
                        "road_address": "",
                        "reservation_url": "https://booking.naver.com/booking/3/bizes/9001",
                    }
                ]
            },
            status=Status.PARTIAL,
            warnings=("source page is not exhaustive",),
            completeness=Completeness(
                complete=False, stop_reason="source_page_limit"
            ),
            budget=kwargs["budget"],
        )

    monkeypatch.setattr(booking_module, "search_places", partial_map)
    result = get_booking_availability(
        query="샘플 숙소",
        check_in="2026-07-20",
        check_out="2026-07-21",
        client=_BatchRateLimitedClient(),
    )

    assert result.data["item_count"] == 1
    assert result.status == Status.PARTIAL
    assert result.completeness.complete is False
    assert result.completeness.stop_reason == "source_page_limit"
