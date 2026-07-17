"""Composable visitor-review capability for public Naver Place data.

The adapter owns pagination, request bounds, result semantics, and agent-facing
views. Proven parsing and normalization helpers are shared with the historical
CLI through the internal compatibility package.
"""

from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timezone
from pathlib import Path
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
    Status,
)


from naver_place._legacy.scrape_naver_map import build_mobile_map_headers
from naver_place._legacy.scrape_naver_place_reviews import (
    build_place_url,
    extract_place_id,
    normalize_reviews,
    post_review_page,
)


CAPABILITY = "place.reviews"
SOURCE = "naver-place-public"
OPERATION = "place.reviews_graphql"
FIXTURE_OPERATION = "fixture.replay"
OWNER_REPLY_ALL = "all"
OWNER_REPLY_EXCLUDE = "exclude_replied"
OWNER_REPLY_ONLY = "only_replied"
OWNER_REPLY_FILTERS = {
    OWNER_REPLY_ALL,
    OWNER_REPLY_EXCLUDE,
    OWNER_REPLY_ONLY,
}
VIEWS = {"compact", "standard", "full"}
AUTH_HEADER_NAMES = {"authorization", "proxy-authorization", "cookie"}

PageFetcher = Callable[..., Mapping[str, Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _http_error(exc: BaseException) -> CapabilityError:
    if isinstance(exc, BudgetExceeded):
        return exc.to_error(OPERATION)
    if isinstance(exc, json.JSONDecodeError):
        return CapabilityError(
            code=ErrorCode.UPSTREAM_CHANGED,
            message="public review response was not valid JSON",
            operation=OPERATION,
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
            operation=OPERATION,
            http_status=status,
            retryable=status == 429 or bool(status and status >= 500),
        )
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, requests.RequestException, OSError)):
        return CapabilityError(
            code=ErrorCode.NETWORK_ERROR,
            message=str(exc),
            operation=OPERATION,
            retryable=isinstance(exc, (requests.Timeout, requests.ConnectionError)),
        )
    return CapabilityError(
        code=ErrorCode.UPSTREAM_CHANGED,
        message=str(exc),
        operation=OPERATION,
    )


def _stop_reason(error: CapabilityError) -> str:
    return getattr(error.code, "value", str(error.code))


def _coerce_page(value: Mapping[str, Any]) -> dict[str, Any]:
    """Accept saved visitorReviews roots or full GraphQL envelopes."""

    if not isinstance(value, Mapping):
        raise ValueError("review page must be a JSON object")
    data = value.get("data")
    if isinstance(data, Mapping):
        root = data.get("visitorReviews")
        if isinstance(root, Mapping):
            return dict(root)
    if "items" not in value and "total" not in value:
        raise ValueError("review GraphQL response has no visitorReviews root")
    return dict(value)


def _raw_page_paths(raw_dir: str | Path) -> list[Path]:
    directory = Path(raw_dir)
    paths = sorted(directory.glob("page-*.json"))
    if not paths:
        raise ValueError("no page-*.json review fixtures found in the supplied directory")
    return paths


