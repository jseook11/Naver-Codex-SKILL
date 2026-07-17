#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import requests

try:
    from .naver_booking_client import (
        ACCOMMODATION_BUSINESS_TYPE_ID,
        MAX_PERSONNEL_CODE,
        STANDARD_PERSONNEL_CODE,
        BookingGraphQLClient,
        BookingIds,
        build_business_url,
        build_item_url,
        clean_text,
        daily_summary_map,
        date_range,
        hourly_schedules,
        iso_now,
        item_search_text,
        normalize_options,
        normalize_resources,
        normalize_slot,
        parse_booking_url,
        parse_jsonish,
        safe_int,
        text_matches,
        time_part,
        type_value_map,
    )
    from .scrape_naver_map import build_mobile_map_headers, fetch_html, parse_map_html
except ImportError:  # Direct script execution.
    from naver_booking_client import (
        ACCOMMODATION_BUSINESS_TYPE_ID,
        MAX_PERSONNEL_CODE,
        STANDARD_PERSONNEL_CODE,
        BookingGraphQLClient,
        BookingIds,
        build_business_url,
        build_item_url,
        clean_text,
        daily_summary_map,
        date_range,
        hourly_schedules,
        iso_now,
        item_search_text,
        normalize_options,
        normalize_resources,
        normalize_slot,
        parse_booking_url,
        parse_jsonish,
        safe_int,
        text_matches,
        time_part,
        type_value_map,
    )
    from scrape_naver_map import build_mobile_map_headers, fetch_html, parse_map_html


