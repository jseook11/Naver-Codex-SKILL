"""Bounded visitor-review snapshots from public Naver Place HTML.

The live capability intentionally reads two public pages and stops:

* ``reviewSort=recent`` for the latest content reviews;
* the default review page for recommended content and keyword-only reviews.

Each upstream group is capped by the page itself (currently ten records).  The
capability does not call the rejected review GraphQL endpoint, follow cursors,
or claim to collect every review for a Place.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from naver_place._legacy.scrape_naver_map import build_mobile_map_headers
from naver_place._legacy.scrape_naver_place_reviews import (
    build_place_url,
    extract_apollo_state,
    extract_place_id,
    normalize_reviews,
    resolve_ref,
)
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
from naver_place.transport import Transport, TransportError


CAPABILITY = "place.reviews"
SOURCE = "naver-place-public"
LATEST_OPERATION = "place.reviews_latest_html"
RECOMMENDED_OPERATION = "place.reviews_recommended_html"
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
MAX_SAMPLE_REVIEWS = 10
SAMPLE_LATEST = "latest"
SAMPLE_RECOMMENDED = "recommended"
SAMPLE_RECOMMENDED_KEYWORD_ONLY = "recommended_keyword_only"
SAMPLE_ORDER = (
    SAMPLE_LATEST,
    SAMPLE_RECOMMENDED,
    SAMPLE_RECOMMENDED_KEYWORD_ONLY,
)


class ReviewSchemaError(ValueError):
    """The public review HTML no longer contains the expected snapshot."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_latest_review_url(place_id: str) -> str:
    return f"{build_place_url(place_id)}?reviewSort=recent"


def build_recommended_review_url(place_id: str) -> str:
    return build_place_url(place_id)


def _recommended_url_from_latest(response_url: str, place_id: str) -> str | None:
    """Reuse the canonical Place type learned from the first safe redirect."""

    try:
        parsed = urlsplit(response_url)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or not (host == "naver.com" or host.endswith(".naver.com"))
        or port not in {None, 443}
        or f"/{place_id}/review/visitor" not in parsed.path
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _invalid(request: Mapping[str, Any], message: str) -> CapabilityResult:
    error = CapabilityError(
        code=ErrorCode.INVALID_INPUT,
        message=message,
        operation=CAPABILITY,
    )
    return CapabilityResult(
        capability=CAPABILITY,
        request=request,
        data={},
        status=Status.ERROR,
        errors=(error,),
        completeness=Completeness(complete=False, stop_reason="invalid_input"),
    )


def _response_text(response: Any) -> str:
    content = getattr(response, "content", b"")
    if isinstance(content, bytes) and content:
        encoding = str(getattr(response, "encoding", "") or "").lower()
        if not encoding or encoding in {"iso-8859-1", "latin-1"}:
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                encoding = str(
                    getattr(response, "apparent_encoding", "") or "utf-8"
                )
        return content.decode(encoding, errors="replace")
    text = getattr(response, "text", None)
    return text if isinstance(text, str) else str(content)


def _transport_budget(
    transport: Any, fallback: RequestBudget | None
) -> RequestBudget | None:
    return getattr(transport, "budget", None) or fallback


def _query_input(key: str) -> Mapping[str, Any] | None:
    prefix = "visitorReviews("
    if not key.startswith(prefix) or not key.endswith(")"):
        return None
    try:
        arguments = json.loads(key[len(prefix) : -1])
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, Mapping):
        return None
    value = arguments.get("input")
    return value if isinstance(value, Mapping) else None


def _is_unfiltered_snapshot(query: Mapping[str, Any]) -> bool:
    if query.get("after") not in (None, ""):
        return False
    if query.get("reviewIds") not in (None, "", []):
        return False
    if query.get("isPhotoUsed") not in (None, False):
        return False
    if query.get("item") not in (None, "", "0", 0):
        return False
    return all(
        query.get(name) in (None, "", [], False)
        for name in ("theme", "menu", "highlightKeyword")
    )


