"""Composable, read-only Naver Booking availability capability.

The public Booking GraphQL shapes and proven normalization helpers live in the
internal compatibility package shared with the historical CLI. This adapter
adds bounded execution, typed partial results, conservative availability
semantics, and agent-sized output views.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests

from naver_place.contracts import (
    BudgetExceeded,
    CapabilityError,
    CapabilityResult,
    Completeness,
    ErrorCode,
    Provenance,
    RequestBudget,
    RequestPolicy,
    Status,
)
from naver_place.transport import Transport

from .map import search_places


from naver_place._legacy.naver_booking_client import (
    BookingGraphQLError,
    BookingGraphQLClient,
    BookingIds,
    BookingRateLimitStopped,
    BookingTooManyRequests,
    build_business_url,
    build_item_url,
    clean_text,
    hourly_schedules,
    normalize_options,
    normalize_resources,
    normalize_slot,
    parse_booking_url,
    safe_int,
    time_part,
)
from naver_place._legacy.scrape_naver_booking import (
    accommodation_basic_reasons,
    has_option_filters,
    item_passes_filters,
    merge_item,
    normalize_accommodation_item,
    options_pass_filters,
    prepare_query_candidates,
    time_basic_reasons,
    unavailable_accommodation_item,
    unavailable_time_item,
)


CAPABILITY = "booking.availability"
SOURCE = "naver-booking-public"
OPERATION = "booking.graphql"
MAP_OPERATION = "map.search_html"
FIXTURE_OPERATION = "fixture.replay"
VIEWS = {"compact", "standard", "full"}
DETAIL_MODES = {"minimal", "full"}
HHMM_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")

CandidateProvider = Callable[..., Sequence[Mapping[str, Any]]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fixture_capture_timestamp(raw_dir: str | Path) -> str | None:
    path = Path(raw_dir) / "fixture-metadata.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, Mapping):
        return None
    raw_capture = value.get("captured_at") or value.get("capture_date")
    if not isinstance(raw_capture, str) or not raw_capture.strip():
        return None
    capture = raw_capture.strip()
    try:
        if len(capture) == 10:
            return f"{date.fromisoformat(capture).isoformat()}T00:00:00+00:00"
        parsed = datetime.fromisoformat(capture.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is not None else None


def _code_value(code: Any) -> str:
    return getattr(code, "value", str(code))


def _invalid(request: Mapping[str, Any], message: str) -> CapabilityResult:
    error = CapabilityError(
        code=ErrorCode.INVALID_INPUT,
        message=message,
        operation=OPERATION,
    )
    return CapabilityResult(
        capability=CAPABILITY,
        request=request,
        data={},
        status=Status.ERROR,
        errors=(error,),
        completeness=Completeness(complete=False, stop_reason="invalid_input"),
    )


def _error_from_exception(exc: BaseException, operation: str = OPERATION) -> CapabilityError:
    if isinstance(exc, BudgetExceeded):
        return exc.to_error(operation)
    if isinstance(exc, json.JSONDecodeError):
        return CapabilityError(
            code=ErrorCode.UPSTREAM_CHANGED,
            message="public Booking response was not valid JSON",
            operation=operation,
        )
    if isinstance(exc, (BookingRateLimitStopped, BookingTooManyRequests)):
        return CapabilityError(
            code=ErrorCode.RATE_LIMITED,
            message=str(exc),
            operation=operation,
            retryable=True,
        )
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            code = ErrorCode.RATE_LIMITED
        elif status in {401, 403}:
            code = ErrorCode.BLOCKED
        elif status == 405:
            code = ErrorCode.UPSTREAM_REJECTED
        elif status == 404:
            code = ErrorCode.NOT_FOUND
        elif status and status >= 500:
            code = ErrorCode.NETWORK_ERROR
        else:
            code = ErrorCode.UPSTREAM_REJECTED
        return CapabilityError(
            code=code,
            message=str(exc),
            operation=operation,
            http_status=status,
            retryable=status == 429 or bool(status and status >= 500),
        )
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, requests.RequestException, OSError)):
        return CapabilityError(
            code=ErrorCode.NETWORK_ERROR,
            message=str(exc),
            operation=operation,
            retryable=isinstance(exc, (requests.Timeout, requests.ConnectionError)),
        )
    if isinstance(exc, BookingGraphQLError):
        return CapabilityError(
            code=ErrorCode.UPSTREAM_CHANGED,
            message=str(exc),
            operation=operation,
        )
    return CapabilityError(
        code=ErrorCode.UPSTREAM_CHANGED,
        message=str(exc),
        operation=operation,
    )


def _with_details(error: CapabilityError, **details: Any) -> CapabilityError:
    existing = getattr(error, "detail", {}) or {}
    return CapabilityError(
        code=error.code,
        message=error.message,
        operation=error.operation,
        http_status=error.http_status,
        retryable=error.retryable,
        detail={**dict(existing), **details},
    )


def _validate_call_result(method: str, result: Any) -> Any:
    if method in {"biz_items", "search_biz_items", "options"}:
        if not isinstance(result, list):
            raise BookingGraphQLError(f"{method} returned an unexpected response shape")
        if any(not isinstance(item, Mapping) for item in result):
            raise BookingGraphQLError(f"{method} returned a non-object list item")
    elif method == "biz_item":
        if not isinstance(result, Mapping):
            raise BookingGraphQLError("biz_item returned an unexpected response shape")
    elif method in {"daily_schedule", "hourly_schedule"}:
        data = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(data, Mapping) or "schedule" not in data:
            raise BookingGraphQLError(f"{method} response is missing data.schedule")
        schedule = data.get("schedule")
        if not isinstance(schedule, Mapping):
            raise BookingGraphQLError(f"{method} data.schedule has an unexpected shape")
        if isinstance(schedule, Mapping):
            item_schedule = schedule.get("bizItemSchedule")
            if not isinstance(item_schedule, Mapping):
                raise BookingGraphQLError(
                    f"{method} data.schedule.bizItemSchedule has an unexpected shape"
                )
            if isinstance(item_schedule, Mapping):
                field = "daily" if method == "daily_schedule" else "hourly"
                if field not in item_schedule:
                    raise BookingGraphQLError(
                        f"{method} response is missing {field} schedules"
                    )
                entries = item_schedule.get(field)
                allowed = (list, dict) if field == "daily" else (list,)
                if entries is not None and not isinstance(entries, allowed):
                    raise BookingGraphQLError(
                        f"{method} {field} schedules have an unexpected shape"
                    )
                if isinstance(entries, list) and any(
                    not isinstance(item, Mapping) for item in entries
                ):
                    raise BookingGraphQLError(
                        f"{method} {field} contains a non-object schedule"
                    )
    return result


def _has_requested_item_evidence(detail: Any, biz_item_id: str) -> bool:
    if not isinstance(detail, Mapping) or not detail:
        return False
    returned_id = str(detail.get("bizItemId") or detail.get("id") or "")
    if returned_id and returned_id != biz_item_id:
        return False
    return any(value not in (None, "", [], {}) for value in detail.values())


class _Calls:
    """Apply one invocation budget at the capability/client boundary."""

    def __init__(
        self,
        client: Any,
        budget: RequestBudget,
        *,
        offline: bool,
        timestamp: str,
        replayed_at: str | None = None,
    ) -> None:
        self.client = client
        self.budget = budget
        self.offline = offline
        self.timestamp = timestamp
        self.replayed_at = replayed_at
        self.provenance: list[Provenance] = []

    def call(self, method: str, *args: Any, detail: Mapping[str, Any] | None = None) -> Any:
        self.budget.check()
        if not self.offline:
            self.budget.consume()
        provenance_detail = dict(detail or {})
        if self.offline:
            provenance_detail["replays"] = OPERATION
            if self.replayed_at:
                provenance_detail["replayed_at"] = self.replayed_at
        previous_timeout = getattr(self.client, "timeout", None)
        timeout_adjusted = not self.offline and isinstance(previous_timeout, (int, float))
        if timeout_adjusted:
            self.client.timeout = min(
                float(previous_timeout),
                max(0.001, self.budget.elapsed_remaining_seconds / 2),
            )
        try:
            with self.budget.deadline():
                result = _validate_call_result(
                    method, getattr(self.client, method)(*args)
                )
        except Exception as exc:
            self.provenance.append(
                Provenance(
                    source="fixture" if self.offline else SOURCE,
                    operation=FIXTURE_OPERATION if self.offline else OPERATION,
                    fetched_at=self.timestamp,
                    live=not self.offline,
                    detail={
                        **provenance_detail,
                        "outcome": "error",
                        "exception": exc.__class__.__name__,
                    },
                )
            )
            try:
                self.budget.check()
            except BudgetExceeded as deadline:
                raise deadline from exc
            if self.offline and isinstance(exc, OSError):
                raise ValueError(
                    "required offline booking fixture is missing or unreadable"
                ) from exc
            raise
        finally:
            if timeout_adjusted:
                self.client.timeout = previous_timeout
        self.provenance.append(
            Provenance(
                source="fixture" if self.offline else SOURCE,
                operation=FIXTURE_OPERATION if self.offline else OPERATION,
                fetched_at=self.timestamp,
                live=not self.offline,
                detail={**provenance_detail, "outcome": "ok"},
            )
        )
        return result


def _string_list(values: Iterable[str] | None) -> list[str]:
    return [str(value).strip() for value in values or () if str(value).strip()]


def _legacy_args(**values: Any) -> SimpleNamespace:
    defaults = {
        "include_text": [],
        "exclude_text": [],
        "place_include_text": [],
        "place_exclude_text": [],
        "item_include_text": [],
        "item_exclude_text": [],
        "option_include_text": [],
        "option_exclude_text": [],
        "available_only": False,
        "detail_mode": "minimal",
        "query_mode": "auto",
        "max_businesses": 10,
        "limit": 20,
        "sort": "",
        "user_agent": "",
        "timeout": 30,
        "query": None,
        "business_id": None,
        "business_type_id": None,
        "check_in": None,
        "check_out": None,
        "booking_date": None,
        "guests": 1,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _direct_candidate(
    booking_url: str | None,
    business_id: str | None,
    business_type_id: int | None,
) -> dict[str, Any]:
    if booking_url:
        ids = parse_booking_url(booking_url)
        return {
            "place_id": "",
            "place_name": "",
            "category": "",
            "address": "",
            "reservation_url": booking_url,
            "ids": ids,
        }
    assert business_id and business_type_id is not None
    url = build_business_url(business_type_id, business_id)
    return {
        "place_id": "",
        "place_name": "",
        "category": "",
        "address": "",
        "reservation_url": url,
        "ids": BookingIds(business_type_id, str(business_id), url=url),
    }


def _resolve_ids(candidate: Mapping[str, Any]) -> BookingIds:
    value = candidate.get("ids")
    if isinstance(value, BookingIds):
        return value
    reservation_url = str(candidate.get("reservation_url") or "")
    if not reservation_url:
        raise ValueError("reservation_url is missing")
    return parse_booking_url(reservation_url)


def _place_shell(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "place_id": str(candidate.get("place_id") or ""),
        "place_name": str(candidate.get("place_name") or ""),
        "category": str(candidate.get("category") or ""),
        "address": str(candidate.get("address") or ""),
        "reservation_url": str(candidate.get("reservation_url") or ""),
        "booking": {},
    }


def _unknown_accommodation(
    item: dict[str, Any],
    *,
    units: int,
    option_unverified: bool = False,
) -> dict[str, Any]:
    reasons = set(str(reason) for reason in item.get("reasons") or ())
    definite_false = {
        reason
        for reason in reasons
        if reason in {
            "closed_booking",
            "capacity_lt_guests",
            "below_min_booking_nights",
            "above_max_booking_nights",
            "option_filter_mismatch",
        }
        or reason.startswith("not_business_day:")
        or reason.startswith("not_sale_day:")
        or reason.startswith("sold_out:")
    }
    unknown: set[str] = set()
    if item.get("capacity_max") is None:
        unknown.add("unknown_capacity")
    inventory = item.get("available_units_by_date") or {}
    if not inventory or any(value is None for value in inventory.values()):
        unknown.add("unknown_inventory")
    for day, value in inventory.items():
        if isinstance(value, int) and value < units:
            definite_false.add(f"insufficient_inventory:{day}")
    if any(
        reason.startswith(("missing_schedule_date:", "missing_price_date:", "no_schedule_price:"))
        for reason in reasons
    ):
        unknown.add("schedule_incomplete")
    if option_unverified:
        unknown.add("option_unverified")
    reasons.update(definite_false)
    reasons.update(unknown)
    if definite_false:
        item["is_available"] = False
    elif unknown:
        item["is_available"] = None
    else:
        item["is_available"] = True
    item["reasons"] = sorted(reasons)
    return item


def _time_item(
    item: dict[str, Any],
    schedule: Mapping[str, Any] | None,
    option_categories: list[dict[str, Any]],
    ids: BookingIds,
    *,
    booking_date: str,
    guests: int,
    units: int,
    time_from: str | None,
    time_to: str | None,
    schedule_failed: bool,
    option_unverified: bool,
) -> dict[str, Any]:
    capacity_max = safe_int(item.get("maxBookingCount"))
    normalized_slots: list[dict[str, Any]] = []
    for raw_slot in hourly_schedules(dict(schedule or {})):
        normalized = normalize_slot(raw_slot, guests)
        slot_time = normalized.get("start_time") or time_part(normalized.get("start_date_time"))
        if time_from and slot_time and slot_time < time_from:
            continue
        if time_to and slot_time and slot_time > time_to:
            continue
        reasons: set[str] = set()
        remain_stock = normalized.get("remain_stock")
        if remain_stock is None:
            reasons.add("unknown_inventory")
            normalized["is_available"] = None
        elif remain_stock < units:
            reasons.add("insufficient_inventory")
            normalized["is_available"] = False
        if capacity_max is None:
            reasons.add("unknown_capacity")
            if normalized.get("is_available") is True:
                normalized["is_available"] = None
        elif guests > capacity_max:
            reasons.add("capacity_lt_guests")
            normalized["is_available"] = False
        normalized["reasons"] = sorted(reasons)
        normalized_slots.append(normalized)

    reasons: set[str] = set()
    states = [slot.get("is_available") for slot in normalized_slots]
    if schedule_failed:
        availability: bool | None = None
        reasons.add("schedule_incomplete")
    elif any(state is True for state in states):
        availability = True
    elif any(state is None for state in states):
        availability = None
        reasons.update(
            reason
            for slot in normalized_slots
            for reason in slot.get("reasons") or ()
        )
    else:
        availability = False
        reasons.add("no_available_slots")
    if option_unverified:
        reasons.add("option_unverified")
        if availability is True:
            availability = None

    biz_item_id = str(item.get("bizItemId") or item.get("id") or "")
    return {
        "biz_item_id": biz_item_id,
        "name": clean_text(item.get("name")),
        "is_available": availability,
        "reasons": sorted(reasons),
        "capacity_max": capacity_max,
        "available_slots": [
            slot for slot in normalized_slots if slot.get("is_available") is not False
        ],
        "detail_url": build_item_url(ids.business_type_id, ids.business_id, biz_item_id),
        "description": clean_text(item.get("desc")),
        "images": normalize_resources(item),
        "options": normalize_options(option_categories),
        "date": booking_date,
    }


def _view_item(item: Mapping[str, Any], kind: str, view: str) -> dict[str, Any]:
    if view == "full":
        return dict(item)
    if kind == "accommodation":
        compact = (
            "biz_item_id",
            "name",
            "is_available",
            "reasons",
            "price_by_date",
            "available_units_by_date",
            "detail_url",
        )
        standard = compact + (
            "capacity_standard",
            "capacity_max",
            "check_in_time",
            "check_out_time",
        )
    else:
        compact = (
            "biz_item_id",
            "name",
            "is_available",
            "reasons",
            "date",
            "available_slots",
            "detail_url",
        )
        standard = compact + ("capacity_max",)
    keys = compact if view == "compact" else standard
    # Standard/compact intentionally omit descriptions, image arrays and option
    # trees.  Those can dominate an agent context without affecting a decision.
    return {key: item.get(key) for key in keys}


def _explore_accommodation(
    calls: _Calls,
    ids: BookingIds,
    args: SimpleNamespace,
    *,
    units: int,
    view: str,
    errors: list[CapabilityError],
) -> dict[str, Any]:
    assert args.check_in and args.check_out
    items = calls.call(
        "search_biz_items",
        ids.business_id,
        args.check_in,
        args.check_out,
        detail={"operation_name": "searchBizItem", "business_id": ids.business_id},
    )
    observed_item_count = len(items)
    requested_item_missing = False
    if ids.biz_item_id:
        matches = [
            item
            for item in items
            if str(item.get("bizItemId") or item.get("id") or "") == ids.biz_item_id
        ]
        observed_item_count = len(matches)
        requested_item_missing = not matches
        items = matches or [{"bizItemId": ids.biz_item_id}]

    normalized_items: list[dict[str, Any]] = []
    for item in items:
        biz_item_id = str(item.get("bizItemId") or item.get("id") or "")
        if not biz_item_id:
            continue
        if args.detail_mode != "full" and not item_passes_filters(item, args):
            continue
        merged = item
        if args.detail_mode == "full" or view == "full" or (ids.biz_item_id and not item.get("name")):
            try:
                detail = calls.call(
                    "biz_item",
                    ids.business_id,
                    biz_item_id,
                    detail={"operation_name": "bizItem", "business_id": ids.business_id, "biz_item_id": biz_item_id},
                )
                if requested_item_missing and not _has_requested_item_evidence(
                    detail, biz_item_id
                ):
                    errors.append(
                        CapabilityError(
                            code=ErrorCode.NOT_FOUND,
                            message="the requested Booking item was not found",
                            operation=OPERATION,
                            detail={
                                "business_id": ids.business_id,
                                "biz_item_id": biz_item_id,
                            },
                        )
                    )
                    continue
                if requested_item_missing:
                    observed_item_count = 1
                merged = merge_item(item, detail)
            except (BudgetExceeded, requests.RequestException, OSError, ValueError) as exc:
                errors.append(_with_details(_error_from_exception(exc), business_id=ids.business_id, biz_item_id=biz_item_id))
                if requested_item_missing:
                    continue
        if not item_passes_filters(merged, args):
            continue

        basic_reasons = accommodation_basic_reasons(
            merged, args.check_in, args.check_out, args.guests
        )
        definite_basic = [
            reason
            for reason in basic_reasons
            if not reason.startswith("missing_price_date:")
        ]
        if definite_basic:
            normalized = unavailable_accommodation_item(
                merged,
                ids,
                args.check_in,
                args.check_out,
                definite_basic,
            )
            normalized_items.append(_unknown_accommodation(normalized, units=units))
            continue

        option_categories: list[dict[str, Any]] = []
        option_unverified = False
        should_fetch_options = has_option_filters(args) or view == "full"
        if should_fetch_options:
            try:
                option_categories = calls.call(
                    "options",
                    ids.business_id,
                    biz_item_id,
                    args.check_in,
                    args.check_out,
                    detail={"operation_name": "option", "business_id": ids.business_id, "biz_item_id": biz_item_id},
                )
            except (BudgetExceeded, requests.RequestException, OSError, ValueError) as exc:
                errors.append(_with_details(_error_from_exception(exc), business_id=ids.business_id, biz_item_id=biz_item_id))
                option_unverified = has_option_filters(args)
            if has_option_filters(args) and not option_unverified and (
                not option_categories or not options_pass_filters(option_categories, args)
            ):
                normalized = unavailable_accommodation_item(
                    merged,
                    ids,
                    args.check_in,
                    args.check_out,
                    ["option_filter_mismatch"],
                    option_categories,
                )
                normalized_items.append(_unknown_accommodation(normalized, units=units))
                continue

        schedule: dict[str, Any] = {}
        schedule_failed = False
        try:
            schedule = calls.call(
                "daily_schedule",
                ids.business_id,
                ids.business_type_id,
                biz_item_id,
                args.check_in,
                args.check_out,
                detail={"operation_name": "schedule", "business_id": ids.business_id, "biz_item_id": biz_item_id},
            )
        except Exception as exc:
            schedule_failed = True
            errors.append(_with_details(_error_from_exception(exc), business_id=ids.business_id, biz_item_id=biz_item_id))

        normalized = normalize_accommodation_item(
            merged,
            schedule,
            option_categories,
            ids,
            args.check_in,
            args.check_out,
            args.guests,
        )
        if schedule_failed:
            normalized.setdefault("reasons", []).append("schedule_incomplete")
        if basic_reasons:
            normalized.setdefault("reasons", []).extend(basic_reasons)
        normalized_items.append(
            _unknown_accommodation(
                normalized,
                units=units,
                option_unverified=option_unverified,
            )
        )

    matched_item_count = len(normalized_items)
    if args.available_only:
        normalized_items = [item for item in normalized_items if item.get("is_available") is True]
    return {
        "business_id": ids.business_id,
        "business_type_id": ids.business_type_id,
        "kind": "accommodation",
        "status": "ok" if normalized_items else "empty",
        "observed_item_count": observed_item_count,
        "matched_item_count": matched_item_count,
        "items": [_view_item(item, "accommodation", view) for item in normalized_items],
    }


def _explore_time(
    calls: _Calls,
    ids: BookingIds,
    args: SimpleNamespace,
    *,
    units: int,
    view: str,
    errors: list[CapabilityError],
) -> dict[str, Any]:
    assert args.booking_date
    items = calls.call(
        "biz_items",
        ids.business_id,
        detail={"operation_name": "bizItems", "business_id": ids.business_id},
    )
    observed_item_count = len(items)
    requested_item_missing = False
    if ids.biz_item_id:
        matches = [
            item
            for item in items
            if str(item.get("bizItemId") or item.get("id") or "") == ids.biz_item_id
        ]
        observed_item_count = len(matches)
        requested_item_missing = not matches
        items = matches or [{"bizItemId": ids.biz_item_id}]

    normalized_items: list[dict[str, Any]] = []
    for item in items:
        biz_item_id = str(item.get("bizItemId") or item.get("id") or "")
        if not biz_item_id:
            continue
        if args.detail_mode != "full" and not item_passes_filters(item, args):
            continue
        merged = item
        if args.detail_mode == "full" or view == "full" or (ids.biz_item_id and not item.get("name")):
            try:
                detail = calls.call(
                    "biz_item",
                    ids.business_id,
                    biz_item_id,
                    detail={"operation_name": "bizItem", "business_id": ids.business_id, "biz_item_id": biz_item_id},
                )
                if requested_item_missing and not _has_requested_item_evidence(
                    detail, biz_item_id
                ):
                    errors.append(
                        CapabilityError(
                            code=ErrorCode.NOT_FOUND,
                            message="the requested Booking item was not found",
                            operation=OPERATION,
                            detail={
                                "business_id": ids.business_id,
                                "biz_item_id": biz_item_id,
                            },
                        )
                    )
                    continue
                if requested_item_missing:
                    observed_item_count = 1
                merged = merge_item(item, detail)
            except (BudgetExceeded, requests.RequestException, OSError, ValueError) as exc:
                errors.append(_with_details(_error_from_exception(exc), business_id=ids.business_id, biz_item_id=biz_item_id))
                if requested_item_missing:
                    continue
        if not item_passes_filters(merged, args):
            continue

        basic_reasons = time_basic_reasons(merged, args.guests)
        if basic_reasons:
            normalized = unavailable_time_item(
                merged,
                ids,
                args.booking_date,
                basic_reasons,
            )
            normalized["capacity_max"] = safe_int(merged.get("maxBookingCount"))
            normalized_items.append(normalized)
            continue

        option_categories: list[dict[str, Any]] = []
        option_unverified = False
        should_fetch_options = has_option_filters(args) or view == "full"
        if should_fetch_options:
            try:
                option_categories = calls.call(
                    "options",
                    ids.business_id,
                    biz_item_id,
                    detail={"operation_name": "option", "business_id": ids.business_id, "biz_item_id": biz_item_id},
                )
            except (BudgetExceeded, requests.RequestException, OSError, ValueError) as exc:
                errors.append(_with_details(_error_from_exception(exc), business_id=ids.business_id, biz_item_id=biz_item_id))
                option_unverified = has_option_filters(args)
            if has_option_filters(args) and not option_unverified and (
                not option_categories or not options_pass_filters(option_categories, args)
            ):
                normalized = unavailable_time_item(
                    merged,
                    ids,
                    args.booking_date,
                    ["option_filter_mismatch"],
                    option_categories,
                )
                normalized["capacity_max"] = safe_int(merged.get("maxBookingCount"))
                normalized_items.append(normalized)
                continue

        schedule: dict[str, Any] = {}
        schedule_failed = False
        try:
            schedule = calls.call(
                "hourly_schedule",
                ids.business_id,
                ids.business_type_id,
                biz_item_id,
                args.booking_date,
                detail={"operation_name": "hourlySchedule", "business_id": ids.business_id, "biz_item_id": biz_item_id},
            )
        except (BudgetExceeded, requests.RequestException, OSError, ValueError) as exc:
            schedule_failed = True
            errors.append(_with_details(_error_from_exception(exc), business_id=ids.business_id, biz_item_id=biz_item_id))

        normalized_items.append(
            _time_item(
                merged,
                schedule,
                option_categories,
                ids,
                booking_date=args.booking_date,
                guests=args.guests,
                units=units,
                time_from=args.time_from,
                time_to=args.time_to,
                schedule_failed=schedule_failed,
                option_unverified=option_unverified,
            )
        )

    matched_item_count = len(normalized_items)
    if args.available_only:
        normalized_items = [item for item in normalized_items if item.get("is_available") is True]
    return {
        "business_id": ids.business_id,
        "business_type_id": ids.business_type_id,
        "kind": "time_booking",
        "status": "ok" if normalized_items else "empty",
        "observed_item_count": observed_item_count,
        "matched_item_count": matched_item_count,
        "items": [_view_item(item, "time_booking", view) for item in normalized_items],
    }


def get_booking_availability(
    *,
    query: str | None = None,
    booking_url: str | None = None,
    business_id: str | None = None,
    business_type_id: int | None = None,
    check_in: str | None = None,
    check_out: str | None = None,
    booking_date: str | None = None,
    guests: int = 1,
    units: int = 1,
    available_only: bool = False,
    include_text: Iterable[str] | None = None,
    exclude_text: Iterable[str] | None = None,
    place_include_text: Iterable[str] | None = None,
    place_exclude_text: Iterable[str] | None = None,
    item_include_text: Iterable[str] | None = None,
    item_exclude_text: Iterable[str] | None = None,
    option_include_text: Iterable[str] | None = None,
    option_exclude_text: Iterable[str] | None = None,
    limit: int = 20,
    max_businesses: int = 10,
    query_mode: str = "auto",
    detail_mode: str = "minimal",
    time_from: str | None = None,
    time_to: str | None = None,
    view: str = "standard",
    request_budget: int = 40,
    max_elapsed_seconds: float = 120,
    raw_dir: str | Path | None = None,
    candidates: Sequence[Mapping[str, Any]] | None = None,
    candidate_provider: CandidateProvider | None = None,
    client: Any | None = None,
    budget: RequestBudget | None = None,
    timeout: int | float = 30,
    user_agent: str | None = None,
    sort: str = "",
    fetched_at: str | None = None,
) -> CapabilityResult:
    """Inspect public accommodation or time-booking availability.

    The function never submits a reservation.  Query discovery is optional and
    injectable; passing a Booking URL or IDs avoids calling Map entirely.
    ``raw_dir`` replays the legacy client's sanitized GraphQL fixture files.
    """

    request = {
        "query": clean_text(query) or None,
        "booking_url": str(booking_url or "").strip() or None,
        "business_id": str(business_id or "").strip() or None,
        "business_type_id": business_type_id,
        "check_in": check_in,
        "check_out": check_out,
        "date": booking_date,
        "guests": guests,
        "units": units,
        "available_only": bool(available_only),
        "include_text": _string_list(include_text),
        "exclude_text": _string_list(exclude_text),
        "place_include_text": _string_list(place_include_text),
        "place_exclude_text": _string_list(place_exclude_text),
        "item_include_text": _string_list(item_include_text),
        "item_exclude_text": _string_list(item_exclude_text),
        "option_include_text": _string_list(option_include_text),
        "option_exclude_text": _string_list(option_exclude_text),
        "limit": limit,
        "max_businesses": max_businesses,
        "query_mode": query_mode,
        "detail_mode": detail_mode,
        "time_from": time_from,
        "time_to": time_to,
        "view": view,
        "request_budget": request_budget,
        "max_elapsed_seconds": max_elapsed_seconds,
    }
    sources = sum(bool(value) for value in (request["query"], request["booking_url"], request["business_id"]))
    if sources != 1:
        return _invalid(request, "provide exactly one of query, booking_url, or business_id")
    if request["business_id"] and business_type_id is None:
        return _invalid(request, "business_id requires business_type_id")
    if request["business_id"]:
        if not re.fullmatch(r"\d+", request["business_id"]):
            return _invalid(request, "business_id must contain digits only")
        if (
            isinstance(business_type_id, bool)
            or not isinstance(business_type_id, int)
            or business_type_id < 1
        ):
            return _invalid(request, "business_type_id must be a positive integer")
    if request["booking_url"]:
        try:
            parse_booking_url(request["booking_url"])
        except (TypeError, ValueError) as exc:
            return _invalid(request, f"invalid booking_url: {exc}")
    schedules = int(bool(check_in or check_out)) + int(bool(booking_date))
    if schedules != 1:
        return _invalid(request, "provide either check_in/check_out or date")
    if bool(check_in) != bool(check_out):
        return _invalid(request, "check_in and check_out must be provided together")
    try:
        if check_in and check_out:
            if date.fromisoformat(check_out) <= date.fromisoformat(check_in):
                return _invalid(request, "check_out must be later than check_in")
        elif booking_date:
            date.fromisoformat(booking_date)
    except ValueError as exc:
        return _invalid(request, f"invalid ISO date: {exc}")
    for label, value in (("time_from", time_from), ("time_to", time_to)):
        if value is not None and not HHMM_PATTERN.fullmatch(str(value)):
            return _invalid(request, f"{label} must use 24-hour HH:MM format")
    if check_in and (time_from or time_to):
        return _invalid(request, "time_from/time_to apply only to time booking")
    if time_from and time_to and time_from > time_to:
        return _invalid(request, "time_from must not be later than time_to")
    if isinstance(guests, bool) or not isinstance(guests, int) or guests < 1:
        return _invalid(request, "guests must be a positive integer")
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        return _invalid(request, "units must be a positive integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        return _invalid(request, "limit must be an integer from 1 to 100")
    if isinstance(max_businesses, bool) or not isinstance(max_businesses, int) or not 1 <= max_businesses <= 20:
        return _invalid(request, "max_businesses must be an integer from 1 to 20")
    if query_mode not in {"auto", "broad", "specific"}:
        return _invalid(request, f"unsupported query_mode: {query_mode}")
    if detail_mode not in DETAIL_MODES:
        return _invalid(request, f"unsupported detail_mode: {detail_mode}")
    if view not in VIEWS:
        return _invalid(request, f"unsupported view: {view}")
    if isinstance(request_budget, bool) or not isinstance(request_budget, int) or not 1 <= request_budget <= 100:
        return _invalid(request, "request_budget must be an integer from 1 to 100")
    if (
        isinstance(max_elapsed_seconds, bool)
        or not isinstance(max_elapsed_seconds, (int, float))
        or not math.isfinite(max_elapsed_seconds)
        or max_elapsed_seconds <= 0
    ):
        return _invalid(request, "max_elapsed_seconds must be greater than zero")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 120
    ):
        return _invalid(request, "timeout must be a number greater than 0 and at most 120 seconds")
    if candidates is not None and not request["query"]:
        return _invalid(request, "injected candidates require query mode")
    if candidate_provider is not None and not request["query"]:
        return _invalid(request, "candidate_provider requires query mode")
    if candidates is not None and candidate_provider is not None:
        return _invalid(request, "candidates and candidate_provider are mutually exclusive")
    if raw_dir is not None and client is not None:
        return _invalid(request, "raw_dir and client are mutually exclusive")

    args = _legacy_args(
        query=request["query"],
        business_id=request["business_id"],
        business_type_id=business_type_id,
        check_in=check_in,
        check_out=check_out,
        booking_date=booking_date,
        guests=guests,
        available_only=available_only,
        include_text=request["include_text"],
        exclude_text=request["exclude_text"],
        place_include_text=request["place_include_text"],
        place_exclude_text=request["place_exclude_text"],
        item_include_text=request["item_include_text"],
        item_exclude_text=request["item_exclude_text"],
        option_include_text=request["option_include_text"],
        option_exclude_text=request["option_exclude_text"],
        limit=limit,
        max_businesses=max_businesses,
        query_mode=query_mode,
        detail_mode=detail_mode,
        time_from=time_from,
        time_to=time_to,
        timeout=timeout,
        user_agent=user_agent or "",
        sort=sort,
    )
    active_budget = budget or RequestBudget(
        max_requests=request_budget,
        max_elapsed_seconds=max_elapsed_seconds,
    )
    invoked_at = _utc_now()
    timestamp = fetched_at or invoked_at
    booking_timestamp = timestamp
    provenance: list[Provenance] = []
    errors: list[CapabilityError] = []
    warnings: list[str] = []
    discovery_incomplete = False
    discovery_stop_reason: str | None = None

    if raw_dir is not None and fetched_at is None:
        try:
            with active_budget.deadline():
                booking_timestamp = _fixture_capture_timestamp(raw_dir) or "unknown"
        except BudgetExceeded as exc:
            error = exc.to_error(FIXTURE_OPERATION)
            return CapabilityResult(
                capability=CAPABILITY,
                request=request,
                data={},
                status=Status.ERROR,
                errors=(error,),
                completeness=Completeness(
                    complete=False, stop_reason=_code_value(error.code)
                ),
                budget=active_budget,
            )

    try:
        if request["query"]:
            if candidates is not None:
                selected = [dict(candidate) for candidate in candidates]
            elif candidate_provider is not None:
                active_budget.consume()
                effective_timeout = min(
                    float(timeout),
                    max(0.001, active_budget.elapsed_remaining_seconds / 2),
                )
                args.timeout = effective_timeout
                try:
                    with active_budget.deadline():
                        discovered = candidate_provider(
                            query=request["query"],
                            limit=limit,
                            max_businesses=max_businesses,
                            timeout=effective_timeout,
                        )
                except Exception as exc:
                    provenance.append(
                        Provenance(
                            source="naver-map-public",
                            operation=MAP_OPERATION,
                            fetched_at=timestamp,
                            live=True,
                            detail={
                                "outcome": "error",
                                "exception": exc.__class__.__name__,
                            },
                        )
                    )
                    raise
                provenance.append(
                    Provenance(
                        source="naver-map-public",
                        operation=MAP_OPERATION,
                        fetched_at=timestamp,
                        live=True,
                        detail={"outcome": "ok"},
                    )
                )
                selected = prepare_query_candidates(
                    [dict(candidate) for candidate in discovered], args, warnings
                )
            else:
                map_transport = Transport(
                    policy=RequestPolicy(
                        connect_timeout_seconds=min(15.0, float(timeout)),
                        read_timeout_seconds=float(timeout),
                    ),
                    budget=active_budget,
                )
                map_result = search_places(
                    request["query"],
                    limit=limit,
                    sort=sort,
                    transport=map_transport,
                    budget=active_budget,
                    fetched_at=timestamp,
                    headers={"User-Agent": user_agent} if user_agent else None,
                )
                provenance.extend(map_result.provenance)
                warnings.extend(map_result.warnings)
                errors.extend(map_result.errors)
                discovery_incomplete = not map_result.completeness.complete
                discovery_stop_reason = map_result.completeness.stop_reason
                if map_result.status is Status.ERROR:
                    return CapabilityResult(
                        capability=CAPABILITY,
                        request=request,
                        data={},
                        status=Status.ERROR,
                        errors=map_result.errors,
                        warnings=tuple(warnings),
                        provenance=tuple(provenance),
                        completeness=Completeness(
                            complete=False,
                            stop_reason=map_result.completeness.stop_reason,
                        ),
                        budget=active_budget,
                    )
                discovered = [
                    {
                        "place_id": str(place.get("place_id") or ""),
                        "place_name": str(place.get("name") or ""),
                        "category": str(place.get("category") or ""),
                        "address": str(
                            place.get("road_address") or place.get("address") or ""
                        ),
                        "reservation_url": str(place.get("reservation_url") or ""),
                    }
                    for place in map_result.data.get("places", [])
                    if isinstance(place, Mapping)
                ]
                selected = prepare_query_candidates(discovered, args, warnings)
            selected = selected[:max_businesses]
        else:
            selected = [_direct_candidate(request["booking_url"], request["business_id"], business_type_id)]
    except Exception as exc:
        try:
            active_budget.check()
        except BudgetExceeded as deadline:
            exc = deadline
        error = _error_from_exception(exc, MAP_OPERATION if request["query"] else OPERATION)
        return CapabilityResult(
            capability=CAPABILITY,
            request=request,
            data={},
            status=Status.ERROR,
            errors=(error,),
            provenance=tuple(provenance),
            completeness=Completeness(complete=False, stop_reason=_code_value(error.code)),
            budget=active_budget,
        )

    if client is None:
        active_client = BookingGraphQLClient(
            timeout=timeout,
            user_agent=user_agent,
            raw_dir=str(raw_dir) if raw_dir is not None else None,
            rate_limit_cooldown=0,
            rate_limit_retries=0,
        )
    else:
        active_client = client
    calls = _Calls(
        active_client,
        active_budget,
        offline=raw_dir is not None,
        timestamp=booking_timestamp,
        replayed_at=invoked_at if raw_dir is not None else None,
    )
    places: list[dict[str, Any]] = []
    completed_candidates = 0

    for candidate in selected:
        place = _place_shell(candidate)
        if not place["reservation_url"] and not candidate.get("ids"):
            place["booking"] = {"status": "no_reservation_url", "items": []}
            error = CapabilityError(
                code=ErrorCode.SECONDARY_NOT_FOUND,
                message="map candidate has no public reservation URL",
                operation=OPERATION,
                detail={"place_id": place["place_id"] or None},
            )
            errors.append(error)
            places.append(place)
            completed_candidates += 1
            continue
        try:
            ids = _resolve_ids(candidate)
            if check_in and check_out:
                place["booking"] = _explore_accommodation(
                    calls,
                    ids,
                    args,
                    units=units,
                    view=view,
                    errors=errors,
                )
            else:
                place["booking"] = _explore_time(
                    calls,
                    ids,
                    args,
                    units=units,
                    view=view,
                    errors=errors,
                )
            completed_candidates += 1
        except Exception as exc:
            error = _with_details(
                _error_from_exception(exc),
                place_id=place["place_id"],
                reservation_url=place["reservation_url"],
            )
            errors.append(error)
            place["booking"] = {
                "status": "error",
                "items": [],
                "error_code": _code_value(error.code),
            }
            places.append(place)
            if isinstance(exc, BudgetExceeded) or error.code == ErrorCode.RATE_LIMITED:
                break
            continue
        places.append(place)
        if getattr(active_client, "rate_limit_stopped", False):
            errors.append(
                CapabilityError(
                    code=ErrorCode.RATE_LIMITED,
                    message="booking batch stopped after repeated rate limiting",
                    operation=OPERATION,
                    retryable=True,
                )
            )
            break

    provenance.extend(calls.provenance)
    item_count = sum(
        len((place.get("booking") or {}).get("items") or []) for place in places
    )
    observed_item_count = sum(
        int((place.get("booking") or {}).get("observed_item_count") or 0)
        for place in places
    )
    data = {
        "places": places,
        "place_count": len(places),
        "item_count": item_count,
        "observed_item_count": observed_item_count,
    }
    if errors:
        has_usable = bool(item_count or (request["query"] and places))
        status = Status.PARTIAL if has_usable else Status.ERROR
        stop_reason = _code_value(errors[-1].code)
        complete = False
    elif item_count:
        if discovery_incomplete:
            status = Status.PARTIAL
            stop_reason = discovery_stop_reason or "source_page_limit"
            complete = False
        else:
            status = Status.OK
            stop_reason = "requested_limit" if len(selected) >= max_businesses and request["query"] else "exhausted"
            complete = True
    else:
        direct = not bool(request["query"])
        if direct and observed_item_count == 0:
            error = CapabilityError(
                code=ErrorCode.NOT_FOUND,
                message="the requested Booking business or item returned no items",
                operation=OPERATION,
            )
            errors.append(error)
            status = Status.ERROR
            stop_reason = "not_found"
            complete = False
        else:
            status = Status.PARTIAL if discovery_incomplete else Status.EMPTY
            stop_reason = (
                discovery_stop_reason or "source_page_limit"
                if discovery_incomplete
                else "filtered" if observed_item_count else "exhausted"
            )
            complete = not discovery_incomplete

    return CapabilityResult(
        capability=CAPABILITY,
        request=request,
        data=data,
        status=status,
        warnings=tuple(warnings),
        errors=tuple(errors),
        provenance=tuple(provenance),
        completeness=Completeness(
            complete=complete,
            stop_reason=stop_reason,
            requested_count=len(selected),
            returned_count=completed_candidates,
        ),
        budget=active_budget,
    )


booking = get_booking_availability
availability = get_booking_availability


__all__ = [
    "CAPABILITY",
    "availability",
    "booking",
    "get_booking_availability",
]