def write_text(path: str, text: str) -> None:
    output_path = Path(path)
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="네이버 예약 객실/시간대 조회 결과를 JSON으로 출력합니다. 예약 제출은 하지 않습니다."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query", help="네이버 지도 검색어. 후보의 reservation_url을 찾아 예약 조회")
    source.add_argument("--booking-url", help="네이버 예약 URL")
    source.add_argument("--business-id", help="네이버 예약 businessId")
    parser.add_argument("--business-type-id", type=int, help="네이버 예약 businessTypeId. --business-id와 함께 필요")
    parser.add_argument("--check-in", help="숙박 체크인 날짜 YYYY-MM-DD")
    parser.add_argument("--check-out", help="숙박 체크아웃 날짜 YYYY-MM-DD")
    parser.add_argument("--date", dest="booking_date", help="시간예약 날짜 YYYY-MM-DD")
    parser.add_argument("--guests", type=int, default=1, help="인원 수")
    parser.add_argument("--time-from", help="시간예약 시작 필터 HH:MM")
    parser.add_argument("--time-to", help="시간예약 종료 필터 HH:MM")
    parser.add_argument("--available-only", action="store_true", help="예약 가능한 항목만 출력")
    parser.add_argument("--include-text", action="append", default=[], help="하위 호환용 item 포함 텍스트. 반복 가능")
    parser.add_argument("--exclude-text", action="append", default=[], help="하위 호환용 item 제외 텍스트. 반복 가능")
    parser.add_argument("--place-include-text", action="append", default=[], help="지도 후보에 포함해야 하는 텍스트. 반복 가능")
    parser.add_argument("--place-exclude-text", action="append", default=[], help="지도 후보에서 제외할 텍스트. 반복 가능")
    parser.add_argument("--item-include-text", action="append", default=[], help="예약 상품/객실/메뉴에 포함해야 하는 텍스트. 반복 가능")
    parser.add_argument("--item-exclude-text", action="append", default=[], help="예약 상품/객실/메뉴에서 제외할 텍스트. 반복 가능")
    parser.add_argument("--option-include-text", action="append", default=[], help="예약 옵션에 포함해야 하는 텍스트. 반복 가능")
    parser.add_argument("--option-exclude-text", action="append", default=[], help="예약 옵션에서 제외할 텍스트. 반복 가능")
    parser.add_argument("--limit", type=int, default=20, help="--query 후보 개수. 0 이하면 전체")
    parser.add_argument("--max-businesses", type=int, default=20, help="broad query에서 예약 조회할 고유 business 개수. 0 이하면 전체")
    parser.add_argument(
        "--query-mode",
        default="auto",
        choices=["auto", "broad", "specific"],
        help="--query 후보 처리 방식. auto는 near-exact 후보가 1~5개면 그 후보만 조회",
    )
    parser.add_argument(
        "--detail-mode",
        default="minimal",
        choices=["minimal", "full"],
        help="minimal은 상세 bizItem 조회를 생략하고, full은 살아남은 item만 상세 조회",
    )
    parser.add_argument("--output", help="JSON 저장 경로. 생략하면 stdout으로 출력")
    parser.add_argument("--raw-dir", help="저장된 GraphQL 원본 JSON 디렉터리만 사용")
    parser.add_argument("--save-raw-dir", help="조회한 GraphQL 원본 JSON을 저장할 디렉터리")
    parser.add_argument("--timeout", type=int, default=10, help="HTTP 요청 타임아웃 초")
    parser.add_argument("--rate-limit-cooldown", type=float, default=20.0, help=argparse.SUPPRESS)
    parser.add_argument(
        "--user-agent",
        "--ua",
        default="",
        help="HTTP User-Agent를 덮어씁니다. 생략하면 NAVER_MAP_USER_AGENT 환경변수 또는 기본 모바일 Safari UA를 사용합니다.",
    )
    parser.add_argument(
        "--sort",
        default="",
        choices=["relativity", "distance", ""],
        help="--query 지도 검색 정렬 파라미터입니다. 기본 요청에서는 비워둡니다.",
    )
    args = parser.parse_args(argv)

    if args.business_id and args.business_type_id is None:
        parser.error("--business-id는 --business-type-id와 함께 사용하세요.")
    if args.guests < 1:
        parser.error("--guests는 1 이상이어야 합니다.")

    has_stay = bool(args.check_in or args.check_out)
    if has_stay and not (args.check_in and args.check_out):
        parser.error("숙박 조회에는 --check-in과 --check-out이 모두 필요합니다.")
    if has_stay and args.booking_date:
        parser.error("숙박 조회(--check-in/--check-out)와 시간예약 조회(--date)는 동시에 사용할 수 없습니다.")
    if not has_stay and not args.booking_date:
        parser.error("--check-in/--check-out 또는 --date 중 하나가 필요합니다.")
    if has_stay:
        try:
            if date.fromisoformat(args.check_out) <= date.fromisoformat(args.check_in):
                parser.error("--check-out은 --check-in보다 늦어야 합니다.")
        except ValueError as exc:
            parser.error(f"날짜 형식 오류: {exc}")
    if args.booking_date:
        try:
            date.fromisoformat(args.booking_date)
        except ValueError as exc:
            parser.error(f"날짜 형식 오류: {exc}")
    return args


def request_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "query": args.query,
        "booking_url": args.booking_url,
        "business_id": args.business_id,
        "business_type_id": args.business_type_id,
        "check_in": args.check_in,
        "check_out": args.check_out,
        "date": args.booking_date,
        "guests": args.guests,
        "time_from": args.time_from,
        "time_to": args.time_to,
        "available_only": args.available_only,
        "include_text": args.include_text,
        "exclude_text": args.exclude_text,
        "place_include_text": args.place_include_text,
        "place_exclude_text": args.place_exclude_text,
        "item_include_text": args.item_include_text,
        "item_exclude_text": args.item_exclude_text,
        "option_include_text": args.option_include_text,
        "option_exclude_text": args.option_exclude_text,
        "limit": args.limit,
        "max_businesses": args.max_businesses,
        "query_mode": args.query_mode,
        "detail_mode": args.detail_mode,
    }