def _select_review_root(
    state: Mapping[str, Any],
    *,
    place_id: str,
    sort: str | None,
    include_content: bool,
    required: bool,
) -> Mapping[str, Any] | None:
    root = state.get("ROOT_QUERY")
    if not isinstance(root, Mapping):
        raise ReviewSchemaError("public review HTML has no Apollo ROOT_QUERY")

    candidates: list[Mapping[str, Any]] = []
    for key, value in root.items():
        query = _query_input(str(key))
        if query is None:
            continue
        if str(query.get("businessId") or "") != place_id:
            continue
        if query.get("includeContent") is not include_content:
            continue
        observed_sort = str(query.get("sort") or "")
        if observed_sort != (sort or ""):
            continue
        if not _is_unfiltered_snapshot(query):
            continue
        resolved = resolve_ref(dict(state), value)
        if resolved:
            candidates.append(resolved)

    if not candidates:
        if required:
            label = "latest" if sort == "recent" else "recommended"
            raise ReviewSchemaError(
                f"public review HTML has no unfiltered {label} review snapshot"
            )
        return None
    if len(candidates) > 1:
        raise ReviewSchemaError(
            "public review HTML has multiple matching review snapshots"
        )
    return candidates[0]


def _coerce_total(root: Mapping[str, Any]) -> int | None:
    value = root.get("total")
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ReviewSchemaError("public review snapshot total is not an integer")
    try:
        total = int(value)
    except (TypeError, ValueError):
        raise ReviewSchemaError(
            "public review snapshot total is not an integer"
        ) from None
    if total < 0:
        raise ReviewSchemaError("public review snapshot total is negative")
    return total


def _reviews_from_root(
    state: Mapping[str, Any],
    root: Mapping[str, Any] | None,
    *,
    require_text: bool,
) -> tuple[list[dict[str, Any]], int | None]:
    if root is None:
        return [], 0
    items = root.get("items")
    if not isinstance(items, list):
        raise ReviewSchemaError("public review snapshot items is not a list")
    records: list[dict[str, Any]] = []
    for item in items:
        record = resolve_ref(dict(state), item)
        if not record:
            raise ReviewSchemaError("public review snapshot contains a broken item reference")
        records.append(record)
    reviews = normalize_reviews(records, dict(state))
    if require_text:
        reviews = [review for review in reviews if review.get("text")]
    total = _coerce_total(root)
    if items and not reviews:
        raise ReviewSchemaError("public review snapshot contained no usable reviews")
    if total and not items:
        raise ReviewSchemaError(
            "public review snapshot reported reviews but exposed no items"
        )
    return reviews, total


def parse_latest_review_html(
    html: str, place_id: str
) -> dict[str, tuple[list[dict[str, Any]], int | None]]:
    try:
        state = extract_apollo_state(html)
    except ValueError as exc:
        raise ReviewSchemaError(str(exc)) from exc
    if not state:
        raise ReviewSchemaError("public latest-review HTML has no Apollo state")
    root = _select_review_root(
        state,
        place_id=place_id,
        sort="recent",
        include_content=True,
        required=True,
    )
    return {
        SAMPLE_LATEST: _reviews_from_root(state, root, require_text=True),
    }


def parse_recommended_review_html(
    html: str, place_id: str
) -> dict[str, tuple[list[dict[str, Any]], int | None]]:
    try:
        state = extract_apollo_state(html)
    except ValueError as exc:
        raise ReviewSchemaError(str(exc)) from exc
    if not state:
        raise ReviewSchemaError("public recommended-review HTML has no Apollo state")
    content_root = _select_review_root(
        state,
        place_id=place_id,
        sort=None,
        include_content=True,
        required=True,
    )
    keyword_root = _select_review_root(
        state,
        place_id=place_id,
        sort=None,
        include_content=False,
        required=True,
    )
    return {
        SAMPLE_RECOMMENDED: _reviews_from_root(
            state, content_root, require_text=True
        ),
        SAMPLE_RECOMMENDED_KEYWORD_ONLY: _reviews_from_root(
            state, keyword_root, require_text=False
        ),
    }


def _filter_owner_reply(
    reviews: Iterable[dict[str, Any]], owner_reply: str
) -> list[dict[str, Any]]:
    if owner_reply == OWNER_REPLY_EXCLUDE:
        return [review for review in reviews if not review.get("has_owner_reply")]
    if owner_reply == OWNER_REPLY_ONLY:
        return [review for review in reviews if review.get("has_owner_reply")]
    return list(reviews)


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
    return {key: review.get(key) for key in keys}


