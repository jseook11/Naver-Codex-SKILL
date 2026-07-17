from __future__ import annotations

from dataclasses import dataclass, field

import pytest
import requests

from naver_place.contracts import ErrorCode, RequestBudget, RequestPolicy
from naver_place.transport import (
    Transport,
    TransportError,
    classify_access_wall,
    classify_status,
    redirect_allowed,
)


@dataclass
class FakeResponse:
    status_code: int
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    url: str = "https://example.test"
    content: bytes = b""


class FakeSession:
    def __init__(self, outcomes: list[FakeResponse | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (403, ErrorCode.BLOCKED, False),
        (404, ErrorCode.NOT_FOUND, False),
        (405, ErrorCode.UPSTREAM_REJECTED, False),
        (429, ErrorCode.RATE_LIMITED, True),
        (503, ErrorCode.NETWORK_ERROR, True),
    ],
)
def test_http_status_classification(status: int, code: ErrorCode, retryable: bool) -> None:
    error = classify_status(status, operation="test")
    assert error is not None
    assert error.code is code
    assert error.retryable is retryable


def test_transport_retries_bounded_transient_response() -> None:
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "99"}), FakeResponse(200)])
    sleeps: list[float] = []
    budget = RequestBudget(max_requests=2, max_elapsed_seconds=120)
    transport = Transport(
        session=session,
        policy=RequestPolicy(max_attempts=2, retry_after_cap_seconds=3),
        budget=budget,
        sleep=sleeps.append,
    )
    response = transport.request(
        "GET", "https://example.test", operation="test", read_only=True
    )
    assert response.status_code == 200
    assert budget.requests_used == 2
    assert sleeps == [3]


def test_transport_exposes_initial_405_without_retry() -> None:
    session = FakeSession([FakeResponse(405)])
    transport = Transport(session=session, sleep=lambda _: None)
    with pytest.raises(TransportError) as caught:
        transport.request("POST", "https://example.test", operation="test", read_only=True)
    assert caught.value.error.code is ErrorCode.UPSTREAM_REJECTED
    assert caught.value.error.http_status == 405
    assert len(session.calls) == 1


def test_transport_maps_timeout_and_stops_at_attempt_bound() -> None:
    session = FakeSession([requests.Timeout("slow"), requests.Timeout("still slow")])
    transport = Transport(
        session=session,
        policy=RequestPolicy(max_attempts=2),
        sleep=lambda _: None,
    )
    with pytest.raises(TransportError) as caught:
        transport.request("GET", "https://example.test", operation="test", read_only=True)
    assert caught.value.error.code is ErrorCode.NETWORK_ERROR
    assert caught.value.error.retryable is True
    assert len(session.calls) == 2


def test_transport_refuses_write_methods() -> None:
    transport = Transport(session=FakeSession([]))
    with pytest.raises(TransportError) as caught:
        transport.request("DELETE", "https://example.test", operation="test", read_only=True)
    assert caught.value.error.code is ErrorCode.INVALID_INPUT


def test_http_200_captcha_page_is_blocked_without_retry() -> None:
    response = FakeResponse(
        200,
        content="자동입력 방지문자를 입력하세요".encode("utf-8"),
    )
    error = classify_access_wall(response, operation="test")
    assert error is not None
    assert error.code is ErrorCode.BLOCKED
    assert error.detail["reason"] == "captcha"

    session = FakeSession([response])
    transport = Transport(session=session, sleep=lambda _: None)
    with pytest.raises(TransportError) as caught:
        transport.request("GET", response.url, operation="test", read_only=True)
    assert caught.value.error.code is ErrorCode.BLOCKED
    assert len(session.calls) == 1


def test_embedded_captcha_loader_script_is_not_an_access_wall() -> None:
    response = FakeResponse(
        200,
        content=b"<script>function initNcaptcha(){window.ncaptcha={}}</script>",
    )
    assert classify_access_wall(response, operation="test") is None


def test_response_finishing_after_elapsed_deadline_is_not_reported_as_success() -> None:
    now = [0.0]

    class SlowSession(FakeSession):
        def request(self, method: str, url: str, **kwargs):
            self.calls.append((method, url, kwargs))
            now[0] = 2.0
            return FakeResponse(200)

    session = SlowSession([])
    budget = RequestBudget(max_requests=1, max_elapsed_seconds=1, _clock=lambda: now[0])
    transport = Transport(
        session=session,
        policy=RequestPolicy(max_attempts=1),
        budget=budget,
    )
    with pytest.raises(TransportError) as caught:
        transport.request("GET", "https://example.test", operation="test", read_only=True)
    assert caught.value.error.code is ErrorCode.TIME_BUDGET_EXHAUSTED
    connect_timeout, read_timeout = session.calls[0][2]["timeout"]
    assert connect_timeout + read_timeout <= 1.001


