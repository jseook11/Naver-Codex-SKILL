"""Naver Place detail capability.

The home page is the required resource.  Feed and business-hours surfaces are
optional secondary operations: failures there preserve useful base data and
produce an explicit partial result.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse

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


CAPABILITY = "place.detail"
SOURCE = "naver-place-public"
FIXTURE_OPERATION = "fixture.replay"
HOME_OPERATION = "place.home_html"
FEED_OPERATION = "place.feed_html"
HOURS_OPERATION = "place.hours_api"
PLACE_URL_TEMPLATE = "https://m.place.naver.com/place/{place_id}/{section}"
PLACE_GRAPHQL_URL = "https://api.place.naver.com/graphql"
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


def _legacy_parser() -> Any:
    """Load the proven normalizer shared with the historical CLI adapter."""

    from naver_place._legacy import scrape_naver_place_detail

    return scrape_naver_place_detail


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_place_id(value: str) -> str:
    clean = str(value).strip()
    if re.fullmatch(r"\d+", clean):
        return clean
    parsed = urlparse(clean)
    host = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("place must be a numeric ID or a Naver Place URL") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not (host == "naver.com" or host.endswith(".naver.com"))
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("place must be a numeric ID or a Naver Place URL")
    match = re.search(r"/place/(\d+)(?:/|$)", parsed.path)
    if not match:
        raise ValueError("the Naver Place URL does not contain a place ID")
    return match.group(1)


def build_place_url(place_id: str, section: str = "home") -> str:
    return PLACE_URL_TEMPLATE.format(place_id=place_id, section=section)


def _code_value(code: Any) -> str:
    return str(getattr(code, "value", code))


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


def _response_json(response: Any) -> dict[str, Any]:
    json_method = getattr(response, "json", None)
    value = json_method() if callable(json_method) else json.loads(_response_text(response))
    if not isinstance(value, dict):
        raise ValueError("Naver Place response root is not an object")
    return value


def _transport_budget(transport: Any, fallback: RequestBudget | None) -> RequestBudget | None:
    return getattr(transport, "budget", None) or fallback


def _provenance(
    operation: str,
    timestamp: str,
    *,
    live: bool,
    url: str,
    outcome: str = "ok",
    error: CapabilityError | None = None,
) -> Provenance:
    detail: dict[str, Any] = {
        "url": url,
        "outcome": outcome,
    }
    if not live:
        detail["replays"] = operation
        detail["replayed_at"] = _utc_now()
    if error is not None:
        detail["error_code"] = _code_value(error.code)
        if error.http_status is not None:
            detail["http_status"] = error.http_status
    return Provenance(
        source=SOURCE if live else "fixture",
        operation=operation if live else FIXTURE_OPERATION,
        fetched_at=timestamp,
        live=live,
        detail=detail,
    )


def _error_result(
    request: Mapping[str, Any],
    error: CapabilityError,
    *,
    provenance: tuple[Provenance, ...] = (),
    budget: RequestBudget | None = None,
) -> CapabilityResult:
    return CapabilityResult(
        capability=CAPABILITY,
        request=request,
        data={},
        status=Status.ERROR,
        errors=(error,),
        provenance=provenance,
        completeness=Completeness(complete=False, stop_reason=_code_value(error.code)),
        budget=budget,
    )


def _invalid_result(
    place: str,
    message: str,
    *,
    include_feed: bool,
    include_hours: bool,
) -> CapabilityResult:
    request = {
        "place": str(place),
        "include_feed": bool(include_feed),
        "include_hours": bool(include_hours),
    }
    return _error_result(
        request,
        CapabilityError(
            code=ErrorCode.INVALID_INPUT,
            message=message,
            operation=HOME_OPERATION,
        ),
    )


def _secondary_not_found_code() -> Any:
    return getattr(ErrorCode, "SECONDARY_NOT_FOUND", "secondary_not_found")


def _as_secondary_error(error: CapabilityError) -> CapabilityError:
    if _code_value(error.code) != _code_value(ErrorCode.NOT_FOUND):
        return error
    return CapabilityError(
        code=_secondary_not_found_code(),
        message=error.message,
        operation=error.operation,
        http_status=error.http_status,
        retryable=error.retryable,
        detail=error.detail,
    )


def _offline_missing_error(operation: str, *, secondary: bool = False) -> CapabilityError:
    return CapabilityError(
        code=_secondary_not_found_code() if secondary else ErrorCode.INVALID_INPUT,
        message=f"offline replay is missing content for enabled operation {operation}",
        operation=operation,
    )


def _schema_error(operation: str, message: str) -> CapabilityError:
    return CapabilityError(
        code=ErrorCode.UPSTREAM_CHANGED,
        message=message,
        operation=operation,
    )


def _validate_feed_html(feed_html: str) -> None:
    _legacy_parser().extract_apollo_state(feed_html)


def _validate_hours_payload(payload: Mapping[str, Any]) -> None:
    data = payload.get("data")
    business = data.get("business") if isinstance(data, Mapping) else None
    if not isinstance(business, Mapping):
        raise ValueError("business-hours response is missing data.business")
    if not isinstance(business.get("newBusinessHours"), list):
        raise ValueError("business-hours response is missing newBusinessHours")


def _hours_request_payload(place_id: str) -> dict[str, Any]:
    legacy = _legacy_parser()
    return {
        "operationName": "getDetail",
        "variables": {"id": place_id, "deviceType": "mobile"},
        "query": legacy.BUSINESS_HOURS_GRAPHQL_QUERY,
    }


def get_place_detail(
    place: str,
    *,
    include_feed: bool = True,
    include_hours: bool = True,
    transport: Transport | None = None,
    budget: RequestBudget | None = None,
    home_html: str | None = None,
    feed_html: str | None = None,
    business_hours_payload: Mapping[str, Any] | None = None,
    offline: bool = False,
    fetched_at: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> CapabilityResult:
    """Return base Place data and requested optional secondary sources.

    ``home_html``, ``feed_html`` and ``business_hours_payload`` support
    sanitized offline replay.  Set ``offline=True`` to guarantee that absent
    secondary inputs never cause an outbound request.
    """

    try:
        place_id = extract_place_id(place)
    except (TypeError, ValueError) as exc:
        return _invalid_result(
            place,
            str(exc),
            include_feed=include_feed,
            include_hours=include_hours,
        )

    request = {
        "place": str(place).strip(),
        "place_id": place_id,
        "include_feed": bool(include_feed),
        "include_hours": bool(include_hours),
    }
    if budget is not None:
        try:
            budget.check()
        except BudgetExceeded as exc:
            return _error_result(request, exc.to_error(HOME_OPERATION), budget=budget)
    invoked_at = _utc_now()
    timestamp = fetched_at or invoked_at
    fixture_timestamp = fetched_at or "unknown"
    home_is_fixture = home_html is not None
    home_timestamp = fixture_timestamp if home_is_fixture else timestamp
    active_transport = transport
    request_headers = {**DEFAULT_HEADERS, **dict(headers or {})}
    provenances: list[Provenance] = []
    secondary_errors: list[CapabilityError] = []
    halt_optional_live_requests = False

    home_url = build_place_url(place_id, "home")
    if home_html is None:
        if offline:
            return _error_result(
                request,
                _offline_missing_error(HOME_OPERATION),
                budget=budget,
            )
        active_transport = active_transport or Transport(budget=budget)
        try:
            response = active_transport.request(
                "GET",
                home_url,
                operation=HOME_OPERATION,
                read_only=True,
                headers=request_headers,
            )
        except TransportError as exc:
            provenances.append(
                _provenance(
                    HOME_OPERATION,
                    timestamp,
                    live=True,
                    url=home_url,
                    outcome="error",
                    error=exc.error,
                )
            )
            return _error_result(
                request,
                exc.error,
                provenance=tuple(provenances),
                budget=_transport_budget(active_transport, budget),
            )
        home_html = _response_text(response)
        provenances.append(_provenance(HOME_OPERATION, timestamp, live=True, url=home_url))
    else:
        provenances.append(
            _provenance(
                HOME_OPERATION, fixture_timestamp, live=False, url=home_url
            )
        )

    # The required base must be trustworthy before any optional outbound work.
    # This also avoids spending request budget when the home schema has drifted.
    preliminary_budget = _transport_budget(active_transport, budget)
    try:
        if preliminary_budget is not None:
            with preliminary_budget.deadline():
                preliminary = _legacy_parser().parse_place_detail_html(
                    home_html,
                    place_id,
                    None,
                    None,
                    fetched_at=home_timestamp,
                )
        else:
            preliminary = _legacy_parser().parse_place_detail_html(
                home_html,
                place_id,
                None,
                None,
                fetched_at=home_timestamp,
            )
    except BudgetExceeded as exc:
        return _error_result(
            request,
            exc.to_error(HOME_OPERATION),
            provenance=tuple(provenances),
            budget=preliminary_budget,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return _error_result(
            request,
            _schema_error(HOME_OPERATION, str(exc)),
            provenance=tuple(provenances),
            budget=preliminary_budget,
        )
    preliminary_base = preliminary.get("base")
    if not isinstance(preliminary_base, dict) or not preliminary_base.get("name"):
        return _error_result(
            request,
            _schema_error(
                HOME_OPERATION, "home response has no normalized Place base record"
            ),
            provenance=tuple(provenances),
            budget=preliminary_budget,
        )

    normalized_feed_html: str | None = None
    feed_url = build_place_url(place_id, "feed")
    if include_feed:
        if feed_html is not None:
            try:
                _validate_feed_html(feed_html)
            except (TypeError, ValueError) as exc:
                error = _schema_error(FEED_OPERATION, str(exc))
                secondary_errors.append(error)
                provenances.append(
                    _provenance(
                        FEED_OPERATION,
                        fixture_timestamp,
                        live=False,
                        url=feed_url,
                        outcome="error",
                        error=error,
                    )
                )
            else:
                normalized_feed_html = feed_html
                provenances.append(
                    _provenance(
                        FEED_OPERATION,
                        fixture_timestamp,
                        live=False,
                        url=feed_url,
                    )
                )
        elif offline:
            error = _offline_missing_error(FEED_OPERATION, secondary=True)
            secondary_errors.append(error)
            provenances.append(
                _provenance(
                    FEED_OPERATION,
                    fixture_timestamp,
                    live=False,
                    url=feed_url,
                    outcome="error",
                    error=error,
                )
            )
        else:
            active_transport = active_transport or Transport(budget=budget)
            try:
                response = active_transport.request(
                    "GET",
                    feed_url,
                    operation=FEED_OPERATION,
                    read_only=True,
                    headers=request_headers,
                )
                candidate_feed_html = _response_text(response)
                _validate_feed_html(candidate_feed_html)
            except TransportError as exc:
                error = _as_secondary_error(exc.error)
                secondary_errors.append(error)
                halt_optional_live_requests = _code_value(error.code) in {
                    _code_value(ErrorCode.BLOCKED),
                    _code_value(ErrorCode.RATE_LIMITED),
                }
                provenances.append(
                    _provenance(
                        FEED_OPERATION,
                        timestamp,
                        live=True,
                        url=feed_url,
                        outcome="error",
                        error=error,
                    )
                )
            except (TypeError, ValueError) as exc:
                error = _schema_error(FEED_OPERATION, str(exc))
                secondary_errors.append(error)
                provenances.append(
                    _provenance(
                        FEED_OPERATION,
                        timestamp,
                        live=True,
                        url=feed_url,
                        outcome="error",
                        error=error,
                    )
                )
            else:
                normalized_feed_html = candidate_feed_html
                provenances.append(_provenance(FEED_OPERATION, timestamp, live=True, url=feed_url))

    normalized_hours_payload: dict[str, Any] | None = None
    if include_hours:
        if business_hours_payload is not None:
            try:
                _validate_hours_payload(business_hours_payload)
            except (TypeError, ValueError) as exc:
                error = _schema_error(HOURS_OPERATION, str(exc))
                secondary_errors.append(error)
                provenances.append(
                    _provenance(
                        HOURS_OPERATION,
                        fixture_timestamp,
                        live=False,
                        url=PLACE_GRAPHQL_URL,
                        outcome="error",
                        error=error,
                    )
                )
            else:
                normalized_hours_payload = dict(business_hours_payload)
                provenances.append(
                    _provenance(
                        HOURS_OPERATION,
                        fixture_timestamp,
                        live=False,
                        url=PLACE_GRAPHQL_URL,
                    )
                )
        elif offline:
            # Embedded hours in the home Apollo state remain a valid source.
            # We decide whether they satisfied the request after normalization.
            pass
        elif not halt_optional_live_requests:
            active_transport = active_transport or Transport(budget=budget)
            graphql_headers = {
                **request_headers,
                "accept": "*/*",
                "accept-language": "ko",
                "content-type": "application/json",
                "origin": "https://m.place.naver.com",
                "referer": home_url,
            }
            try:
                response = active_transport.request(
                    "POST",
                    PLACE_GRAPHQL_URL,
                    operation=HOURS_OPERATION,
                    read_only=True,
                    headers=graphql_headers,
                    json=_hours_request_payload(place_id),
                )
                candidate_hours = _response_json(response)
                _validate_hours_payload(candidate_hours)
            except TransportError as exc:
                error = _as_secondary_error(exc.error)
                secondary_errors.append(error)
                provenances.append(
                    _provenance(
                        HOURS_OPERATION,
                        timestamp,
                        live=True,
                        url=PLACE_GRAPHQL_URL,
                        outcome="error",
                        error=error,
                    )
                )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                error = _schema_error(HOURS_OPERATION, str(exc))
                secondary_errors.append(error)
                provenances.append(
                    _provenance(
                        HOURS_OPERATION,
                        timestamp,
                        live=True,
                        url=PLACE_GRAPHQL_URL,
                        outcome="error",
                        error=error,
                    )
                )
            else:
                normalized_hours_payload = candidate_hours
                provenances.append(
                    _provenance(HOURS_OPERATION, timestamp, live=True, url=PLACE_GRAPHQL_URL)
                )

    active_budget = _transport_budget(active_transport, budget)
    try:
        if active_budget is not None:
            with active_budget.deadline():
                payload = _legacy_parser().parse_place_detail_html(
                    home_html,
                    place_id,
                    normalized_feed_html,
                    normalized_hours_payload,
                    fetched_at=home_timestamp,
                )
        else:
            payload = _legacy_parser().parse_place_detail_html(
                home_html,
                place_id,
                normalized_feed_html,
                normalized_hours_payload,
                fetched_at=home_timestamp,
            )
    except BudgetExceeded as exc:
        return _error_result(
            request,
            exc.to_error(HOME_OPERATION),
            provenance=tuple(provenances),
            budget=active_budget,
        )
    except (KeyError, TypeError, ValueError) as exc:
        error = _schema_error(HOME_OPERATION, str(exc))
        return _error_result(
            request,
            error,
            provenance=tuple(provenances),
            budget=_transport_budget(active_transport, budget),
        )

    base = payload.get("base")
    if not isinstance(base, dict) or not base.get("name"):
        error = _schema_error(HOME_OPERATION, "home response has no normalized Place base record")
        return _error_result(
            request,
            error,
            provenance=tuple(provenances),
            budget=_transport_budget(active_transport, budget),
        )

    if include_hours and business_hours_payload is None and offline and not payload.get("business_hours"):
        error = _offline_missing_error(HOURS_OPERATION, secondary=True)
        secondary_errors.append(error)
        provenances.append(
            _provenance(
                HOURS_OPERATION,
                fixture_timestamp,
                live=False,
                url=PLACE_GRAPHQL_URL,
                outcome="error",
                error=error,
            )
        )

    status = Status.PARTIAL if secondary_errors else Status.OK
    stop_reason = _code_value(secondary_errors[0].code) if secondary_errors else None
    return CapabilityResult(
        capability=CAPABILITY,
        request=request,
        data=payload,
        status=status,
        errors=tuple(secondary_errors),
        provenance=tuple(provenances),
        completeness=Completeness(
            complete=not secondary_errors,
            stop_reason=stop_reason,
            returned_count=1,
        ),
        budget=_transport_budget(active_transport, budget),
    )


detail = get_place_detail


__all__ = ["CAPABILITY", "build_place_url", "detail", "extract_place_id", "get_place_detail"]
