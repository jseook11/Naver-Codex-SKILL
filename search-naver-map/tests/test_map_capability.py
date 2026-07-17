from __future__ import annotations

from pathlib import Path

from naver_place.capabilities.map import OPERATION, parse_search_html, search_places
from naver_place.contracts import CapabilityError, ErrorCode, RequestBudget
from naver_place.transport import TransportError


FIXTURES = Path(__file__).parent / "fixtures" / "map"
FETCHED_AT = "2026-01-01T00:00:00+00:00"


def status_value(result) -> str:
    return getattr(result.status, "value", str(result.status))


def code_value(error) -> str:
    return getattr(error.code, "value", str(error.code))


def test_parse_search_html_deduplicates_by_place_id_and_preserves_rank() -> None:
    parsed = parse_search_html((FIXTURES / "search-success.html").read_text(encoding="utf-8"))

    assert [place.place_id for place in parsed["places"]] == ["1001", "1002"]
    assert [place.rank for place in parsed["places"]] == [1, 2]
    assert parsed["places"][0].reservation_url.endswith("fixture-1")
    assert parsed["places"][1].latitude == 37.5002


def test_search_is_general_discovery_and_does_not_require_a_target() -> None:
    result = search_places(
        "샘플 장소",
        html=(FIXTURES / "search-success.html").read_text(encoding="utf-8"),
        fetched_at=FETCHED_AT,
    )

    assert status_value(result) == "ok"
    assert result.request["target_place_id"] is None
    assert "target" not in result.data
    assert result.data["upstream_returned_count"] == 2
    assert result.data["returned_count"] == 2
    assert result.completeness.complete is True
    assert result.completeness.stop_reason == "exhausted"
    assert result.provenance[0].live is False
    assert result.provenance[0].detail["replays"] == OPERATION


def test_local_filters_keep_upstream_rank_and_target_lookup_is_optional() -> None:
    result = search_places(
        "샘플 장소",
        html=(FIXTURES / "search-success.html").read_text(encoding="utf-8"),
        include_text=["분식"],
        exclude_text=["제주"],
        target_place_id="1001",
        fetched_at=FETCHED_AT,
    )

    assert status_value(result) == "ok"
    assert [place["place_id"] for place in result.data["places"]] == ["1002"]
    assert result.data["places"][0]["rank"] == 2
    assert result.data["target"]["found"] is True
    assert result.data["target"]["rank"] == 1
    assert result.data["post_filter_count"] == 1


def test_valid_zero_result_payload_is_empty_not_schema_drift() -> None:
    result = search_places(
        "존재하지 않는 샘플",
        html=(FIXTURES / "search-empty.html").read_text(encoding="utf-8"),
        fetched_at=FETCHED_AT,
    )

    assert status_value(result) == "empty"
    assert result.errors == ()
    assert result.data["places"] == []
    assert result.completeness.complete is True
    assert result.completeness.stop_reason == "exhausted"


def test_missing_embedded_payload_is_typed_as_upstream_changed() -> None:
    result = search_places("샘플", html="<html><body>changed</body></html>")

    assert status_value(result) == "error"
    assert code_value(result.errors[0]) == "upstream_changed"
    assert result.errors[0].operation == OPERATION
    assert result.completeness.complete is False


def test_nonempty_unrecognized_place_rows_are_schema_drift() -> None:
    html = """<script>window.__RQ_STREAMING_STATE__.push({"queries":[{"state":{"data":{"items":[{"newId":"1","newTitle":"changed"}],"totalCount":1}}}]})</script>"""
    result = search_places("샘플", html=html)

    assert status_value(result) == "error"
    assert code_value(result.errors[0]) == "upstream_changed"


def test_live_source_url_override_is_rejected_before_transport() -> None:
    class FakeTransport:
        budget = None

        def __init__(self):
            self.calls = []

        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("must not fetch caller-selected URL")

    transport = FakeTransport()
    result = search_places(
        "샘플",
        source_url="http://127.0.0.1:8080/private",
        transport=transport,
    )

    assert status_value(result) == "error"
    assert code_value(result.errors[0]) == "invalid_input"
    assert transport.calls == []


def test_reported_total_beyond_source_page_is_partial_not_exhausted() -> None:
    html = """<script>window.__RQ_STREAMING_STATE__.push({"queries":[{"state":{"data":{"items":[{"id":"1","name":"one"}],"totalCount":100,"pageInfo":{"page":1,"size":1}}}}]})</script>"""
    result = search_places("샘플", html=html, limit=20)

    assert status_value(result) == "partial"
    assert result.data["returned_count"] == 1
    assert result.completeness.complete is False
    assert result.completeness.stop_reason == "source_page_limit"
    assert result.warnings


def test_fixture_without_capture_metadata_does_not_claim_current_fetch_time() -> None:
    result = search_places(
        "샘플",
        html=(FIXTURES / "search-empty.html").read_text(encoding="utf-8"),
    )

    assert result.provenance[0].fetched_at == "unknown"
    assert "replayed_at" in result.provenance[0].detail


def test_search_validates_query_limit_and_optional_target_id() -> None:
    assert code_value(search_places("", html="").errors[0]) == "invalid_input"
    assert code_value(search_places("샘플", limit=0, html="").errors[0]) == "invalid_input"
    assert (
        code_value(search_places("샘플", target_place_id="not-an-id", html="").errors[0])
        == "invalid_input"
    )


def test_injected_transport_receives_one_read_only_search_request() -> None:
    class Response:
        text = (FIXTURES / "search-success.html").read_text(encoding="utf-8")

    class FakeTransport:
        budget = None

        def __init__(self) -> None:
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response()

    transport = FakeTransport()
    result = search_places("샘플", transport=transport, fetched_at=FETCHED_AT)

    assert status_value(result) == "ok"
    assert len(transport.calls) == 1
    method, _, kwargs = transport.calls[0]
    assert method == "GET"
    assert kwargs["operation"] == OPERATION
    assert kwargs["read_only"] is True
    assert result.provenance[0].live is True


def test_search_endpoint_404_is_not_direct_place_not_found() -> None:
    class MissingSearchTransport:
        budget = None

        def request(self, *_args, **_kwargs):
            raise TransportError(
                CapabilityError(
                    code=ErrorCode.NOT_FOUND,
                    message="HTTP 404",
                    operation=OPERATION,
                    http_status=404,
                )
            )

    result = search_places("샘플", transport=MissingSearchTransport())
    assert status_value(result) == "error"
    assert code_value(result.errors[0]) == "upstream_rejected"
    assert result.exit_code == 10


def test_offline_search_respects_elapsed_budget() -> None:
    now = [0.0]
    budget = RequestBudget(max_requests=1, max_elapsed_seconds=1, _clock=lambda: now[0])
    now[0] = 2.0
    result = search_places(
        "샘플",
        html=(FIXTURES / "search-success.html").read_text(encoding="utf-8"),
        budget=budget,
    )
    assert status_value(result) == "error"
    assert code_value(result.errors[0]) == "time_budget_exhausted"