def test_redirect_hops_are_manual_and_each_consumes_budget() -> None:
    session = FakeSession(
        [
            FakeResponse(302, headers={"Location": "/final"}),
            FakeResponse(200, url="https://example.test/final"),
        ]
    )
    budget = RequestBudget(max_requests=2)
    response = Transport(session=session, budget=budget).request(
        "GET", "https://example.test/start", operation="test", read_only=True
    )

    assert response.status_code == 200
    assert [call[1] for call in session.calls] == [
        "https://example.test/start",
        "https://example.test/final",
    ]
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert budget.requests_used == 2


def test_redirect_cannot_escape_a_one_request_budget() -> None:
    session = FakeSession(
        [
            FakeResponse(302, headers={"Location": "/final"}),
            FakeResponse(200),
        ]
    )
    transport = Transport(session=session, budget=RequestBudget(max_requests=1))

    with pytest.raises(TransportError) as caught:
        transport.request(
            "GET", "https://example.test/start", operation="test", read_only=True
        )
    assert caught.value.error.code is ErrorCode.REQUEST_BUDGET_EXHAUSTED
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "target",
    (
        "http://m.place.naver.com/next",
        "ftp://m.place.naver.com/next",
        "https://m.place.naver.com:444/next",
        "https://user:secret@m.place.naver.com/next",
    ),
)
def test_redirect_policy_rejects_unsafe_targets(target: str) -> None:
    assert redirect_allowed("https://m.place.naver.com/start", target) is False


def test_cross_host_redirect_strips_explicit_host_header() -> None:
    session = FakeSession(
        [
            FakeResponse(
                302,
                headers={"Location": "https://api.place.naver.com/final"},
            ),
            FakeResponse(200, url="https://api.place.naver.com/final"),
        ]
    )
    Transport(session=session, budget=RequestBudget(max_requests=2)).request(
        "GET",
        "https://m.place.naver.com/start",
        operation="test",
        read_only=True,
        headers={"Host": "m.place.naver.com", "X-Test": "kept"},
    )

    second_headers = session.calls[1][2]["headers"]
    assert "Host" not in second_headers
    assert second_headers["X-Test"] == "kept"


@pytest.mark.parametrize("header", ("Authorization", "Proxy-Authorization", "Cookie"))
def test_authenticated_initial_request_is_rejected_before_network(header: str) -> None:
    session = FakeSession([])
    with pytest.raises(TransportError) as caught:
        Transport(session=session).request(
            "GET",
            "https://m.place.naver.com/start",
            operation="test",
            read_only=True,
            headers={header: "secret"},
        )
    assert caught.value.error.code is ErrorCode.INVALID_INPUT
    assert session.calls == []


def test_non_redirect_304_with_location_is_not_followed() -> None:
    session = FakeSession([FakeResponse(304, headers={"Location": "/wrong"})])
    with pytest.raises(TransportError) as caught:
        Transport(session=session).request(
            "GET", "https://example.test/start", operation="test", read_only=True
        )
    assert caught.value.error.code is ErrorCode.UPSTREAM_REJECTED
    assert caught.value.error.http_status == 304
    assert len(session.calls) == 1


def test_retry_does_not_sleep_when_request_budget_has_no_next_slot() -> None:
    sleeps: list[float] = []
    session = FakeSession([FakeResponse(429)])
    transport = Transport(
        session=session,
        budget=RequestBudget(max_requests=1),
        sleep=sleeps.append,
    )
    with pytest.raises(TransportError) as caught:
        transport.request(
            "GET", "https://example.test", operation="test", read_only=True
        )
    assert caught.value.error.code is ErrorCode.REQUEST_BUDGET_EXHAUSTED
    assert sleeps == []


def test_upstream_set_cookies_are_cleared_between_public_requests() -> None:
    class CookieSession(FakeSession):
        def __init__(self):
            super().__init__([FakeResponse(200), FakeResponse(200)])
            self.cookies = {}

        def request(self, method: str, url: str, **kwargs):
            assert self.cookies == {}
            response = super().request(method, url, **kwargs)
            self.cookies["anonymous"] = "must-not-persist"
            return response

    session = CookieSession()
    transport = Transport(session=session, budget=RequestBudget(max_requests=2))
    transport.request(
        "GET", "https://example.test/one", operation="test", read_only=True
    )
    transport.request(
        "GET", "https://example.test/two", operation="test", read_only=True
    )

    assert session.cookies == {}


def test_default_transport_disables_netrc_authentication(monkeypatch) -> None:
    monkeypatch.setattr(
        requests.sessions,
        "get_netrc_auth",
        lambda _url: ("user", "secret"),
    )
    transport = Transport()
    prepared = transport.session.prepare_request(
        requests.Request("GET", "https://m.place.naver.com/place/1/home")
    )

    assert transport.session.trust_env is False
    assert "Authorization" not in prepared.headers


def test_malformed_redirect_location_is_typed_without_second_hop() -> None:
    session = FakeSession(
        [FakeResponse(302, headers={"Location": "https://[bad"})]
    )
    with pytest.raises(TransportError) as caught:
        Transport(session=session).request(
            "GET",
            "https://m.place.naver.com/start",
            operation="test",
            read_only=True,
        )

    assert caught.value.error.code is ErrorCode.UPSTREAM_REJECTED
    assert len(session.calls) == 1