def _read_raw_page(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise ValueError(f"review fixture {path.name} is unreadable") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid review fixture {path.name}: {exc}") from exc
    return _coerce_page(value)


def _fixture_capture_timestamp(raw_dir: str | Path) -> str | None:
    path = Path(raw_dir) / "fixture-metadata.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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


def _view_review(review: Mapping[str, Any], view: str) -> dict[str, Any]:
    if view == "full":
        return dict(review)
    if view == "compact":
        keys = ("id", "date", "text", "tags", "has_owner_reply")
    else:
        keys = (
            "id",
            "nickname",
            "date",
            "created_at",
            "visited_at",
            "visit_count",
            "text",
            "tags",
            "image_count",
            "has_owner_reply",
            "owner_reply_created_at",
        )
    # In particular, standard and compact never expose reviewer_id,
    # receipt_info_url, cursor, or raw media/profile metadata.
    return {key: review.get(key) for key in keys}


def _filter_owner_reply(
    reviews: Iterable[dict[str, Any]], owner_reply: str
) -> list[dict[str, Any]]:
    if owner_reply == OWNER_REPLY_EXCLUDE:
        return [review for review in reviews if not review.get("has_owner_reply")]
    if owner_reply == OWNER_REPLY_ONLY:
        return [review for review in reviews if review.get("has_owner_reply")]
    return list(reviews)


def get_reviews(
    place: str,
    *,
    limit: int = 20,
    page_size: int = 10,
    owner_reply: str | bool = OWNER_REPLY_ALL,
    request_delay: float = 1.5,
    view: str = "standard",
    request_budget: int = 40,
    max_elapsed_seconds: float = 120,
    raw_dir: str | Path | None = None,
    pages: Sequence[Mapping[str, Any]] | None = None,
    page_fetcher: PageFetcher | None = None,
    budget: RequestBudget | None = None,
    headers: Mapping[str, str] | None = None,
    user_agent: str | None = None,
    timeout: int | float = 30,
    sleep: Callable[[float], None] = time.sleep,
    fetched_at: str | None = None,
) -> CapabilityResult:
    """Return bounded, normalized visitor reviews.

    Exactly one of ``raw_dir``, ``pages``, or ``page_fetcher`` may be used to
    inject an offline/custom source.  Without one, the existing read-only
    GraphQL page fetcher is called.  Later-page failures preserve collected
    records as a typed partial result.
    """

    normalized_owner_reply = (
        OWNER_REPLY_EXCLUDE if owner_reply is True else OWNER_REPLY_ALL if owner_reply is False else owner_reply
    )
    request = {
        "place": str(place or "").strip(),
        "limit": limit,
        "page_size": page_size,
        "owner_reply": normalized_owner_reply,
        "request_delay": request_delay,
        "view": view,
        "request_budget": request_budget,
        "max_elapsed_seconds": max_elapsed_seconds,
    }
    if not request["place"]:
        return _invalid(request, "place must be a numeric Place ID or Naver Place URL")
    try:
        place_id = extract_place_id(request["place"])
    except ValueError as exc:
        return _invalid(request, str(exc))
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 500:
        return _invalid(request, "limit must be an integer from 0 to 500")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 50:
        return _invalid(request, "page_size must be an integer from 1 to 50")
    if normalized_owner_reply not in OWNER_REPLY_FILTERS:
        return _invalid(request, f"unsupported owner_reply filter: {normalized_owner_reply}")
    if view not in VIEWS:
        return _invalid(request, f"unsupported view: {view}")
    if (
        isinstance(request_delay, bool)
        or not isinstance(request_delay, (int, float))
        or not math.isfinite(request_delay)
        or request_delay < 0
        or request_delay > 30
    ):
        return _invalid(request, "request_delay must be from 0 to 30 seconds")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > 120
    ):
        return _invalid(request, "timeout must be from 1 to 120 seconds")
    if isinstance(request_budget, bool) or not isinstance(request_budget, int) or not 1 <= request_budget <= 100:
        return _invalid(request, "request_budget must be an integer from 1 to 100")
    if (
        isinstance(max_elapsed_seconds, bool)
        or not isinstance(max_elapsed_seconds, (int, float))
        or not math.isfinite(max_elapsed_seconds)
        or max_elapsed_seconds <= 0
    ):
        return _invalid(request, "max_elapsed_seconds must be greater than zero")
    source_count = sum(value is not None for value in (raw_dir, pages, page_fetcher))
    if source_count > 1:
        return _invalid(request, "raw_dir, pages, and page_fetcher are mutually exclusive")
    if headers and any(str(key).casefold() in AUTH_HEADER_NAMES for key in headers):
        return _invalid(
            request,
            "authenticated headers are outside the public read-only boundary",
        )

    active_budget = budget or RequestBudget(
        max_requests=request_budget,
        max_elapsed_seconds=max_elapsed_seconds,
    )
    replayed_at = _utc_now()
    timestamp = fetched_at or replayed_at
    offline = raw_dir is not None or pages is not None
    try:
        with active_budget.deadline():
            replay_paths = _raw_page_paths(raw_dir) if raw_dir is not None else []
            replay_pages = list(pages or ())
            capture_timestamp = (
                _fixture_capture_timestamp(raw_dir) if raw_dir is not None else None
            )
    except (BudgetExceeded, ValueError) as exc:
        error = (
            exc.to_error(FIXTURE_OPERATION)
            if isinstance(exc, BudgetExceeded)
            else CapabilityError(
                code=ErrorCode.UPSTREAM_CHANGED,
                message=str(exc),
                operation=FIXTURE_OPERATION,
            )
        )
        return CapabilityResult(
            capability=CAPABILITY,
            request=request,
            data={},
            status=Status.ERROR,
            errors=(error,),
            completeness=Completeness(
                complete=False, stop_reason=_stop_reason(error)
            ),
            budget=active_budget,
        )
    if offline and fetched_at is None:
        timestamp = capture_timestamp or "unknown"

    replay_index = 0
    session = requests.Session()
    session.trust_env = False
    active_headers = {**build_mobile_map_headers(user_agent), **dict(headers or {})}

    def fetch(cursor: str | None, requested: int) -> dict[str, Any]:
        nonlocal replay_index
        if offline:
            source_count = len(replay_paths) if raw_dir is not None else len(replay_pages)
            if replay_index >= source_count:
                return {"items": [], "total": None}
            source = (
                replay_paths[replay_index]
                if raw_dir is not None
                else replay_pages[replay_index]
            )
            replay_index += 1
            active_budget.check()
            with active_budget.deadline():
                return (
                    _read_raw_page(source)
                    if isinstance(source, Path)
                    else _coerce_page(source)
                )
        active_budget.consume()
        effective_timeout = min(
            float(timeout), max(0.001, active_budget.elapsed_remaining_seconds / 2)
        )
        if page_fetcher is not None:
            with active_budget.deadline():
                value = page_fetcher(
                    place_id=place_id,
                    cursor=cursor,
                    page_size=requested,
                    timeout=effective_timeout,
                    headers=active_headers,
                )
            page = _coerce_page(value)
        else:
            session.cookies.clear()
            try:
                with active_budget.deadline():
                    page = _coerce_page(
                        post_review_page(
                            session,
                            place_id,
                            cursor,
                            requested,
                            effective_timeout,
                            active_headers,
                            0,
                            0,
                        )
                    )
            finally:
                session.cookies.clear()
        return page

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor: str | None = None
    total_available: int | None = None
    pages_fetched = 0
    provenance: list[Provenance] = []
    errors: list[CapabilityError] = []
    stop_reason = "requested_limit" if limit == 0 else ""

    def matching_count() -> int:
        normalized = normalize_reviews(records)
        return len(_filter_owner_reply(normalized, str(normalized_owner_reply)))

    while limit > 0 and matching_count() < limit:
        requested = (
            min(page_size, max(1, limit - len(records)))
            if normalized_owner_reply == OWNER_REPLY_ALL
            else page_size
        )
        try:
            active_budget.check()
            page = fetch(cursor, requested)
        except (BudgetExceeded, requests.RequestException, OSError, ValueError) as exc:
            try:
                active_budget.check()
            except BudgetExceeded as deadline:
                exc = deadline
            error = _http_error(exc)
            errors.append(error)
            provenance.append(
                Provenance(
                    source="fixture" if offline else SOURCE,
                    operation=FIXTURE_OPERATION if offline else OPERATION,
                    fetched_at=timestamp,
                    live=not offline,
                    detail={
                        "page": pages_fetched + 1,
                        "outcome": "error",
                        "error_code": str(error.code),
                        **({"replays": OPERATION} if offline else {}),
                        **({"replayed_at": replayed_at} if offline else {}),
                    },
                )
            )
            stop_reason = _stop_reason(error)
            break

        pages_fetched += 1
        provenance.append(
            Provenance(
                source="fixture" if offline else SOURCE,
                operation=FIXTURE_OPERATION if offline else OPERATION,
                fetched_at=timestamp,
                live=not offline,
                detail={
                    "page": pages_fetched,
                    "replays": OPERATION,
                    "replayed_at": replayed_at,
                }
                if offline
                else {"page": pages_fetched},
            )
        )
        if total_available is None:
            value = page.get("total")
            try:
                total_available = int(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                total_available = None
        items = page.get("items")
        if not isinstance(items, list):
            errors.append(
                CapabilityError(
                    code=ErrorCode.UPSTREAM_CHANGED,
                    message="review GraphQL items is not a list",
                    operation=OPERATION,
                )
            )
            stop_reason = "upstream_changed"
            break
        if not items:
            if total_available is not None and len(records) < total_available:
                errors.append(
                    CapabilityError(
                        code=ErrorCode.UPSTREAM_CHANGED,
                        message="review pagination ended before the reported total was reached",
                        operation=OPERATION,
                    )
                )
                stop_reason = "upstream_changed"
            else:
                stop_reason = "exhausted"
            break

        added = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or item.get("reviewId") or item.get("cursor") or "")
            if not key:
                key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            records.append(item)
            added += 1
        next_cursor = ""
        if items and isinstance(items[-1], Mapping):
            next_cursor = str(items[-1].get("cursor") or "").strip()
        if matching_count() >= limit:
            stop_reason = "requested_limit"
            break
        if total_available is not None and len(records) >= total_available:
            stop_reason = "exhausted"
            break
        if not added:
            errors.append(
                CapabilityError(
                    code=ErrorCode.UPSTREAM_CHANGED,
                    message="review cursor page repeated without new records",
                    operation=OPERATION,
                )
            )
            stop_reason = "upstream_changed"
            break
        if not next_cursor:
            if (
                total_available is not None and len(records) < total_available
            ) or (total_available is None and len(items) >= requested):
                errors.append(
                    CapabilityError(
                        code=ErrorCode.UPSTREAM_CHANGED,
                        message="review pagination cursor is missing before exhaustion was established",
                        operation=OPERATION,
                    )
                )
                stop_reason = "upstream_changed"
            else:
                stop_reason = "exhausted"
            break
        if next_cursor == cursor:
            errors.append(
                CapabilityError(
                    code=ErrorCode.UPSTREAM_CHANGED,
                    message="review cursor did not advance",
                    operation=OPERATION,
                )
            )
            stop_reason = "upstream_changed"
            break
        cursor = next_cursor
        if not offline and active_budget.requests_remaining < 1:
            error = CapabilityError(
                code=ErrorCode.REQUEST_BUDGET_EXHAUSTED,
                message=f"request budget of {active_budget.max_requests} was exhausted",
                operation=OPERATION,
            )
            errors.append(error)
            stop_reason = _stop_reason(error)
            break
        if request_delay and not offline:
            if active_budget.elapsed_seconds + request_delay >= active_budget.max_elapsed_seconds:
                error = CapabilityError(
                    code=ErrorCode.TIME_BUDGET_EXHAUSTED,
                    message="review page delay would exceed the invocation time budget",
                    operation=OPERATION,
                )
                errors.append(error)
                stop_reason = _stop_reason(error)
                break
            sleep(request_delay)
            try:
                active_budget.check()
            except BudgetExceeded as exc:
                error = exc.to_error(OPERATION)
                errors.append(error)
                stop_reason = _stop_reason(error)
                break

    normalized = normalize_reviews(records)
    filtered = _filter_owner_reply(normalized, str(normalized_owner_reply))
    visible = [_view_review(review, view) for review in filtered[:limit]]
    data = {
        "place_id": place_id,
        "place_url": build_place_url(place_id),
        "total_available": total_available,
        "returned_count": len(visible),
        "reviews": visible,
    }
    complete = not errors and stop_reason in {"requested_limit", "exhausted"}
    completeness = Completeness(
        complete=complete,
        stop_reason=stop_reason or "exhausted",
        pages_fetched=pages_fetched,
        requested_count=limit,
        returned_count=len(visible),
    )
    if errors:
        status = Status.PARTIAL if visible else Status.ERROR
    else:
        status = Status.OK if visible else Status.EMPTY
    return CapabilityResult(
        capability=CAPABILITY,
        request=request,
        data=data,
        status=status,
        errors=tuple(errors),
        provenance=tuple(provenance),
        completeness=completeness,
        budget=active_budget,
    )


reviews = get_reviews
review = get_reviews


__all__ = [
    "CAPABILITY",
    "OWNER_REPLY_ALL",
    "OWNER_REPLY_EXCLUDE",
    "OWNER_REPLY_ONLY",
    "get_reviews",
    "review",
    "reviews",
]
