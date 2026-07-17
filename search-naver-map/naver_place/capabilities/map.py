"""Naver Map search capability.

This adapter owns the mobile-map HTML shape and local result filtering.  It
does not decide what to search for or call another capability on the agent's
behalf.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode

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
from naver_place.place import PlaceSummary
from naver_place.transport import Transport, TransportError


CAPABILITY = "map.search"
SOURCE = "naver-map-public"
OPERATION = "map.search_html"
FIXTURE_OPERATION = "fixture.replay"
MOBILE_MAP_SEARCH_URL = "https://m.map.naver.com/search2/search.naver"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
    "Referer": "https://m.map.naver.com/",
}
ALLOWED_SORTS = {"", "relativity", "distance"}


class MapSchemaError(ValueError):
    """The public map response no longer has the expected embedded payload."""


def build_search_url(query: str, sort: str = "") -> str:
    params = {"query": query, "sm": "hty", "style": "v5"}
    if sort:
        params["siteSort"] = sort
    return f"{MOBILE_MAP_SEARCH_URL}?{urlencode(params)}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_balanced_json(text: str, start: int) -> str:
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] not in "[{":
        return ""

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _streaming_payloads(html: str) -> list[dict[str, Any]]:
    marker = "window.__RQ_STREAMING_STATE__.push("
    payloads: list[dict[str, Any]] = []
    cursor = 0
    while True:
        marker_index = html.find(marker, cursor)
        if marker_index < 0:
            return payloads
        start = marker_index + len(marker)
        payload_text = _extract_balanced_json(html, start)
        cursor = start + max(1, len(payload_text))
        if not payload_text:
            continue
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)


def _strip_text(value: Any) -> str:
    if value is None:
        return ""
    value = re.sub(r"<[^>]+>", "", str(value))
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _find_result_data(html: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for payload in _streaming_payloads(html):
        queries = payload.get("queries")
        if not isinstance(queries, list):
            continue
        for query in queries:
            if not isinstance(query, dict):
                continue
            state = query.get("state")
            data = state.get("data") if isinstance(state, dict) else None
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                candidates.append(data)

    if not candidates:
        raise MapSchemaError("Naver Map embedded search data was not found")

    # A response may contain more than one React Query cache entry.  Prefer the
    # one with the most parseable place rows, while still accepting a genuine
    # zero-result payload (items=[]).
    return max(
        candidates,
        key=lambda data: sum(
            1
            for item in data.get("items", [])
            if isinstance(item, dict) and item.get("id") and item.get("name")
        ),
    )


def parse_search_html(html: str) -> dict[str, Any]:
    """Parse one mobile-map response into the capability-owned normalized shape."""

    data = _find_result_data(html)
    places: list[PlaceSummary] = []
    seen_ids: set[str] = set()
    upstream_items = data.get("items", [])
    for upstream_rank, item in enumerate(upstream_items, start=1):
        if not isinstance(item, dict):
            continue
        place_id = _strip_text(item.get("id"))
        name = _strip_text(item.get("name"))
        if not place_id or not name or place_id in seen_ids:
            continue
        seen_ids.add(place_id)
        places.append(
            PlaceSummary(
                place_id=place_id,
                name=name,
                rank=upstream_rank,
                category=_strip_text(item.get("category")),
                address=_strip_text(item.get("address")),
                road_address=_strip_text(item.get("roadAddress")),
                telephone=_strip_text(item.get("tel")),
                virtual_telephone=_strip_text(item.get("virtualTel")),
                latitude=_float_or_none(item.get("latitude")),
                longitude=_float_or_none(item.get("longitude")),
                place_url=f"https://m.place.naver.com/place/{place_id}/home",
                reservation_url=_strip_text(item.get("reservationUrl")),
                has_menu_info=bool(item.get("hasMenuInfo")),
                has_npay=bool(item.get("hasNPay")),
            )
        )

    total_count = data.get("totalCount")
    try:
        reported_nonempty = int(total_count) > 0
    except (TypeError, ValueError):
        reported_nonempty = False
    if not places and (bool(upstream_items) or reported_nonempty):
        raise MapSchemaError(
            "Naver Map returned non-empty search data without recognizable Place fields"
        )

    return {
        "total_count": total_count,
        "source_item_count": len(upstream_items),
        "search_type": _strip_text(data.get("searchType")),
        "location_query_info": _strip_text(data.get("locationQueryInfo")),
        "page_info": data.get("pageInfo") if isinstance(data.get("pageInfo"), dict) else {},
        "places": places,
    }


def _normalize_terms(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(term.casefold() for value in values or () if (term := _strip_text(value)))


def _search_text(place: PlaceSummary) -> str:
    return " ".join(
        (
            place.name,
            place.category,
            place.address,
            place.road_address,
            place.telephone,
            place.virtual_telephone,
        )
    ).casefold()


def _apply_text_filters(
    places: Sequence[PlaceSummary],
    include_text: Iterable[str] | None,
    exclude_text: Iterable[str] | None,
) -> list[PlaceSummary]:
    include = _normalize_terms(include_text)
    exclude = _normalize_terms(exclude_text)
    output: list[PlaceSummary] = []
    for place in places:
        corpus = _search_text(place)
        if include and not all(term in corpus for term in include):
            continue
        if exclude and any(term in corpus for term in exclude):
            continue
        output.append(place)
    return output


def _target_payload(
    places: Sequence[PlaceSummary],
    target_place_id: str | None,
    target_name: str | None,
) -> dict[str, Any] | None:
    place_id = (target_place_id or "").strip()
    normalized_name = re.sub(r"\s+", "", target_name or "").casefold()
    if not place_id and not normalized_name:
        return None

    match = next(
        (
            place
            for place in places
            if (place_id and place.place_id == place_id)
            or (
                normalized_name
                and normalized_name in re.sub(r"\s+", "", place.name).casefold()
            )
        ),
        None,
    )
    return {
        "place_id": place_id or None,
        "name": _strip_text(target_name) or None,
        "found": match is not None,
        "rank": match.rank if match else None,
        "place": match.to_dict() if match else None,
    }


def _response_text(response: Any) -> str:
    content = getattr(response, "content", b"")
    if isinstance(content, bytes) and content:
        encoding = str(getattr(response, "encoding", "") or "").lower()
        if not encoding or encoding in {"iso-8859-1", "latin-1"}:
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                encoding = str(getattr(response, "apparent_encoding", "") or "utf-8")
        return content.decode(encoding, errors="replace")
    text = getattr(response, "text", None)
    return text if isinstance(text, str) else str(content)


def _invalid_result(request: Mapping[str, Any], message: str) -> CapabilityResult:
    error = CapabilityError(code=ErrorCode.INVALID_INPUT, message=message, operation=OPERATION)
    return CapabilityResult(
        capability=CAPABILITY,
        request=request,
        data={},
        status=Status.ERROR,
        errors=(error,),
        completeness=Completeness(complete=False, stop_reason="invalid_input"),
    )


def _transport_budget(transport: Any, fallback: RequestBudget | None) -> RequestBudget | None:
    return getattr(transport, "budget", None) or fallback


def search_places(
    query: str,
    *,
    limit: int = 20,
    sort: str = "",
    target_place_id: str | None = None,
    target_name: str | None = None,
    include_text: Iterable[str] | None = None,
    exclude_text: Iterable[str] | None = None,
    transport: Transport | None = None,
    budget: RequestBudget | None = None,
    html: str | None = None,
    source_url: str | None = None,
    fetched_at: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> CapabilityResult:
    """Search Naver Map without requiring a target Place.

    Passing ``html`` performs a deterministic offline replay and never creates
    or calls a transport.  Live callers may inject a ``Transport`` for tests,
    request budgeting, or a custom session.
    """

    clean_query = _strip_text(query)
    request = {
        "query": clean_query,
        "limit": limit,
        "sort": sort,
        "target_place_id": (target_place_id or "").strip() or None,
        "target_name": _strip_text(target_name) or None,
        "include_text": list(_normalize_terms(include_text)),
        "exclude_text": list(_normalize_terms(exclude_text)),
    }
    if not clean_query:
        return _invalid_result(request, "query must be a non-empty string")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        return _invalid_result(request, "limit must be an integer from 1 to 100")
    if sort not in ALLOWED_SORTS:
        return _invalid_result(request, f"unsupported map sort: {sort}")
    if target_place_id and not re.fullmatch(r"\d+", target_place_id.strip()):
        return _invalid_result(request, "target_place_id must contain digits only")
    if source_url is not None and html is None:
        return _invalid_result(
            request, "source_url may only label an offline HTML fixture"
        )

    url = source_url or build_search_url(clean_query, sort)
    replayed_at = _utc_now()
    timestamp = fetched_at or ("unknown" if html is not None else replayed_at)
    active_transport = transport
    if html is not None:
        response_html = html
        provenance = Provenance(
            source="fixture",
            operation=FIXTURE_OPERATION,
            fetched_at=timestamp,
            live=False,
            detail={"replays": OPERATION, "url": url, "replayed_at": replayed_at},
        )
    else:
        active_transport = active_transport or Transport(budget=budget)
        try:
            response = active_transport.request(
                "GET",
                url,
                operation=OPERATION,
                read_only=True,
                headers={**DEFAULT_HEADERS, **dict(headers or {})},
            )
        except TransportError as exc:
            error = exc.error
            if error.code == ErrorCode.NOT_FOUND:
                error = CapabilityError(
                    code=ErrorCode.UPSTREAM_REJECTED,
                    message="Naver Map search endpoint returned HTTP 404",
                    operation=OPERATION,
                    http_status=error.http_status,
                    retryable=False,
                    detail=error.detail,
                )
            provenance = Provenance(
                source=SOURCE,
                operation=OPERATION,
                fetched_at=timestamp,
                live=True,
                detail={
                    "url": url,
                    "outcome": "error",
                    "error_code": str(error.code),
                    **(
                        {"http_status": error.http_status}
                        if error.http_status is not None
                        else {}
                    ),
                },
            )
            return CapabilityResult(
                capability=CAPABILITY,
                request=request,
                data={},
                status=Status.ERROR,
                errors=(error,),
                provenance=(provenance,),
                completeness=Completeness(complete=False, stop_reason=str(error.code)),
                budget=_transport_budget(active_transport, budget),
            )
        response_html = _response_text(response)
        provenance = Provenance(
            source=SOURCE,
            operation=OPERATION,
            fetched_at=timestamp,
            live=True,
            detail={"url": url},
        )

    active_budget = _transport_budget(active_transport, budget)
    try:
        if active_budget is not None:
            with active_budget.deadline():
                parsed = parse_search_html(response_html)
        else:
            parsed = parse_search_html(response_html)
    except BudgetExceeded as exc:
        error = exc.to_error(OPERATION)
        return CapabilityResult(
            capability=CAPABILITY,
            request=request,
            data={},
            status=Status.ERROR,
            errors=(error,),
            provenance=(provenance,),
            completeness=Completeness(complete=False, stop_reason=str(error.code)),
            budget=active_budget,
        )
    except (MapSchemaError, TypeError, ValueError) as exc:
        error = CapabilityError(
            code=ErrorCode.UPSTREAM_CHANGED,
            message=str(exc),
            operation=OPERATION,
        )
        return CapabilityResult(
            capability=CAPABILITY,
            request=request,
            data={},
            status=Status.ERROR,
            errors=(error,),
            provenance=(provenance,),
            completeness=Completeness(complete=False, stop_reason="upstream_changed"),
            budget=_transport_budget(active_transport, budget),
        )

    upstream_places: list[PlaceSummary] = parsed.pop("places")
    filtered = _apply_text_filters(upstream_places, include_text, exclude_text)
    shown = filtered[:limit]
    source_item_count = int(parsed.get("source_item_count") or 0)
    try:
        reported_total = int(parsed.get("total_count"))
    except (TypeError, ValueError):
        reported_total = None
    page_info = parsed.get("page_info")
    has_next = bool(
        isinstance(page_info, Mapping)
        and any(
            page_info.get(key) is True
            for key in ("hasNext", "hasNextPage", "hasMore", "has_next", "has_more")
        )
    )
    source_truncated = (
        (reported_total is not None and reported_total > source_item_count) or has_next
    ) and len(shown) < limit
    if len(shown) >= limit:
        stop_reason = "requested_limit"
    elif source_truncated:
        stop_reason = "source_page_limit"
    else:
        stop_reason = "exhausted"
    target = _target_payload(upstream_places, target_place_id, target_name)
    data = {
        "query": clean_query,
        "url": url,
        **parsed,
        "upstream_returned_count": len(upstream_places),
        "post_filter_count": len(filtered),
        "returned_count": len(shown),
        "places": [place.to_dict() for place in shown],
    }
    if target is not None:
        data["target"] = target

    status = Status.PARTIAL if source_truncated else Status.OK if shown else Status.EMPTY
    warnings = (
        (
            "the public Map response reported more results than this source page; "
            "the returned set is not exhaustive"
        ),
    ) if source_truncated else ()
    return CapabilityResult(
        capability=CAPABILITY,
        request=request,
        data=data,
        status=status,
        warnings=warnings,
        provenance=(provenance,),
        completeness=Completeness(
            complete=not source_truncated,
            stop_reason=stop_reason,
            requested_count=limit,
            returned_count=len(shown),
        ),
        budget=_transport_budget(active_transport, budget),
    )


# A short verb works well for Python callers while keeping the public name
# explicit for discovery and documentation.
search = search_places


__all__ = ["CAPABILITY", "MapSchemaError", "build_search_url", "parse_search_html", "search", "search_places"]