def query_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    headers = build_mobile_map_headers(args.user_agent)
    html, url = fetch_html(args.query, args.timeout, args.sort, headers)
    result = parse_map_html(html, args.query, url)
    places = result.places if args.limit <= 0 else result.places[: args.limit]
    return [
        {
            "place_id": place.id,
            "place_name": place.name,
            "category": place.category,
            "address": place.road_address or place.address,
            "reservation_url": place.reservation_url,
        }
        for place in places
    ]


def filter_terms(group: str) -> list[str]:
    return [term.strip() for term in group.split("|") if term.strip()]


def text_matches_group(text: str, group: str) -> bool:
    terms = filter_terms(group)
    return bool(terms) and any(text_matches(text, term) for term in terms)


def text_passes_filters(text: str, include_groups: list[str], exclude_groups: list[str]) -> bool:
    return all(text_matches_group(text, group) for group in include_groups) and not any(
        text_matches_group(text, group) for group in exclude_groups
    )


def item_include_text(args: argparse.Namespace) -> list[str]:
    return args.item_include_text + args.include_text


def item_exclude_text(args: argparse.Namespace) -> list[str]:
    return args.item_exclude_text + args.exclude_text


def has_option_filters(args: argparse.Namespace) -> bool:
    return bool(args.option_include_text or args.option_exclude_text)


def place_filter_text(candidate: dict[str, Any]) -> str:
    return " ".join(
        str(candidate.get(key) or "")
        for key in ("place_name", "category", "address", "reservation_url")
    )


def candidate_passes_place_filters(candidate: dict[str, Any], args: argparse.Namespace) -> bool:
    return text_passes_filters(place_filter_text(candidate), args.place_include_text, args.place_exclude_text)


def item_passes_filters(item: dict[str, Any], args: argparse.Namespace) -> bool:
    return text_passes_filters(item_search_text(item), item_include_text(args), item_exclude_text(args))


def option_text(category: dict[str, Any], option: dict[str, Any] | None = None) -> str:
    parts = [
        clean_text(category.get("name")),
        clean_text(category.get("categoryName")),
        clean_text(category.get("categoryTypeCode")),
        clean_text(category.get("selectionTypeCode")),
    ]
    if option:
        parts.extend(
            [
                clean_text(option.get("name")),
                clean_text(option.get("desc")),
                clean_text(option.get("priceDesc")),
                clean_text(option.get("categoryName")),
                clean_text(option.get("categoryTypeCode")),
            ]
        )
    return " ".join(part for part in parts if part)


def options_pass_filters(categories: list[dict[str, Any]], args: argparse.Namespace) -> bool:
    if not has_option_filters(args):
        return True
    for category in categories:
        if not isinstance(category, dict):
            continue
        if text_passes_filters(option_text(category), args.option_include_text, args.option_exclude_text):
            return True
        for option in category.get("options") or []:
            if isinstance(option, dict) and text_passes_filters(
                option_text(category, option), args.option_include_text, args.option_exclude_text
            ):
                return True
    return False


def normalized_match_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.lower())


def is_near_exact_match(query: str, place_name: str) -> bool:
    query_text = normalized_match_text(query)
    name_text = normalized_match_text(place_name)
    return bool(query_text and name_text) and (query_text == name_text or query_text in name_text or name_text in query_text)


