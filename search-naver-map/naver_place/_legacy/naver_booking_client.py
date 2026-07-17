#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

try:
    from .scrape_naver_map import build_mobile_map_headers, strip_html_text
except ImportError:  # Direct script execution.
    from scrape_naver_map import build_mobile_map_headers, strip_html_text


BOOKING_GRAPHQL_URL = "https://booking.naver.com/graphql"
ACCOMMODATION_BUSINESS_TYPE_ID = 3
STANDARD_PERSONNEL_CODE = "CD128"
MAX_PERSONNEL_CODE = "CD129"

BIZ_ITEMS_QUERY = """
query bizItems($input: BizItemsParams) {
  bizItems(input: $input) {
    id
    businessId
    bizItemId
    bizItemType
    bizItemSubType
    name
    desc
    stock
    price
    startDate
    endDate
    availableStartDate
    bookingAvailableCode
    bookingAvailableValue
    minBookingCount
    maxBookingCount
    minBookingTime
    maxBookingTime
    priceByDates
    bizItemResources {
      resourceUrl
      bizItemResourceSeq
      bizItemId
      order
      resourceTypeCode
      regDateTime
    }
    minMaxPrice {
      minPrice
      maxPrice
      isSinglePrice
    }
    typeValues {
      bizItemId
      code
      codeValue
    }
  }
}
""".strip()

SEARCH_BIZ_ITEM_QUERY = """
query searchBizItem($bizItemSearchParams: BizItemSearchParams) {
  searchBizItem(input: $bizItemSearchParams) {
    id
    bizItems {
      id
      businessId
      bizItemId
      bizItemType
      bizItemSubType
      name
      desc
      phone
      addressJson
      startDate
      endDate
      refundDate
      isPeriodFixed
      isOnsitePayment
      isClosedBooking
      isClosedBookingUser
      isImp
      bookingTimeUnitCode
      price
      bizItemResources {
        resourceUrl
        bizItemResourceSeq
        bizItemId
        order
        resourceTypeCode
        regDateTime
      }
      minBookingCount
      maxBookingCount
      extraFeeSettingJson
      bookableSettingJson
      bookingCountSettingJson
      additionalPropertyJson {
        ageRatingSetting
        openingHoursSetting
        runningTime
        ticketingTypeSetting
        accommodationAdditionalProperty
      }
      todayDealRate
      extraDescJson
      bookingPrecautionJson {
        title
        desc
      }
      orderSettingJson
      minBookingTime
      maxBookingTime
      priceByDates
      minMaxPrice {
        minPrice
        maxPrice
        isSinglePrice
      }
      typeValues {
        bizItemId
        code
        codeValue
      }
    }
  }
}
""".strip()

BIZ_ITEM_DETAIL_QUERY = """
query bizItem($input: BizItemParams) {
  bizItem(input: $input) {
    id
    businessId
    bizItemId
    bizItemType
    bizItemSubType
    name
    desc
    phone
    stock
    price
    addressJson
    startDate
    endDate
    refundDate
    availableStartDate
    bookingAvailableCode
    bookingAvailableValue
    bookingConfirmCode
    bookingTimeUnitCode
    isPeriodFixed
    isOnsitePayment
    isClosedBooking
    isClosedBookingUser
    minBookingCount
    maxBookingCount
    minBookingTime
    maxBookingTime
    extraFeeSettingJson
    bookableSettingJson
    bookingCountSettingJson
    paymentSettingJson
    priceByDates
    websiteUrl
    customFormJson
    optionCategoryMappings
    bizItemCategoryId
    additionalPropertyJson {
      ageRatingSetting
      openingHoursSetting
      runningTime
      ticketingTypeSetting
      accommodationAdditionalProperty
      arrangementCountSetting {
        isUsingHeadCount
        minHeadCount
        maxHeadCount
      }
    }
    bizItemResources {
      resourceUrl
      bizItemResourceSeq
      bizItemId
      order
      resourceTypeCode
      regDateTime
    }
    minMaxPrice {
      minPrice
      minNormalPrice
      maxPrice
      maxNormalPrice
      isSinglePrice
    }
    typeValues {
      bizItemId
      code
      codeValue
    }
  }
}
""".strip()