def _review_key(review: Mapping[str, Any]) -> str:
    review_id = str(review.get("id") or "").strip()
    if review_id:
        return f"id:{review_id}"
    return "content:" + "|".join(
        str(review.get(key) or "").strip()
        for key in ("nickname", "date", "text")
    )


def _sample_payloads(
    raw_samples: Mapping[
        str, tuple[list[dict[str, Any]], int | None]
    ],
    *,
    limit: int,
    owner_reply: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    payloads: dict[str, dict[str, Any]] = {}
    selected: dict[str, list[dict[str, Any]]] = {}
    for name in SAMPLE_ORDER:
        reviews, total = raw_samples.get(name, ([], None))
        visible = _filter_owner_reply(reviews, owner_reply)[:limit]
        selected[name] = visible
        payloads[name] = {
            "sort": "latest" if name == SAMPLE_LATEST else "recommended",
            "review_type": (
                "keyword_only"
                if name == SAMPLE_RECOMMENDED_KEYWORD_ONLY
                else "content"
            ),
            "total_available": total,
            "returned_count": len(visible),
            "review_ids": [
                review.get("id") for review in visible if review.get("id")
            ],
        }
    return payloads, selected


def _merge_reviews(
    selected: Mapping[str, list[dict[str, Any]]], view: str
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for sample_name in SAMPLE_ORDER:
        for rank, review in enumerate(selected.get(sample_name, ()), start=1):
            key = _review_key(review)
            if key in positions:
                existing = merged[positions[key]]
                existing["sample_sources"].append(sample_name)
                existing["sample_ranks"][sample_name] = rank
                continue
            visible = _view_review(review, view)
            visible["sample_sources"] = [sample_name]
            visible["sample_ranks"] = {sample_name: rank}
            positions[key] = len(merged)
            merged.append(visible)
    return merged


def _empty_sample_payloads() -> dict[str, dict[str, Any]]:
    return {
        name: {
            "sort": "latest" if name == SAMPLE_LATEST else "recommended",
            "review_type": (
                "keyword_only"
                if name == SAMPLE_RECOMMENDED_KEYWORD_ONLY
                else "content"
            ),
            "total_available": None,
            "returned_count": 0,
            "review_ids": [],
        }
        for name in SAMPLE_ORDER
    }


def _data_payload(
    place_id: str,
    sample_payloads: Mapping[str, Mapping[str, Any]],
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    recommended_total = sample_payloads.get(SAMPLE_RECOMMENDED, {}).get(
        "total_available"
    )
    keyword_total = sample_payloads.get(
        SAMPLE_RECOMMENDED_KEYWORD_ONLY, {}
    ).get("total_available")
    totals = [
        value
        for value in (recommended_total, keyword_total)
        if isinstance(value, int)
    ]
    return {
        "place_id": place_id,
        "place_url": build_place_url(place_id),
        "snapshot_scope": "latest_and_recommended_public_html",
        "total_available": sum(totals) if totals else None,
        "returned_count": len(reviews),
        "samples": dict(sample_payloads),
        "reviews": reviews,
    }


def _error_result(
    *,
    request: Mapping[str, Any],
    place_id: str,
    error: CapabilityError,
    provenance: tuple[Provenance, ...],
    budget: RequestBudget | None,
    sample_payloads: Mapping[str, Mapping[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    pages_fetched: int = 0,
) -> CapabilityResult:
    visible = reviews or []
    data = (
        _data_payload(
            place_id,
            sample_payloads or _empty_sample_payloads(),
            visible,
        )
        if visible
        else {}
    )
    return CapabilityResult(
        capability=CAPABILITY,
        request=request,
        data=data,
        status=Status.PARTIAL if visible else Status.ERROR,
        errors=(error,),
        provenance=provenance,
        completeness=Completeness(
            complete=False,
            stop_reason=str(error.code),
            pages_fetched=pages_fetched,
            requested_count=int(request.get("limit") or 0) * len(SAMPLE_ORDER),
            returned_count=sum(
                int(sample.get("returned_count") or 0)
                for sample in (sample_payloads or {}).values()
            ),
        ),
        budget=budget,
    )


def get_reviews(
    place: str,
    *,
    limit: int = MAX_SAMPLE_REVIEWS,
    owner_reply: str | bool = OWNER_REPLY_ALL,
    view: str = "standard",
    request_budget: int = 40,
    max_elapsed_seconds: float = 120,
    latest_html: str | None = None,
    recommended_html: str | None = None,
    latest_source_url: str | None = None,
    recommended_source_url: str | None = None,
    transport: Transport | None = None,
    budget: RequestBudget | None = None,
    headers: Mapping[str, str] | None = None,
    user_agent: str | None = None,
    fetched_at: str | None = None,
) -> CapabilityResult:
    """Return bounded latest and recommended public review snapshots.

    Offline replay requires both ``latest_html`` and ``recommended_html``.
    Live execution makes exactly two sequential public HTML GETs and never
    calls the review GraphQL endpoint.
    """

    normalized_owner_reply = (
        OWNER_REPLY_EXCLUDE
        if owner_reply is True
        else OWNER_REPLY_ALL
        if owner_reply is False
        else owner_reply
    )
    request = {
        "place": str(place or "").strip(),
        "limit": limit,
        "owner_reply": normalized_owner_reply,
        "view": view,
        "request_budget": request_budget,
        "max_elapsed_seconds": max_elapsed_seconds,
        "samples": list(SAMPLE_ORDER),
    }
    if not request["place"]:
        return _invalid(request, "place must be a numeric Place ID or Naver Place URL")
    try:
        place_id = extract_place_id(request["place"])
    except ValueError as exc:
        return _invalid(request, str(exc))
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 0 <= limit <= MAX_SAMPLE_REVIEWS
    ):
        return _invalid(
            request,
            f"limit must be an integer from 0 to {MAX_SAMPLE_REVIEWS}",
        )
    if normalized_owner_reply not in OWNER_REPLY_FILTERS:
        return _invalid(
            request, f"unsupported owner_reply filter: {normalized_owner_reply}"
        )
    if view not in VIEWS:
        return _invalid(request, f"unsupported view: {view}")
    if (
        isinstance(request_budget, bool)
        or not isinstance(request_budget, int)
        or not 1 <= request_budget <= 100
    ):
        return _invalid(
            request, "request_budget must be an integer from 1 to 100"
        )
    if (
        isinstance(max_elapsed_seconds, bool)
        or not isinstance(max_elapsed_seconds, (int, float))
        or not math.isfinite(max_elapsed_seconds)
        or max_elapsed_seconds <= 0
    ):
        return _invalid(
            request, "max_elapsed_seconds must be greater than zero"
        )
    if headers and any(
        str(key).casefold() in AUTH_HEADER_NAMES for key in headers
    ):
        return _invalid(
            request,
            "authenticated headers are outside the public read-only boundary",
        )
    offline_values = (latest_html is not None, recommended_html is not None)
    if any(offline_values) and not all(offline_values):
        return _invalid(
            request,
            "latest_html and recommended_html must be supplied together",
        )
    offline = all(offline_values)
    if transport is not None and offline:
        return _invalid(
            request, "transport cannot be combined with offline review HTML"
        )
    if (latest_source_url is not None or recommended_source_url is not None) and not offline:
        return _invalid(
            request, "source URLs may only label offline review HTML fixtures"
        )

    if limit == 0:
        samples = _empty_sample_payloads()
        return CapabilityResult(
            capability=CAPABILITY,
            request=request,
            data=_data_payload(place_id, samples, []),
            status=Status.EMPTY,
            completeness=Completeness(
                complete=True,
                stop_reason="requested_limit",
                pages_fetched=0,
                requested_count=0,
                returned_count=0,
            ),
            budget=_transport_budget(transport, budget),
        )

    replayed_at = _utc_now()
    timestamp = fetched_at or ("unknown" if offline else replayed_at)
    active_transport = transport
    active_budget = budget
    if not offline:
        active_budget = active_budget or RequestBudget(
            max_requests=request_budget,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        active_transport = active_transport or Transport(budget=active_budget)
        active_budget = _transport_budget(active_transport, active_budget)
    active_headers = {
        **build_mobile_map_headers(user_agent),
        **dict(headers or {}),
    }

    latest_url = latest_source_url or build_latest_review_url(place_id)
    recommended_url = recommended_source_url or build_recommended_review_url(place_id)
    provenance: list[Provenance] = []

    def load_html(
        supplied: str | None,
        *,
        url: str,
        operation: str,
    ) -> tuple[str, str]:
        if supplied is not None:
            return supplied, url
        assert active_transport is not None
        response = active_transport.request(
            "GET",
            url,
            operation=operation,
            read_only=True,
            headers=active_headers,
        )
        response_url = str(getattr(response, "url", "") or url)
        return _response_text(response), response_url

    def parse_with_budget(parser: Any, html: str) -> Any:
        if active_budget is None:
            return parser(html, place_id)
        with active_budget.deadline():
            return parser(html, place_id)

    try:
        latest_response_html, latest_observed_url = load_html(
            latest_html,
            url=latest_url,
            operation=LATEST_OPERATION,
        )
        latest_samples = parse_with_budget(
            parse_latest_review_html, latest_response_html
        )
    except TransportError as exc:
        error = exc.error
        provenance.append(
            Provenance(
                source=SOURCE,
                operation=LATEST_OPERATION,
                fetched_at=timestamp,
                live=True,
                detail={
                    "url": latest_url,
                    "outcome": "error",
                    "error_code": str(error.code),
                },
            )
        )
        return _error_result(
            request=request,
            place_id=place_id,
            error=error,
            provenance=tuple(provenance),
            budget=active_budget,
        )
    except BudgetExceeded as exc:
        error = exc.to_error(LATEST_OPERATION)
        return _error_result(
            request=request,
            place_id=place_id,
            error=error,
            provenance=tuple(provenance),
            budget=active_budget,
        )
    except (ReviewSchemaError, TypeError, ValueError) as exc:
        error = CapabilityError(
            code=ErrorCode.UPSTREAM_CHANGED,
            message=str(exc),
            operation=LATEST_OPERATION,
        )
        provenance.append(
            Provenance(
                source="fixture" if offline else SOURCE,
                operation=FIXTURE_OPERATION if offline else LATEST_OPERATION,
                fetched_at=timestamp,
                live=not offline,
                detail={
                    "url": latest_url,
                    "outcome": "error",
                    "error_code": str(error.code),
                    **(
                        {"replays": LATEST_OPERATION, "replayed_at": replayed_at}
                        if offline
                        else {}
                    ),
                },
            )
        )
        return _error_result(
            request=request,
            place_id=place_id,
            error=error,
            provenance=tuple(provenance),
            budget=active_budget,
        )

    latest_count = len(latest_samples[SAMPLE_LATEST][0])
    if not offline:
        recommended_url = (
            _recommended_url_from_latest(latest_observed_url, place_id)
            or recommended_url
        )
    provenance.append(
        Provenance(
            source="fixture" if offline else SOURCE,
            operation=FIXTURE_OPERATION if offline else LATEST_OPERATION,
            fetched_at=timestamp,
            live=not offline,
            detail={
                "url": latest_observed_url,
                **(
                    {"requested_url": latest_url}
                    if latest_observed_url != latest_url
                    else {}
                ),
                "sort": "latest",
                "source_count": latest_count,
                **({"replays": LATEST_OPERATION, "replayed_at": replayed_at} if offline else {}),
            },
        )
    )

    try:
        recommended_response_html, recommended_observed_url = load_html(
            recommended_html,
            url=recommended_url,
            operation=RECOMMENDED_OPERATION,
        )
        recommended_samples = parse_with_budget(
            parse_recommended_review_html, recommended_response_html
        )
    except TransportError as exc:
        error = exc.error
        provenance.append(
            Provenance(
                source=SOURCE,
                operation=RECOMMENDED_OPERATION,
                fetched_at=timestamp,
                live=True,
                detail={
                    "url": recommended_url,
                    "outcome": "error",
                    "error_code": str(error.code),
                },
            )
        )
        partial_payloads, partial_selected = _sample_payloads(
            latest_samples,
            limit=limit,
            owner_reply=str(normalized_owner_reply),
        )
        partial_reviews = _merge_reviews(partial_selected, view)
        return _error_result(
            request=request,
            place_id=place_id,
            error=error,
            provenance=tuple(provenance),
            budget=active_budget,
            sample_payloads=partial_payloads,
            reviews=partial_reviews,
            pages_fetched=1,
        )
    except BudgetExceeded as exc:
        error = exc.to_error(RECOMMENDED_OPERATION)
        partial_payloads, partial_selected = _sample_payloads(
            latest_samples,
            limit=limit,
            owner_reply=str(normalized_owner_reply),
        )
        partial_reviews = _merge_reviews(partial_selected, view)
        return _error_result(
            request=request,
            place_id=place_id,
            error=error,
            provenance=tuple(provenance),
            budget=active_budget,
            sample_payloads=partial_payloads,
            reviews=partial_reviews,
            pages_fetched=1,
        )
    except (ReviewSchemaError, TypeError, ValueError) as exc:
        error = CapabilityError(
            code=ErrorCode.UPSTREAM_CHANGED,
            message=str(exc),
            operation=RECOMMENDED_OPERATION,
        )
        provenance.append(
            Provenance(
                source="fixture" if offline else SOURCE,
                operation=FIXTURE_OPERATION if offline else RECOMMENDED_OPERATION,
                fetched_at=timestamp,
                live=not offline,
                detail={
                    "url": recommended_url,
                    "outcome": "error",
                    "error_code": str(error.code),
                    **(
                        {
                            "replays": RECOMMENDED_OPERATION,
                            "replayed_at": replayed_at,
                        }
                        if offline
                        else {}
                    ),
                },
            )
        )
        partial_payloads, partial_selected = _sample_payloads(
            latest_samples,
            limit=limit,
            owner_reply=str(normalized_owner_reply),
        )
        partial_reviews = _merge_reviews(partial_selected, view)
        return _error_result(
            request=request,
            place_id=place_id,
            error=error,
            provenance=tuple(provenance),
            budget=active_budget,
            sample_payloads=partial_payloads,
            reviews=partial_reviews,
            pages_fetched=1,
        )

    provenance.append(
        Provenance(
            source="fixture" if offline else SOURCE,
            operation=FIXTURE_OPERATION if offline else RECOMMENDED_OPERATION,
            fetched_at=timestamp,
            live=not offline,
            detail={
                "url": recommended_observed_url,
                **(
                    {"requested_url": recommended_url}
                    if recommended_observed_url != recommended_url
                    else {}
                ),
                "sort": "recommended",
                "content_source_count": len(
                    recommended_samples[SAMPLE_RECOMMENDED][0]
                ),
                "keyword_source_count": len(
                    recommended_samples[SAMPLE_RECOMMENDED_KEYWORD_ONLY][0]
                ),
                **(
                    {
                        "replays": RECOMMENDED_OPERATION,
                        "replayed_at": replayed_at,
                    }
                    if offline
                    else {}
                ),
            },
        )
    )

    raw_samples = {**latest_samples, **recommended_samples}
    sample_payloads, selected = _sample_payloads(
        raw_samples,
        limit=limit,
        owner_reply=str(normalized_owner_reply),
    )
    visible = _merge_reviews(selected, view)
    returned_slots = sum(
        int(sample["returned_count"]) for sample in sample_payloads.values()
    )
    return CapabilityResult(
        capability=CAPABILITY,
        request=request,
        data=_data_payload(place_id, sample_payloads, visible),
        status=Status.OK if visible else Status.EMPTY,
        provenance=tuple(provenance),
        completeness=Completeness(
            complete=True,
            stop_reason="snapshot_complete",
            pages_fetched=2,
            requested_count=limit * len(SAMPLE_ORDER),
            returned_count=returned_slots,
        ),
        budget=active_budget,
    )


reviews = get_reviews
review = get_reviews


__all__ = [
    "CAPABILITY",
    "MAX_SAMPLE_REVIEWS",
    "OWNER_REPLY_ALL",
    "OWNER_REPLY_EXCLUDE",
    "OWNER_REPLY_ONLY",
    "ReviewSchemaError",
    "build_latest_review_url",
    "build_recommended_review_url",
    "get_reviews",
    "parse_latest_review_html",
    "parse_recommended_review_html",
    "review",
    "reviews",
]
