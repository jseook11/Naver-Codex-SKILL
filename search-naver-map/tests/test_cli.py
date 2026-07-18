from __future__ import annotations

import json
from pathlib import Path

import pytest

from naver_place.cli import main


FIXTURES = Path(__file__).parent / "fixtures"


def run_json(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict]:
    exit_code = main(args)
    output = capsys.readouterr()
    assert output.err == ""
    return exit_code, json.loads(output.out)


def test_capability_catalog_is_generated_from_all_public_subcommands(capsys) -> None:
    exit_code, payload = run_json(capsys, "capabilities", "--json")
    assert exit_code == 0
    assert [item["command"] for item in payload["capabilities"]] == [
        "search",
        "detail",
        "reviews",
        "booking",
    ]
    assert {item["capability"] for item in payload["capabilities"]} == {
        "map.search",
        "place.detail",
        "place.reviews",
        "booking.availability",
    }
    assert "view" in {argument["name"] for argument in payload["common_arguments"]}
    reviews = next(item for item in payload["capabilities"] if item["command"] == "reviews")
    review_arguments = {argument["name"]: argument for argument in reviews["arguments"]}
    assert review_arguments["limit"]["default"] == 10
    assert {"latest_html", "recommended_html"} <= set(review_arguments)
    assert {"page_size", "request_delay", "raw_dir"}.isdisjoint(review_arguments)
    booking = next(item for item in payload["capabilities"] if item["command"] == "booking")
    assert booking["required_one_of"] == [["query", "booking_url", "business_id"]]


def test_search_fixture_emits_versioned_general_discovery(capsys) -> None:
    exit_code, payload = run_json(
        capsys,
        "search",
        "--query",
        "샘플 장소",
        "--html",
        str(FIXTURES / "map/search-success.html"),
    )
    assert exit_code == 0
    assert payload["schema_version"] == "1"
    assert payload["status"] == "ok"
    assert payload["capability"] == "map.search"
    assert payload["data"]["returned_count"] == 2
    assert "target" not in payload["data"]
    assert payload["provenance"][0]["live"] is False
    assert payload["data"]["url"] == "fixture://search-success.html"
    assert str(FIXTURES.parent) not in json.dumps(payload, ensure_ascii=False)


def test_detail_fixture_can_report_enabled_missing_sources_as_partial(capsys) -> None:
    exit_code, payload = run_json(
        capsys,
        "detail",
        "--place",
        "1001",
        "--html",
        str(FIXTURES / "detail/home.html"),
        "--offline",
    )
    assert exit_code == 0
    assert payload["status"] == "partial"
    assert payload["data"]["base"]["name"] == "샘플 제주 게스트하우스"
    assert {error["code"] for error in payload["errors"]} == {"secondary_not_found"}


def test_review_standard_view_replays_labeled_html_snapshots_without_private_fields(
    capsys,
) -> None:
    exit_code, payload = run_json(
        capsys,
        "reviews",
        "--place",
        "1234567890",
        "--latest-html",
        str(FIXTURES / "reviews/latest.html"),
        "--recommended-html",
        str(FIXTURES / "reviews/recommended.html"),
        "--limit",
        "3",
    )
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["budget"]["requests_used"] == 0
    assert set(payload["data"]["samples"]) == {
        "latest",
        "recommended",
        "recommended_keyword_only",
    }
    for review in payload["data"]["reviews"]:
        assert "reviewer_id" not in review
        assert "receipt_info_url" not in review


def test_booking_standard_view_is_small_and_conservative(capsys) -> None:
    exit_code, payload = run_json(
        capsys,
        "booking",
        "--business-id",
        "5001",
        "--business-type-id",
        "3",
        "--check-in",
        "2026-07-20",
        "--check-out",
        "2026-07-22",
        "--raw-dir",
        str(FIXTURES / "booking"),
    )
    assert exit_code == 0
    assert payload["status"] == "ok"
    items = payload["data"]["places"][0]["booking"]["items"]
    assert [item["is_available"] for item in items] == [True, None]
    assert all("description" not in item and "images" not in item for item in items)


def test_invalid_cli_arguments_are_a_typed_json_error(capsys) -> None:
    exit_code, payload = run_json(capsys, "search")
    assert exit_code == 2
    assert payload["status"] == "error"
    assert payload["errors"][0]["code"] == "invalid_input"

    exit_code, payload = run_json(
        capsys,
        "reviews",
        "--place",
        "1234567890",
        "--read-timeout",
        "0",
    )
    assert exit_code == 2
    assert payload["errors"][0]["code"] == "invalid_input"


def test_output_flag_writes_the_envelope(tmp_path: Path, capsys) -> None:
    output_path = tmp_path / "result.json"
    exit_code = main(
        (
            "search",
            "--query",
            "샘플 장소",
            "--html",
            str(FIXTURES / "map/search-empty.html"),
            "--output",
            str(output_path),
        )
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == captured.err == ""
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "empty"


def test_nonfinite_budget_is_typed_json_without_nan(capsys) -> None:
    exit_code, payload = run_json(
        capsys,
        "search",
        "--query",
        "샘플",
        "--html",
        str(FIXTURES / "map/search-empty.html"),
        "--time-budget",
        "nan",
    )

    assert exit_code == 2
    assert payload["errors"][0]["code"] == "invalid_input"
    assert "NaN" not in json.dumps(payload)


def test_unwritable_output_path_falls_back_to_typed_stdout(
    tmp_path: Path, capsys
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")

    exit_code, payload = run_json(
        capsys,
        "search",
        "--query",
        "샘플",
        "--html",
        str(FIXTURES / "map/search-empty.html"),
        "--output",
        str(blocker / "result.json"),
    )

    assert exit_code == 2
    assert payload["errors"][0]["code"] == "invalid_input"
    assert payload["errors"][0]["message"] == "could not write the requested output file"


def test_standard_errors_do_not_expose_absolute_fixture_paths(
    tmp_path: Path, capsys
) -> None:
    secret_root = tmp_path / "PrivateProject"
    cases = (
        (
            "search",
            "--query",
            "샘플",
            "--html",
            str(secret_root / "missing.html"),
        ),
        (
            "reviews",
            "--place",
            "1234567890",
            "--latest-html",
            str(secret_root / "latest.html"),
            "--recommended-html",
            str(secret_root / "recommended.html"),
        ),
        (
            "booking",
            "--business-id",
            "5001",
            "--business-type-id",
            "3",
            "--check-in",
            "2026-07-20",
            "--check-out",
            "2026-07-22",
            "--raw-dir",
            str(secret_root / "booking"),
        ),
    )

    for args in cases:
        _, payload = run_json(capsys, *args)
        rendered = json.dumps(payload, ensure_ascii=False)
        assert str(tmp_path) not in rendered
        assert "/Users/" not in rendered