DAILY_SCHEDULE_QUERY = """
query schedule($scheduleParams: ScheduleParams) {
  schedule(input: $scheduleParams) {
    bizItemSchedule {
      daily {
        date
        summary {
          dateKey
          minBookingCount
          maxBookingCount
          bookingCount
          stock
          isBusinessDay
          hasBusinessDays
          isSaleDay
          startTime
          endTime
          todayDealRate
          hasBookableSlots
          prices {
            groupName
            isDefault
            price
            priceId
            name
            normalPrice
            desc
            order
            saleStartDateTime
            saleEndDateTime
          }
        }
      }
    }
  }
}
""".strip()

HOURLY_SCHEDULE_QUERY = """
query hourlySchedule($scheduleParams: ScheduleParams) {
  schedule(input: $scheduleParams) {
    bizItemSchedule {
      hourly {
        id
        name
        slotId
        scheduleId
        detailScheduleId
        unitStartDateTime
        unitStartTime
        unitBookingCount
        unitStock
        bookingCount
        occupiedBookingCount
        stock
        isBusinessDay
        isSaleDay
        isUnitSaleDay
        isUnitBusinessDay
        isHoliday
        duration
        desc
        minBookingCount
        maxBookingCount
        saleStartDateTime
        saleEndDateTime
        seatGroups {
          color
          maxPrice
          name
          remainStock
        }
        prices {
          groupName
          isDefault
          price
          priceId
          scheduleId
          priceTypeCode
          name
          normalPrice
          desc
          order
          groupOrder
          slotId
          agencyKey
          bookingCount
          isImp
          saleStartDateTime
          saleEndDateTime
        }
      }
    }
  }
}
""".strip()

OPTION_QUERY = """
query option($input: OptionParams) {
  option(input: $input) {
    id
    categoryId
    name
    categoryTypeCode
    selectionTypeCode
    isImp
    options {
      id
      optionId
      desc
      stock
      isFree
      isSoldOut
      categoryId
      categoryName
      name
      normalPrice
      price
      sumPrice
      priceDesc
      schedules
      categoryTypeCode
      titleImageUrl
      discountRate
      minBookingCount
      maxBookingCount
      bookingCount
      remainStock
      isOnsitePayment
    }
  }
}
""".strip()


@dataclass(frozen=True)
class BookingIds:
    business_type_id: int
    business_id: str
    biz_item_id: str = ""
    url: str = ""


class BookingGraphQLError(ValueError):
    pass


class BookingTooManyRequests(BookingGraphQLError):
    pass


class BookingRateLimitStopped(BookingTooManyRequests):
    pass


def build_booking_headers(user_agent: str | None = None, referer: str | None = None) -> dict[str, str]:
    headers = build_mobile_map_headers(user_agent)
    headers.update(
        {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": "https://booking.naver.com",
            "Referer": referer or "https://booking.naver.com/",
        }
    )
    return headers


def parse_booking_url(value: str) -> BookingIds:
    value = value.strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("올바른 네이버 예약 URL을 입력하세요.") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or not (host == "booking.naver.com" or host.endswith(".booking.naver.com"))
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("네이버 예약 URL을 입력하세요.")

    match = re.search(r"/booking/(\d+)/bizes/(\d+)(?:/items/(\d+))?", parsed.path)
    if match:
        return BookingIds(
            business_type_id=int(match.group(1)),
            business_id=match.group(2),
            biz_item_id=match.group(3) or "",
            url=value,
        )

    query = parse_qs(parsed.query)
    business_type = first_present(query.get("businessTypeId"), query.get("business_type_id"))
    business_id = first_present(query.get("businessId"), query.get("business_id"), query.get("bizId"))
    biz_item_id = first_present(query.get("bizItemId"), query.get("biz_item_id"))
    if business_type and business_id:
        return BookingIds(
            business_type_id=int(first_list_value(business_type)),
            business_id=str(first_list_value(business_id)),
            biz_item_id=str(first_list_value(biz_item_id or "")),
            url=value,
        )

    raise ValueError("네이버 예약 URL에서 businessTypeId/businessId를 찾지 못했습니다.")


def build_business_url(business_type_id: int, business_id: str) -> str:
    return f"https://booking.naver.com/booking/{business_type_id}/bizes/{business_id}"


