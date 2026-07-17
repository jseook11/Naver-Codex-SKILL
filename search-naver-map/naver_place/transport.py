"""Bounded HTTP execution and response classification for public surfaces."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlparse

import requests

from .contracts import (
    BudgetExceeded,
    CapabilityError,
    ErrorCode,
    RequestBudget,
    RequestPolicy,
)


REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SENSITIVE_REDIRECT_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "host",
}
ENTITY_HEADERS = {
    "content-encoding",
    "content-language",
    "content-length",
    "content-location",
    "content-type",
    "digest",
    "transfer-encoding",
}


class TransportError(RuntimeError):
    def __init__(self, error: CapabilityError, response: requests.Response | None = None) -> None:
        super().__init__(error.message)
        self.error = error
        self.response = response


def classify_status(status: int, *, operation: str) -> CapabilityError | None:
    if status < 300:
        return None
    if status < 400:
        return CapabilityError(
            code=ErrorCode.UPSTREAM_REJECTED,
            message=f"upstream returned an unresolved HTTP {status} redirect",
            operation=operation,
            http_status=status,
            retryable=False,
        )
    if status == 429:
        code, retryable = ErrorCode.RATE_LIMITED, True
    elif status in {401, 403}:
        code, retryable = ErrorCode.BLOCKED, False
    elif status == 404:
        code, retryable = ErrorCode.NOT_FOUND, False
    elif status == 405:
        code, retryable = ErrorCode.UPSTREAM_REJECTED, False
    elif status >= 500:
        code, retryable = ErrorCode.NETWORK_ERROR, True
    else:
        code, retryable = ErrorCode.UPSTREAM_REJECTED, False
    return CapabilityError(
        code=code,
        message=f"upstream returned HTTP {status}",
        operation=operation,
        http_status=status,
        retryable=retryable,
    )


def classify_exception(exc: BaseException, *, operation: str) -> CapabilityError:
    if isinstance(exc, BudgetExceeded):
        return exc.to_error(operation)
    return CapabilityError(
        code=ErrorCode.NETWORK_ERROR,
        message=str(exc) or exc.__class__.__name__,
        operation=operation,
        retryable=isinstance(exc, (requests.Timeout, requests.ConnectionError)),
        detail={"exception": exc.__class__.__name__},
    )


def classify_access_wall(response: Any, *, operation: str) -> CapabilityError | None:
    """Recognize strong login/CAPTCHA signals even when HTTP status is 200."""

    url = str(getattr(response, "url", "") or "").casefold()
    content = getattr(response, "content", b"")
    if isinstance(content, bytes):
        text = content[:200_000].decode("utf-8", errors="ignore").casefold()
    else:
        text = str(getattr(response, "text", "") or "")[:200_000].casefold()
    markers = {
        "자동입력 방지문자": "captcha",
        "보안문자를 입력": "captcha",
        "캡차 인증이 필요": "captcha",
        "로그인이 필요한 서비스": "login_required",
    }
    reason = "login_required" if "nidlogin" in url else None
    if "captcha" in url:
        reason = "captcha"
    if reason is None:
        reason = next((kind for marker, kind in markers.items() if marker in text), None)
    if reason is None:
        return None
    return CapabilityError(
        code=ErrorCode.BLOCKED,
        message=f"public operation encountered an access wall ({reason})",
        operation=operation,
        http_status=getattr(response, "status_code", None),
        retryable=False,
        detail={"reason": reason},
    )


def retry_after_seconds(value: str | None, *, now: Callable[[], float] = time.time) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, target.timestamp() - now())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def bounded_timeout(value: Any, remaining_seconds: float) -> Any:
    """Clamp connect/read phases so one attempt fits the remaining budget."""

    remaining = max(0.001, float(remaining_seconds))
    if isinstance(value, tuple) and len(value) == 2:
        connect, read = (max(0.001, float(part)) for part in value)
        total = connect + read
        if total <= remaining:
            return (connect, read)
        scale = remaining / total
        return (max(0.001, connect * scale), max(0.001, read * scale))
    timeout = max(0.001, float(value))
    # requests applies a scalar independently to connection and socket reads.
    return min(timeout, max(0.001, remaining / 2))


def redirect_allowed(source_url: str, target_url: str) -> bool:
    try:
        source_parsed = urlparse(source_url)
        target_parsed = urlparse(target_url)
        source_port = source_parsed.port
        target_port = target_parsed.port
    except ValueError:
        return False
    source = (source_parsed.hostname or "").lower()
    target = (target_parsed.hostname or "").lower()
    if not source or not target:
        return False
    if (
        source_parsed.scheme.lower() != "https"
        or target_parsed.scheme.lower() != "https"
        or source_port not in {None, 443}
        or target_port not in {None, 443}
        or target_parsed.username is not None
        or target_parsed.password is not None
    ):
        return False
    if source == target:
        return True
    return (source == "naver.com" or source.endswith(".naver.com")) and (
        target == "naver.com" or target.endswith(".naver.com")
    )


def _drop_headers(headers: Mapping[str, str], names: set[str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).casefold() not in names
    }


def _has_session_cookies(session: Any) -> bool:
    cookies = getattr(session, "cookies", None)
    if cookies is None:
        return False
    try:
        return len(cookies) > 0
    except TypeError:
        return bool(cookies)


def _clear_session_cookies(session: Any) -> None:
    cookies = getattr(session, "cookies", None)
    clear = getattr(cookies, "clear", None)
    if callable(clear):
        clear()


def _has_authentication_state(session: Any, kwargs: Mapping[str, Any]) -> bool:
    explicit_headers = kwargs.get("headers")
    session_headers = getattr(session, "headers", None)
    for headers in (explicit_headers, session_headers):
        if isinstance(headers, Mapping) and any(
            str(key).casefold() in SENSITIVE_REDIRECT_HEADERS - {"host"}
            for key in headers
        ):
            return True
    if kwargs.get("auth") or kwargs.get("cookies") or getattr(session, "auth", None):
        return True
    return False


class Transport:
    def __init__(
        self,
        *,
        session: requests.Session | Any | None = None,
        policy: RequestPolicy | None = None,
        budget: RequestBudget | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False
        self.policy = policy or RequestPolicy()
        self.budget = budget or RequestBudget()
        self.sleep = sleep
        self._initial_session_had_cookies = _has_session_cookies(self.session)

    def request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        read_only: bool,
        **kwargs: Any,
    ) -> requests.Response:
        normalized_method = method.upper()
        if not read_only or normalized_method not in {"GET", "HEAD", "OPTIONS", "POST"}:
            raise TransportError(
                CapabilityError(
                    code=ErrorCode.INVALID_INPUT,
                    message="transport permits only explicitly read-only public operations",
                    operation=operation,
                )
            )
        if self._initial_session_had_cookies or _has_authentication_state(
            self.session, kwargs
        ):
            raise TransportError(
                CapabilityError(
                    code=ErrorCode.INVALID_INPUT,
                    message="authenticated headers, cookies, and sessions are outside the public read-only boundary",
                    operation=operation,
                )
            )

        kwargs.setdefault(
            "timeout",
            (
                self.policy.connect_timeout_seconds,
                self.policy.read_timeout_seconds,
            ),
        )
        retry_sleep_used = 0.0
        last_error: CapabilityError | None = None
        last_response: requests.Response | None = None

        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                request_method = normalized_method
                request_url = url
                redirect_kwargs = dict(kwargs)
                redirects = 0
                while True:
                    self.budget.consume()
                    attempt_kwargs = dict(redirect_kwargs)
                    attempt_kwargs["allow_redirects"] = False
                    attempt_kwargs["timeout"] = bounded_timeout(
                        attempt_kwargs["timeout"], self.budget.elapsed_remaining_seconds
                    )
                    _clear_session_cookies(self.session)
                    try:
                        with self.budget.deadline():
                            response = self.session.request(
                                request_method, request_url, **attempt_kwargs
                            )
                    finally:
                        # Upstream-set cookies are intentionally not carried to
                        # redirects or later capability calls.
                        _clear_session_cookies(self.session)
                    location = response.headers.get("Location") if response.headers else None
                    if not (response.status_code in REDIRECT_STATUSES and location):
                        break
                    redirects += 1
                    if redirects > 5:
                        raise TransportError(
                            CapabilityError(
                                code=ErrorCode.UPSTREAM_REJECTED,
                                message="upstream redirect limit was exceeded",
                                operation=operation,
                                http_status=response.status_code,
                            ),
                            response,
                        )
                    try:
                        target_url = urljoin(request_url, location)
                    except ValueError as exc:
                        raise TransportError(
                            CapabilityError(
                                code=ErrorCode.UPSTREAM_REJECTED,
                                message="upstream returned a malformed redirect target",
                                operation=operation,
                                http_status=response.status_code,
                            ),
                            response,
                        ) from exc
                    if not redirect_allowed(request_url, target_url):
                        try:
                            target_host = urlparse(target_url).hostname
                        except ValueError:
                            target_host = None
                        raise TransportError(
                            CapabilityError(
                                code=ErrorCode.BLOCKED,
                                message="upstream redirected outside the allowed Naver origin",
                                operation=operation,
                                http_status=response.status_code,
                                detail={"target_host": target_host},
                            ),
                            response,
                        )
                    source_host = (urlparse(request_url).hostname or "").casefold()
                    target_host = (urlparse(target_url).hostname or "").casefold()
                    redirect_headers = dict(redirect_kwargs.get("headers") or {})
                    if source_host != target_host:
                        redirect_headers = _drop_headers(
                            redirect_headers, SENSITIVE_REDIRECT_HEADERS
                        )
                    if response.status_code == 303 or (
                        response.status_code in {301, 302} and request_method == "POST"
                    ):
                        request_method = "GET"
                        for body_key in ("data", "files", "json"):
                            redirect_kwargs.pop(body_key, None)
                        redirect_headers = _drop_headers(
                            redirect_headers, ENTITY_HEADERS
                        )
                    if redirect_headers:
                        redirect_kwargs["headers"] = redirect_headers
                    else:
                        redirect_kwargs.pop("headers", None)
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                    request_url = target_url
                last_response = response
                error = classify_status(response.status_code, operation=operation)
                if error is None:
                    error = classify_access_wall(response, operation=operation)
            except BudgetExceeded as exc:
                raise TransportError(exc.to_error(operation)) from exc
            except requests.RequestException as exc:
                response = None
                try:
                    self.budget.check()
                except BudgetExceeded as deadline:
                    raise TransportError(deadline.to_error(operation)) from exc
                error = classify_exception(exc, operation=operation)

            if error is None:
                return response
            last_error = error
            if not error.retryable or attempt >= self.policy.max_attempts:
                break
            if self.budget.requests_remaining < 1:
                exhausted = BudgetExceeded(
                    ErrorCode.REQUEST_BUDGET_EXHAUSTED,
                    f"request budget of {self.budget.max_requests} was exhausted",
                )
                raise TransportError(exhausted.to_error(operation), response)

            retry_after = 0.0
            if response is not None:
                retry_after = retry_after_seconds(response.headers.get("Retry-After"))
            delay = min(
                self.policy.retry_after_cap_seconds,
                retry_after if retry_after > 0 else float(2 ** (attempt - 1)),
            )
            if retry_sleep_used + delay > self.policy.retry_time_budget_seconds:
                break
            self.budget.check()
            if self.budget.elapsed_seconds + delay >= self.budget.max_elapsed_seconds:
                raise TransportError(
                    CapabilityError(
                        code=ErrorCode.TIME_BUDGET_EXHAUSTED,
                        message="retry would exceed the invocation time budget",
                        operation=operation,
                    ),
                    response,
                )
            if delay:
                self.sleep(delay)
                retry_sleep_used += delay

        assert last_error is not None
        raise TransportError(last_error, last_response)


__all__ = [
    "Transport",
    "TransportError",
    "bounded_timeout",
    "classify_access_wall",
    "classify_exception",
    "classify_status",
    "retry_after_seconds",
]