def dedupe_and_limit_candidates(
    candidates: list[dict[str, Any]],
    args: argparse.Namespace,
    warnings: list[str],
    apply_business_limit: bool,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_businesses: set[tuple[int, str]] = set()
    business_count = 0
    max_businesses = args.max_businesses if apply_business_limit and args.max_businesses > 0 else None

    for candidate in candidates:
        reservation_url = candidate.get("reservation_url") or ""
        if not reservation_url:
            result.append(candidate)
            continue
        try:
            ids = parse_booking_url(reservation_url)
        except ValueError as exc:
            warnings.append(f"{candidate.get('place_name') or reservation_url}: invalid reservation_url: {exc}")
            result.append(candidate)
            continue
        key = (ids.business_type_id, ids.business_id)
        if key in seen_businesses:
            continue
        if max_businesses is not None and business_count >= max_businesses:
            continue
        seen_businesses.add(key)
        business_count += 1
        result.append(candidate)
    return result


def prepare_query_candidates(
    candidates: list[dict[str, Any]],
    args: argparse.Namespace,
    warnings: list[str],
) -> list[dict[str, Any]]:
    filtered = [candidate for candidate in candidates if candidate_passes_place_filters(candidate, args)]
    matches = [candidate for candidate in filtered if is_near_exact_match(args.query or "", candidate.get("place_name") or "")]

    if args.query_mode == "specific":
        selected = matches if matches else filtered
        return dedupe_and_limit_candidates(selected, args, warnings, apply_business_limit=False)
    if args.query_mode == "auto" and 1 <= len(matches) <= 5:
        return dedupe_and_limit_candidates(matches, args, warnings, apply_business_limit=False)
    return dedupe_and_limit_candidates(filtered, args, warnings, apply_business_limit=True)


def direct_candidate(args: argparse.Namespace) -> dict[str, Any]:
    if args.booking_url:
        ids = parse_booking_url(args.booking_url)
        return {
            "place_id": "",
            "place_name": "",
            "category": "",
            "address": "",
            "reservation_url": args.booking_url,
            "ids": ids,
        }
    assert args.business_id and args.business_type_id is not None
    return {
        "place_id": "",
        "place_name": "",
        "category": "",
        "address": "",
        "reservation_url": build_business_url(args.business_type_id, args.business_id),
        "ids": BookingIds(args.business_type_id, str(args.business_id), url=build_business_url(args.business_type_id, args.business_id)),
    }


def normalize_accommodation_item(
    item: dict[str, Any],
    schedule_payload: dict[str, Any],
    option_categories: list[dict[str, Any]],
    ids: BookingIds,
    check_in: str,
    check_out: str,
    guests: int,
) -> dict[str, Any]:
    type_values = type_value_map(item)
    capacity_standard = safe_int(type_values.get(STANDARD_PERSONNEL_CODE))
    capacity_max = safe_int(type_values.get(MAX_PERSONNEL_CODE)) or safe_int(item.get("maxBookingCount"))
    price_by_dates = parse_jsonish(item.get("priceByDates")) or {}
    if not isinstance(price_by_dates, dict):
        price_by_dates = {}
    stay_dates = date_range(check_in, check_out)
    summaries = daily_summary_map(schedule_payload)
    available_units_by_date: dict[str, int | None] = {}
    reasons: list[str] = []
    nights = len(stay_dates)

    if item.get("isClosedBooking") and not item.get("isClosedBookingUser"):
        reasons.append("closed_booking")
    if capacity_max is not None and guests > capacity_max:
        reasons.append("capacity_lt_guests")
    min_booking_time = safe_int(item.get("minBookingTime"))
    max_booking_time = safe_int(item.get("maxBookingTime"))
    if min_booking_time and nights < min_booking_time:
        reasons.append("below_min_booking_nights")
    if max_booking_time and nights > max_booking_time:
        reasons.append("above_max_booking_nights")

    for day in stay_dates:
        if day not in price_by_dates or price_by_dates.get(day) in (None, ""):
            reasons.append(f"missing_price_date:{day}")
        summary = summaries.get(day)
        if not summary:
            reasons.append(f"missing_schedule_date:{day}")
            available_units_by_date[day] = None
            continue
        if summary.get("isBusinessDay") is False:
            reasons.append(f"not_business_day:{day}")
        if summary.get("isSaleDay") is False:
            reasons.append(f"not_sale_day:{day}")
        prices = summary.get("prices") or []
        if not prices:
            reasons.append(f"no_schedule_price:{day}")
        stock = safe_int(summary.get("stock"))
        booking_count = safe_int(summary.get("bookingCount")) or 0
        if stock is None:
            available_units_by_date[day] = None
        else:
            remain = max(0, stock - booking_count)
            available_units_by_date[day] = remain
            if remain < 1:
                reasons.append(f"sold_out:{day}")

    additional = parse_jsonish(item.get("additionalPropertyJson")) or {}
    if not isinstance(additional, dict):
        additional = {}
    accommodation = parse_jsonish(additional.get("accommodationAdditionalProperty")) or {}
    if not isinstance(accommodation, dict):
        accommodation = {}

    biz_item_id = str(item.get("bizItemId") or item.get("id") or "")
    return {
        "biz_item_id": biz_item_id,
        "name": clean_text(item.get("name")),
        "is_available": not reasons,
        "reasons": sorted(set(reasons)),
        "capacity_standard": capacity_standard,
        "capacity_max": capacity_max,
        "price_by_date": {day: price_by_dates.get(day) for day in stay_dates},
        "available_units_by_date": available_units_by_date,
        "check_in_time": accommodation.get("checkInTime"),
        "check_out_time": accommodation.get("checkOutTime"),
        "detail_url": build_item_url(ids.business_type_id, ids.business_id, biz_item_id, check_in, check_out),
        "description": clean_text(item.get("desc")),
        "images": normalize_resources(item),
        "options": normalize_options(option_categories),
    }


def normalize_time_item(
    item: dict[str, Any],
    schedule_payload: dict[str, Any],
    option_categories: list[dict[str, Any]],
    ids: BookingIds,
    booking_date: str,
    guests: int,
    time_from: str | None,
    time_to: str | None,
) -> dict[str, Any]:
    slots = []
    for slot in hourly_schedules(schedule_payload):
        normalized = normalize_slot(slot, guests)
        slot_time = normalized.get("start_time") or time_part(normalized.get("start_date_time"))
        if time_from and slot_time and slot_time < time_from:
            continue
        if time_to and slot_time and slot_time > time_to:
            continue
        if normalized["is_available"]:
            slots.append(normalized)

    biz_item_id = str(item.get("bizItemId") or item.get("id") or "")
    reasons = [] if slots else ["no_available_slots"]
    return {
        "biz_item_id": biz_item_id,
        "name": clean_text(item.get("name")),
        "is_available": bool(slots),
        "reasons": reasons,
        "available_slots": slots,
        "detail_url": build_item_url(ids.business_type_id, ids.business_id, biz_item_id),
        "description": clean_text(item.get("desc")),
        "images": normalize_resources(item),
        "options": normalize_options(option_categories),
        "date": booking_date,
    }


def accommodation_basic_reasons(
    item: dict[str, Any],
    check_in: str,
    check_out: str,
    guests: int,
) -> list[str]:
    reasons: list[str] = []
    type_values = type_value_map(item)
    capacity_max = safe_int(type_values.get(MAX_PERSONNEL_CODE)) or safe_int(item.get("maxBookingCount"))
    price_by_dates = parse_jsonish(item.get("priceByDates")) or {}
    if not isinstance(price_by_dates, dict):
        price_by_dates = {}
    nights = len(date_range(check_in, check_out))

    if item.get("isClosedBooking") and not item.get("isClosedBookingUser"):
        reasons.append("closed_booking")
    if capacity_max is not None and guests > capacity_max:
        reasons.append("capacity_lt_guests")
    min_booking_time = safe_int(item.get("minBookingTime"))
    max_booking_time = safe_int(item.get("maxBookingTime"))
    if min_booking_time and nights < min_booking_time:
        reasons.append("below_min_booking_nights")
    if max_booking_time and nights > max_booking_time:
        reasons.append("above_max_booking_nights")
    for day in date_range(check_in, check_out):
        if day not in price_by_dates or price_by_dates.get(day) in (None, ""):
            reasons.append(f"missing_price_date:{day}")
    return sorted(set(reasons))


def time_basic_reasons(item: dict[str, Any], guests: int) -> list[str]:
    reasons: list[str] = []
    if item.get("isClosedBooking") and not item.get("isClosedBookingUser"):
        reasons.append("closed_booking")
    capacity_max = safe_int(item.get("maxBookingCount"))
    if capacity_max is not None and guests > capacity_max:
        reasons.append("capacity_lt_guests")
    return sorted(set(reasons))


def unavailable_accommodation_item(
    item: dict[str, Any],
    ids: BookingIds,
    check_in: str,
    check_out: str,
    reasons: list[str],
    option_categories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    type_values = type_value_map(item)
    capacity_standard = safe_int(type_values.get(STANDARD_PERSONNEL_CODE))
    capacity_max = safe_int(type_values.get(MAX_PERSONNEL_CODE)) or safe_int(item.get("maxBookingCount"))
    price_by_dates = parse_jsonish(item.get("priceByDates")) or {}
    if not isinstance(price_by_dates, dict):
        price_by_dates = {}
    additional = parse_jsonish(item.get("additionalPropertyJson")) or {}
    if not isinstance(additional, dict):
        additional = {}
    accommodation = parse_jsonish(additional.get("accommodationAdditionalProperty")) or {}
    if not isinstance(accommodation, dict):
        accommodation = {}
    stay_dates = date_range(check_in, check_out)
    biz_item_id = str(item.get("bizItemId") or item.get("id") or "")
    return {
        "biz_item_id": biz_item_id,
        "name": clean_text(item.get("name")),
        "is_available": False,
        "reasons": sorted(set(reasons)),
        "capacity_standard": capacity_standard,
        "capacity_max": capacity_max,
        "price_by_date": {day: price_by_dates.get(day) for day in stay_dates},
        "available_units_by_date": {day: None for day in stay_dates},
        "check_in_time": accommodation.get("checkInTime"),
        "check_out_time": accommodation.get("checkOutTime"),
        "detail_url": build_item_url(ids.business_type_id, ids.business_id, biz_item_id, check_in, check_out),
        "description": clean_text(item.get("desc")),
        "images": normalize_resources(item),
        "options": normalize_options(option_categories),
    }


def unavailable_time_item(
    item: dict[str, Any],
    ids: BookingIds,
    booking_date: str,
    reasons: list[str],
    option_categories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    biz_item_id = str(item.get("bizItemId") or item.get("id") or "")
    return {
        "biz_item_id": biz_item_id,
        "name": clean_text(item.get("name")),
        "is_available": False,
        "reasons": sorted(set(reasons)),
        "available_slots": [],
        "detail_url": build_item_url(ids.business_type_id, ids.business_id, biz_item_id),
        "description": clean_text(item.get("desc")),
        "images": normalize_resources(item),
        "options": normalize_options(option_categories),
        "date": booking_date,
    }


def apply_filters(items: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    return [item for item in items if not args.available_only or item.get("is_available") is True]


def merge_item(base: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in detail.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def explore_accommodation(
    client: BookingGraphQLClient,
    ids: BookingIds,
    args: argparse.Namespace,
    warnings: list[str],
) -> dict[str, Any]:
    assert args.check_in and args.check_out
    items = client.search_biz_items(ids.business_id, args.check_in, args.check_out)
    if ids.biz_item_id:
        items = [item for item in items if str(item.get("bizItemId") or item.get("id")) == ids.biz_item_id] or [
            {"bizItemId": ids.biz_item_id}
        ]

    normalized_items = []
    for item in items:
        biz_item_id = str(item.get("bizItemId") or item.get("id") or "")
        if not biz_item_id:
            continue
        if args.detail_mode != "full" and not item_passes_filters(item, args):
            continue
        detail: dict[str, Any] = {}
        merged = item
        if args.detail_mode == "full" or (ids.biz_item_id and not item.get("name")):
            try:
                detail = client.biz_item(ids.business_id, biz_item_id)
            except (requests.RequestException, OSError, ValueError) as exc:
                warnings.append(f"{ids.business_id}/{biz_item_id}: detail fetch failed: {exc}")
            merged = merge_item(item, detail)
        if not item_passes_filters(merged, args):
            continue
        basic_reasons = accommodation_basic_reasons(merged, args.check_in, args.check_out, args.guests)
        if basic_reasons:
            normalized_items.append(
                unavailable_accommodation_item(merged, ids, args.check_in, args.check_out, basic_reasons)
            )
            continue

        option_categories: list[dict[str, Any]] = []
        if has_option_filters(args):
            try:
                option_categories = client.options(ids.business_id, biz_item_id, args.check_in, args.check_out)
            except (requests.RequestException, OSError, ValueError) as exc:
                warnings.append(f"{ids.business_id}/{biz_item_id}: option fetch failed: {exc}")
                normalized_items.append(
                    unavailable_accommodation_item(
                        merged,
                        ids,
                        args.check_in,
                        args.check_out,
                        ["option_fetch_failed"],
                    )
                )
                continue
            if not option_categories or not options_pass_filters(option_categories, args):
                normalized_items.append(
                    unavailable_accommodation_item(
                        merged,
                        ids,
                        args.check_in,
                        args.check_out,
                        ["option_filter_mismatch"],
                        option_categories,
                    )
                )
                continue

        schedule: dict[str, Any] = {}
        try:
            schedule = client.daily_schedule(
                ids.business_id,
                ids.business_type_id,
                biz_item_id,
                args.check_in,
                args.check_out,
            )
        except (requests.RequestException, OSError, ValueError) as exc:
            warnings.append(f"{ids.business_id}/{biz_item_id}: schedule fetch failed: {exc}")

        normalized = normalize_accommodation_item(
            merged,
            schedule,
            option_categories,
            ids,
            args.check_in,
            args.check_out,
            args.guests,
        )
        normalized_items.append(normalized)

    visible_items = apply_filters(normalized_items, args)
    return {
        "business_id": ids.business_id,
        "business_type_id": ids.business_type_id,
        "kind": "accommodation",
        "status": "ok",
        "items": visible_items,
    }


def explore_time_booking(
    client: BookingGraphQLClient,
    ids: BookingIds,
    args: argparse.Namespace,
    warnings: list[str],
) -> dict[str, Any]:
    assert args.booking_date
    items = client.biz_items(ids.business_id)
    if ids.biz_item_id:
        items = [item for item in items if str(item.get("bizItemId") or item.get("id")) == ids.biz_item_id] or [
            {"bizItemId": ids.biz_item_id}
        ]

    normalized_items = []
    for item in items:
        biz_item_id = str(item.get("bizItemId") or item.get("id") or "")
        if not biz_item_id:
            continue
        if args.detail_mode != "full" and not item_passes_filters(item, args):
            continue
        detail: dict[str, Any] = {}
        merged = item
        if args.detail_mode == "full" or (ids.biz_item_id and not item.get("name")):
            try:
                detail = client.biz_item(ids.business_id, biz_item_id)
            except (requests.RequestException, OSError, ValueError) as exc:
                warnings.append(f"{ids.business_id}/{biz_item_id}: detail fetch failed: {exc}")
            merged = merge_item(item, detail)
        if not item_passes_filters(merged, args):
            continue
        basic_reasons = time_basic_reasons(merged, args.guests)
        if basic_reasons:
            normalized_items.append(unavailable_time_item(merged, ids, args.booking_date, basic_reasons))
            continue

        option_categories: list[dict[str, Any]] = []
        if has_option_filters(args):
            try:
                option_categories = client.options(ids.business_id, biz_item_id)
            except (requests.RequestException, OSError, ValueError) as exc:
                warnings.append(f"{ids.business_id}/{biz_item_id}: option fetch failed: {exc}")
                normalized_items.append(unavailable_time_item(merged, ids, args.booking_date, ["option_fetch_failed"]))
                continue
            if not option_categories or not options_pass_filters(option_categories, args):
                normalized_items.append(
                    unavailable_time_item(merged, ids, args.booking_date, ["option_filter_mismatch"], option_categories)
                )
                continue

        schedule: dict[str, Any] = {}
        try:
            schedule = client.hourly_schedule(ids.business_id, ids.business_type_id, biz_item_id, args.booking_date)
        except (requests.RequestException, OSError, ValueError) as exc:
            warnings.append(f"{ids.business_id}/{biz_item_id}: hourly schedule fetch failed: {exc}")

        normalized = normalize_time_item(
            merged,
            schedule,
            option_categories,
            ids,
            args.booking_date,
            args.guests,
            args.time_from,
            args.time_to,
        )
        normalized_items.append(normalized)

    visible_items = apply_filters(normalized_items, args)
    return {
        "business_id": ids.business_id,
        "business_type_id": ids.business_type_id,
        "kind": "time_booking",
        "status": "ok",
        "items": visible_items,
    }


def resolve_ids(candidate: dict[str, Any], args: argparse.Namespace) -> BookingIds:
    if candidate.get("ids"):
        return candidate["ids"]
    reservation_url = candidate.get("reservation_url") or ""
    if reservation_url:
        return parse_booking_url(reservation_url)
    if args.business_id and args.business_type_id is not None:
        return BookingIds(args.business_type_id, str(args.business_id), url=build_business_url(args.business_type_id, args.business_id))
    raise ValueError("reservation_url이 없습니다.")


def explore_candidate(
    candidate: dict[str, Any],
    client: BookingGraphQLClient,
    args: argparse.Namespace,
    warnings: list[str],
) -> dict[str, Any]:
    place = {
        "place_id": candidate.get("place_id") or "",
        "place_name": candidate.get("place_name") or "",
        "category": candidate.get("category") or "",
        "address": candidate.get("address") or "",
        "reservation_url": candidate.get("reservation_url") or "",
        "booking": {},
    }
    if not place["reservation_url"] and not candidate.get("ids"):
        place["booking"] = {"status": "no_reservation_url", "items": []}
        return place

    try:
        ids = resolve_ids(candidate, args)
    except ValueError as exc:
        place["booking"] = {"status": "invalid_reservation_url", "error": str(exc), "items": []}
        warnings.append(f"{place['place_name'] or place['reservation_url']}: invalid reservation_url: {exc}")
        return place

    try:
        if args.check_in and args.check_out:
            place["booking"] = explore_accommodation(client, ids, args, warnings)
        else:
            place["booking"] = explore_time_booking(client, ids, args, warnings)
    except (requests.RequestException, OSError, ValueError) as exc:
        place["booking"] = {
            "business_id": ids.business_id,
            "business_type_id": ids.business_type_id,
            "status": "error",
            "error": str(exc),
            "items": [],
        }
        warnings.append(f"{place['place_name'] or ids.business_id}: booking fetch failed: {exc}")
    return place


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    warnings: list[str] = []

    try:
        if args.query:
            candidates = prepare_query_candidates(query_candidates(args), args, warnings)
        else:
            candidates = [direct_candidate(args)]
    except (requests.RequestException, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    client = BookingGraphQLClient(
        timeout=args.timeout,
        user_agent=args.user_agent,
        raw_dir=args.raw_dir,
        save_raw_dir=args.save_raw_dir,
        rate_limit_cooldown=args.rate_limit_cooldown,
    )
    places = []
    for candidate in candidates:
        places.append(explore_candidate(candidate, client, args, warnings))
        if client.rate_limit_stopped:
            warnings.append("booking batch stopped after consecutive TooManyRequests responses")
            break
    payload = {
        "source": "naver_booking_exploration",
        "fetched_at": iso_now(),
        "request": request_payload(args),
        "places": places,
        "warnings": warnings,
    }
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        write_text(args.output, json_text + "\n")
    else:
        print(json_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