def build_item_url(
    business_type_id: int,
    business_id: str,
    biz_item_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    url = f"{build_business_url(business_type_id, business_id)}/items/{biz_item_id}"
    if start_date and end_date:
        return f"{url}?{urlencode({'startDateTime': start_date + 'T00:00:00', 'endDateTime': end_date + 'T00:00:00'})}"
    return url


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", []):
            return value
    return None


def first_list_value(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else ""
    return value


def safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = value.strip()
    if not value:
        return value
    if value[0] not in "[{":
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def date_range(start_date: str, end_date: str, include_end: bool = False) -> list[str]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")
    dates: list[str] = []
    current = start
    while current < end or (include_end and current <= end):
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> str:
    return strip_html_text(value)


def type_value_map(item: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for entry in item.get("typeValues") or []:
        if isinstance(entry, dict) and entry.get("code"):
            values[str(entry["code"])] = str(entry.get("codeValue", ""))
    return values


def normalize_resources(item: dict[str, Any]) -> list[dict[str, Any]]:
    resources = []
    for resource in item.get("bizItemResources") or []:
        if not isinstance(resource, dict):
            continue
        url = resource.get("resourceUrl")
        if not url:
            continue
        resources.append(
            {
                "url": url,
                "order": resource.get("order"),
                "resource_type": resource.get("resourceTypeCode"),
            }
        )
    return resources


def normalize_options(categories: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for category in categories or []:
        if not isinstance(category, dict):
            continue
        normalized_options = []
        for option in category.get("options") or []:
            if not isinstance(option, dict):
                continue
            normalized_options.append(
                {
                    "id": option.get("optionId") or option.get("id"),
                    "name": clean_text(option.get("name")),
                    "price": option.get("price"),
                    "normal_price": option.get("normalPrice"),
                    "min_booking_count": option.get("minBookingCount"),
                    "max_booking_count": option.get("maxBookingCount"),
                    "booking_count": option.get("bookingCount"),
                    "remain_stock": option.get("remainStock"),
                    "is_sold_out": option.get("isSoldOut"),
                    "schedules": option.get("schedules"),
                }
            )
        normalized.append(
            {
                "id": category.get("categoryId") or category.get("id"),
                "name": clean_text(category.get("name")),
                "category_type": category.get("categoryTypeCode"),
                "selection_type": category.get("selectionTypeCode"),
                "options": normalized_options,
            }
        )
    return normalized


def daily_summary_map(schedule_payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    root = schedule_payload or {}
    if isinstance(root.get("data"), dict):
        root = root["data"]
    daily = ((root.get("schedule") or {}).get("bizItemSchedule") or {}).get("daily")
    if not daily:
        return {}
    if isinstance(daily, dict):
        summary = daily.get("summary")
        if isinstance(summary, list):
            return {str(item.get("dateKey")): item for item in summary if isinstance(item, dict) and item.get("dateKey")}
        date_map = daily.get("date")
        if isinstance(date_map, dict):
            return {str(key): value for key, value in date_map.items() if isinstance(value, dict)}
    if isinstance(daily, list):
        result: dict[str, dict[str, Any]] = {}
        for item in daily:
            if not isinstance(item, dict):
                continue
            summary = item.get("summary") if isinstance(item.get("summary"), dict) else item
            date_key = summary.get("dateKey") or item.get("date")
            if date_key:
                result[str(date_key)] = summary
        return result
    return {}


def hourly_schedules(schedule_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    root = schedule_payload or {}
    if isinstance(root.get("data"), dict):
        root = root["data"]
    hourly = ((root.get("schedule") or {}).get("bizItemSchedule") or {}).get("hourly")
    return hourly if isinstance(hourly, list) else []


def remaining_stock(slot: dict[str, Any]) -> int | None:
    unit_stock = safe_int(slot.get("unitStock"))
    unit_booking_count = safe_int(slot.get("unitBookingCount")) or 0
    stock = safe_int(slot.get("stock"))
    booking_count = safe_int(slot.get("bookingCount")) or 0
    if unit_stock is not None:
        return max(0, unit_stock - unit_booking_count)
    if stock is not None:
        return max(0, stock - booking_count)
    seat_groups = slot.get("seatGroups") or []
    if isinstance(seat_groups, list):
        stocks = [safe_int(group.get("remainStock")) for group in seat_groups if isinstance(group, dict)]
        stocks = [value for value in stocks if value is not None]
        if stocks:
            return max(stocks)
    return None


def normalize_slot(slot: dict[str, Any], guests: int) -> dict[str, Any]:
    remain_stock = remaining_stock(slot)
    prices = slot.get("prices") or []
    first_price = prices[0] if prices and isinstance(prices[0], dict) else {}
    start_time = time_part(slot.get("unitStartTime")) or time_part(slot.get("unitStartDateTime"))
    is_open = slot.get("isOpen", True)
    is_bookable = (
        is_open is not False
        and slot.get("isBusinessDay") is not False
        and slot.get("isSaleDay") is not False
        and slot.get("isUnitBusinessDay") is not False
        and slot.get("isUnitSaleDay") is not False
        and slot.get("isHoliday") is not True
        and (remain_stock is None or remain_stock >= guests)
    )
    return {
        "slot_id": slot.get("slotId"),
        "start_date_time": slot.get("unitStartDateTime"),
        "start_time": start_time,
        "duration": slot.get("duration"),
        "remain_stock": remain_stock,
        "is_available": bool(is_bookable),
        "price": first_price.get("price"),
        "price_name": clean_text(first_price.get("name")),
    }


def time_part(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = re.search(r"T(\d{2}:\d{2})", value)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{2}:\d{2})", value)
    return match.group(1) if match else ""


def text_matches(text: str, needle: str) -> bool:
    text_lower = text.lower()
    needle_lower = needle.lower()
    compact_text = re.sub(r"\s+", "", text_lower)
    compact_needle = re.sub(r"\s+", "", needle_lower)
    return needle_lower in text_lower or bool(compact_needle and compact_needle in compact_text)


def item_search_text(item: dict[str, Any]) -> str:
    parts = [
        clean_text(item.get("name")),
        clean_text(item.get("desc")),
        json.dumps(parse_jsonish(item.get("additionalPropertyJson")), ensure_ascii=False, sort_keys=True),
        json.dumps(item.get("bookingPrecautionJson") or [], ensure_ascii=False, sort_keys=True),
    ]
    return " ".join(part for part in parts if part)


def raw_key(*parts: Any) -> str:
    joined = "_".join(str(part) for part in parts if part not in (None, ""))
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", joined).strip("_") or "raw"


def is_too_many_requests_payload(data: dict[str, Any]) -> bool:
    errors = data.get("errors")
    if not errors:
        return False
    error_text = json.dumps(errors, ensure_ascii=False)
    return "BookingAPITooManyRequests" in error_text or "TooManyRequests" in error_text


class BookingGraphQLClient:
    def __init__(
        self,
        timeout: int = 10,
        user_agent: str | None = None,
        raw_dir: str | None = None,
        save_raw_dir: str | None = None,
        rate_limit_cooldown: float = 20.0,
        rate_limit_retries: int = 1,
        rate_limit_max_consecutive: int = 3,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.save_raw_dir = Path(save_raw_dir) if save_raw_dir else None
        self.rate_limit_cooldown = max(0.0, rate_limit_cooldown)
        self.rate_limit_retries = max(0, rate_limit_retries)
        self.rate_limit_max_consecutive = max(1, rate_limit_max_consecutive)
        self.consecutive_rate_limits = 0
        self.rate_limit_stopped = False
        self.session = requests.Session()
        self.session.trust_env = False

    def _save_raw(self, raw_name: str | None, data: dict[str, Any]) -> None:
        if self.save_raw_dir and raw_name:
            self.save_raw_dir.mkdir(parents=True, exist_ok=True)
            (self.save_raw_dir / f"{raw_name}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def _raise_rate_limited(self, errors: Any) -> None:
        self.consecutive_rate_limits += 1
        message = json.dumps(errors, ensure_ascii=False) if errors else "BookingAPITooManyRequests"
        if self.consecutive_rate_limits >= self.rate_limit_max_consecutive:
            self.rate_limit_stopped = True
            raise BookingRateLimitStopped(
                f"{message} (stopped after {self.consecutive_rate_limits} consecutive rate limits)"
            )
        raise BookingTooManyRequests(message)

    def _post_graphql(
        self,
        operation_name: str,
        query: str,
        variables: dict[str, Any],
        referer: str | None,
    ) -> dict[str, Any]:
        headers = build_booking_headers(self.user_agent, referer)
        self.session.cookies.clear()
        try:
            response = self.session.post(
                BOOKING_GRAPHQL_URL,
                headers=headers,
                json={"operationName": operation_name, "query": query, "variables": variables},
                timeout=self.timeout,
                allow_redirects=False,
            )
        finally:
            self.session.cookies.clear()
        if 300 <= response.status_code < 400:
            raise requests.HTTPError(
                f"unexpected Booking GraphQL redirect (HTTP {response.status_code})",
                response=response,
            )
        if response.status_code == 429:
            try:
                data = response.json()
            except ValueError:
                data = {"errors": [{"message": "BookingAPITooManyRequests", "status": 429}]}
            self._raise_rate_limited(data.get("errors"))
        response.raise_for_status()
        return response.json()

    def graphql(
        self,
        operation_name: str,
        query: str,
        variables: dict[str, Any],
        referer: str | None = None,
        raw_name: str | None = None,
    ) -> dict[str, Any]:
        if self.raw_dir and raw_name:
            raw_path = self.raw_dir / f"{raw_name}.json"
            data = json.loads(raw_path.read_text(encoding="utf-8"))
        else:
            attempts = self.rate_limit_retries + 1
            for attempt in range(attempts):
                try:
                    data = self._post_graphql(operation_name, query, variables, referer)
                    if not isinstance(data, dict):
                        raise BookingGraphQLError("네이버 예약 GraphQL 응답이 객체가 아닙니다.")
                    self._save_raw(raw_name, data)
                    if is_too_many_requests_payload(data):
                        self._raise_rate_limited(data.get("errors"))
                    break
                except BookingTooManyRequests:
                    if self.rate_limit_stopped or attempt >= attempts - 1:
                        raise
                    if self.rate_limit_cooldown:
                        time.sleep(self.rate_limit_cooldown)
            else:
                raise BookingGraphQLError("네이버 예약 GraphQL 응답을 받지 못했습니다.")

        if not isinstance(data, dict):
            raise BookingGraphQLError("네이버 예약 GraphQL 응답이 객체가 아닙니다.")
        errors = data.get("errors")
        if errors:
            raise BookingGraphQLError(json.dumps(errors, ensure_ascii=False))
        self.consecutive_rate_limits = 0
        return data

    def biz_items(self, business_id: str, lang: str = "ko") -> list[dict[str, Any]]:
        variables = {
            "input": {
                "businessId": str(business_id),
                "lang": lang,
                "projections": "RESOURCE, MIN_MAX_PRICE, AVAILABLE_START_DATE, TYPE_VALUE",
            }
        }
        data = self.graphql("bizItems", BIZ_ITEMS_QUERY, variables, raw_name=raw_key(business_id, "bizItems"))
        root = data.get("data")
        if not isinstance(root, dict) or "bizItems" not in root:
            raise BookingGraphQLError("bizItems 응답에 data.bizItems가 없습니다.")
        items = root.get("bizItems")
        if items is None:
            return []
        if not isinstance(items, list):
            raise BookingGraphQLError("bizItems 응답의 data.bizItems가 배열이 아닙니다.")
        return items

    def search_biz_items(
        self,
        business_id: str,
        available_start_date: str,
        available_end_date: str,
    ) -> list[dict[str, Any]]:
        variables = {
            "bizItemSearchParams": {
                "businessId": str(business_id),
                "projections": "resource,min-max-price,type-value",
                "isAvailableBooking": False,
                "availableStartDate": available_start_date,
                "availableEndDate": available_end_date,
            }
        }
        data = self.graphql(
            "searchBizItem",
            SEARCH_BIZ_ITEM_QUERY,
            variables,
            raw_name=raw_key(business_id, "search", available_start_date, available_end_date),
        )
        root = data.get("data")
        search_root = root.get("searchBizItem") if isinstance(root, dict) else None
        if not isinstance(search_root, dict) or "bizItems" not in search_root:
            raise BookingGraphQLError(
                "searchBizItem 응답에 data.searchBizItem.bizItems가 없습니다."
            )
        items = search_root.get("bizItems")
        if items is None:
            return []
        if not isinstance(items, list):
            raise BookingGraphQLError("searchBizItem의 bizItems가 배열이 아닙니다.")
        return items

    def biz_item(self, business_id: str, biz_item_id: str, lang: str = "ko") -> dict[str, Any]:
        variables = {
            "input": {
                "businessId": str(business_id),
                "bizItemId": str(biz_item_id),
                "lang": lang,
                "projections": "RESOURCE, MIN_MAX_PRICE, AVAILABLE_START_DATE, TYPE_VALUE, BIZ_ITEM_DETAIL",
            }
        }
        data = self.graphql(
            "bizItem",
            BIZ_ITEM_DETAIL_QUERY,
            variables,
            raw_name=raw_key(business_id, biz_item_id, "detail"),
        )
        root = data.get("data")
        if not isinstance(root, dict) or "bizItem" not in root:
            raise BookingGraphQLError("bizItem 응답에 data.bizItem이 없습니다.")
        item = root.get("bizItem")
        if item is None:
            return {}
        if not isinstance(item, dict):
            raise BookingGraphQLError("bizItem 응답의 data.bizItem이 객체가 아닙니다.")
        return item

    def daily_schedule(
        self,
        business_id: str,
        business_type_id: int,
        biz_item_id: str,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        variables = {
            "scheduleParams": {
                "businessId": str(business_id),
                "businessTypeId": int(business_type_id),
                "bizItemId": str(biz_item_id),
                "startDateTime": f"{start_date}T00:00:00",
                "endDateTime": f"{end_date}T00:00:00",
            }
        }
        data = self.graphql(
            "schedule",
            DAILY_SCHEDULE_QUERY,
            variables,
            raw_name=raw_key(business_id, biz_item_id, "daily", start_date, end_date),
        )
        root = data.get("data")
        if not isinstance(root, dict) or "schedule" not in root:
            raise BookingGraphQLError("schedule 응답에 data.schedule이 없습니다.")
        if root.get("schedule") is not None and not isinstance(root.get("schedule"), dict):
            raise BookingGraphQLError("schedule 응답의 data.schedule이 객체가 아닙니다.")
        return data

    def hourly_schedule(
        self,
        business_id: str,
        business_type_id: int,
        biz_item_id: str,
        schedule_date: str,
    ) -> dict[str, Any]:
        variables = {
            "scheduleParams": {
                "businessId": str(business_id),
                "businessTypeId": int(business_type_id),
                "bizItemId": str(biz_item_id),
                "startDateTime": f"{schedule_date}T00:00:00",
                "endDateTime": f"{schedule_date}T23:59:59",
                "fixedTime": True,
                "includesHolidaySchedules": True,
            }
        }
        data = self.graphql(
            "hourlySchedule",
            HOURLY_SCHEDULE_QUERY,
            variables,
            raw_name=raw_key(business_id, biz_item_id, "hourly", schedule_date),
        )
        root = data.get("data")
        if not isinstance(root, dict) or "schedule" not in root:
            raise BookingGraphQLError("hourlySchedule 응답에 data.schedule이 없습니다.")
        if root.get("schedule") is not None and not isinstance(root.get("schedule"), dict):
            raise BookingGraphQLError("hourlySchedule의 data.schedule이 객체가 아닙니다.")
        return data

    def options(
        self,
        business_id: str,
        biz_item_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
        lang: str = "ko",
    ) -> list[dict[str, Any]]:
        input_payload: dict[str, Any] = {"businessId": str(business_id), "bizItemId": str(biz_item_id), "lang": lang}
        if start_date:
            input_payload["startDate"] = start_date
        if end_date:
            input_payload["endDate"] = end_date
        data = self.graphql(
            "option",
            OPTION_QUERY,
            {"input": input_payload},
            raw_name=raw_key(business_id, biz_item_id, "option", start_date or "", end_date or ""),
        )
        root = data.get("data")
        if not isinstance(root, dict) or "option" not in root:
            raise BookingGraphQLError("option 응답에 data.option이 없습니다.")
        categories = root.get("option")
        if categories is None:
            return []
        if not isinstance(categories, list):
            raise BookingGraphQLError("option 응답의 data.option이 배열이 아닙니다.")
        return categories
