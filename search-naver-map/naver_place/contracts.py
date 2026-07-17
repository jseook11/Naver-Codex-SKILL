"""Small, stable contracts shared by independent Naver capabilities."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import math
import signal
import threading
import time
from typing import Any, Callable, Iterator, Mapping


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class Status(StringEnum):
    OK = "ok"
    EMPTY = "empty"
    PARTIAL = "partial"
    ERROR = "error"


class ErrorCode(StringEnum):
    INVALID_INPUT = "invalid_input"
    DEPENDENCY_MISSING = "dependency_missing"
    NETWORK_ERROR = "network_error"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    UPSTREAM_REJECTED = "upstream_rejected"
    UPSTREAM_CHANGED = "upstream_changed"
    SECONDARY_NOT_FOUND = "secondary_not_found"
    NOT_FOUND = "not_found"
    REQUEST_BUDGET_EXHAUSTED = "request_budget_exhausted"
    TIME_BUDGET_EXHAUSTED = "time_budget_exhausted"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class CapabilityError:
    code: ErrorCode | str
    message: str
    operation: str = ""
    http_status: int | None = None
    retryable: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Provenance:
    source: str
    operation: str
    fetched_at: str
    live: bool
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Completeness:
    complete: bool
    stop_reason: str | None = None
    pages_fetched: int | None = None
    requested_count: int | None = None
    returned_count: int | None = None


@dataclass(frozen=True)
class RequestPolicy:
    connect_timeout_seconds: float = 15.0
    read_timeout_seconds: float = 30.0
    max_attempts: int = 2
    retry_after_cap_seconds: float = 30.0
    retry_time_budget_seconds: float = 60.0

    def __post_init__(self) -> None:
        values = (
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.retry_after_cap_seconds,
            self.retry_time_budget_seconds,
        )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise ValueError("transport policy values must be numbers")
        if not all(
            math.isfinite(value)
            for value in values
        ):
            raise ValueError("transport policy values must be finite")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("transport timeouts must be greater than zero")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 3
        ):
            raise ValueError("max_attempts must be between 1 and 3")
        if self.retry_after_cap_seconds < 0 or self.retry_time_budget_seconds < 0:
            raise ValueError("retry bounds cannot be negative")


class BudgetExceeded(RuntimeError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_error(self, operation: str) -> CapabilityError:
        return CapabilityError(code=self.code, message=str(self), operation=operation)


@dataclass
class RequestBudget:
    max_requests: int = 40
    max_elapsed_seconds: float = 120.0
    requests_used: int = 0
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    _started_at: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_requests, bool)
            or not isinstance(self.max_requests, int)
            or not 1 <= self.max_requests <= 100
        ):
            raise ValueError("max_requests must be an integer from 1 to 100")
        if (
            isinstance(self.max_elapsed_seconds, bool)
            or not isinstance(self.max_elapsed_seconds, (int, float))
            or not math.isfinite(self.max_elapsed_seconds)
            or self.max_elapsed_seconds <= 0
        ):
            raise ValueError("max_elapsed_seconds must be greater than zero")
        if (
            isinstance(self.requests_used, bool)
            or not isinstance(self.requests_used, int)
            or self.requests_used < 0
            or self.requests_used > self.max_requests
        ):
            raise ValueError("requests_used is outside the request budget")
        self._started_at = self._clock()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self._started_at)

    @property
    def requests_remaining(self) -> int:
        return max(0, self.max_requests - self.requests_used)

    @property
    def elapsed_remaining_seconds(self) -> float:
        return max(0.0, self.max_elapsed_seconds - self.elapsed_seconds)

    def check(self) -> None:
        if self.elapsed_seconds >= self.max_elapsed_seconds:
            raise BudgetExceeded(
                ErrorCode.TIME_BUDGET_EXHAUSTED,
                f"elapsed-time budget of {self.max_elapsed_seconds:g}s was exhausted",
            )

    def consume(self, count: int = 1) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("request budget consumption must be a positive integer")
        self.check()
        if self.requests_used + count > self.max_requests:
            raise BudgetExceeded(
                ErrorCode.REQUEST_BUDGET_EXHAUSTED,
                f"request budget of {self.max_requests} was exhausted",
            )
        self.requests_used += count

    @contextmanager
    def deadline(self) -> Iterator[None]:
        """Interrupt blocking I/O at the invocation deadline when Unix permits.

        macOS/Linux main-thread calls use ``ITIMER_REAL``. Other environments
        retain the portable post-operation check, so typed semantics stay the
        same even when hard interruption is unavailable.
        """

        self.check()
        supports_alarm = (
            hasattr(signal, "setitimer")
            and threading.current_thread() is threading.main_thread()
        )
        if not supports_alarm:
            try:
                yield
            finally:
                self.check()
            return

        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        if previous_timer[0] > 0:
            # Do not steal another library's active process alarm.
            try:
                yield
            finally:
                self.check()
            return

        previous_handler = signal.getsignal(signal.SIGALRM)

        def expire(_signum: int, _frame: Any) -> None:
            raise BudgetExceeded(
                ErrorCode.TIME_BUDGET_EXHAUSTED,
                f"elapsed-time budget of {self.max_elapsed_seconds:g}s was exhausted",
            )

        signal.signal(signal.SIGALRM, expire)
        signal.setitimer(
            signal.ITIMER_REAL,
            max(0.001, self.elapsed_remaining_seconds),
        )
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
        self.check()

    def snapshot(self) -> dict[str, int | float]:
        return {
            "requests_used": self.requests_used,
            "request_limit": self.max_requests,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "elapsed_limit_seconds": self.max_elapsed_seconds,
        }


@dataclass(frozen=True)
class CapabilityResult:
    capability: str
    request: Mapping[str, Any]
    data: Any
    status: Status = Status.OK
    errors: tuple[CapabilityError, ...] = ()
    warnings: tuple[str, ...] = ()
    provenance: tuple[Provenance, ...] = ()
    completeness: Completeness = field(
        default_factory=lambda: Completeness(complete=True)
    )
    budget: RequestBudget | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", Status(self.status))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "provenance", tuple(self.provenance))
        if not self.capability:
            raise ValueError("capability name cannot be empty")
        if self.status is Status.ERROR and not self.errors:
            raise ValueError("error results must include at least one typed error")
        if self.status is Status.PARTIAL and not self.errors and not self.warnings:
            raise ValueError("partial results must explain the missing portion")

    @property
    def exit_code(self) -> int:
        if self.status is not Status.ERROR:
            return 0
        code = str(self.errors[0].code) if self.errors else ErrorCode.INTERNAL_ERROR.value
        return {
            ErrorCode.INVALID_INPUT.value: 2,
            ErrorCode.DEPENDENCY_MISSING.value: 3,
            ErrorCode.NETWORK_ERROR.value: 10,
            ErrorCode.RATE_LIMITED.value: 10,
            ErrorCode.BLOCKED.value: 10,
            ErrorCode.UPSTREAM_REJECTED.value: 10,
            ErrorCode.REQUEST_BUDGET_EXHAUSTED.value: 10,
            ErrorCode.TIME_BUDGET_EXHAUSTED.value: 10,
            ErrorCode.UPSTREAM_CHANGED.value: 11,
            ErrorCode.NOT_FOUND.value: 12,
            ErrorCode.SECONDARY_NOT_FOUND.value: 12,
            ErrorCode.INTERNAL_ERROR.value: 1,
        }.get(code, 1)
