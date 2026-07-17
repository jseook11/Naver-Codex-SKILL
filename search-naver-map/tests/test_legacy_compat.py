from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from naver_place._legacy import scrape_naver_booking as booking_impl
from naver_place._legacy import scrape_naver_map as map_impl
from naver_place._legacy import scrape_naver_place_detail as detail_impl
from naver_place._legacy import scrape_naver_place_reviews as reviews_impl
from scripts import scrape_naver_booking as booking_adapter
from scripts import scrape_naver_map as map_adapter
from scripts import scrape_naver_place_detail as detail_adapter
from scripts import scrape_naver_place_reviews as reviews_adapter


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


def test_historical_modules_reexport_one_canonical_implementation() -> None:
    assert map_adapter.parse_map_html is map_impl.parse_map_html
    assert detail_adapter.parse_place_detail_html is detail_impl.parse_place_detail_html
    assert reviews_adapter.normalize_reviews is reviews_impl.normalize_reviews
    assert booking_adapter.normalize_accommodation_item is booking_impl.normalize_accommodation_item


def test_booking_client_supports_historical_scripts_only_import(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import naver_booking_client"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def run_legacy(*args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def test_historical_map_cli_keeps_old_json_shape() -> None:
    payload = run_legacy(
        "scripts/scrape_naver_map.py",
        "샘플 장소",
        "--target-id",
        "1001",
        "--html",
        str(FIXTURES / "map/search-success.html"),
        "--json",
    )
    assert payload["source"] == "naver_map_mobile"
    assert payload["target"]["found"] is True
    assert payload["places"][0]["id"] == "1001"


def test_historical_detail_and_booking_paths_still_execute_offline() -> None:
    detail = run_legacy(
        "scripts/scrape_naver_place_detail.py",
        "1001",
        "--html",
        str(FIXTURES / "detail/home.html"),
        "--feed-html",
        str(FIXTURES / "detail/feed.html"),
        "--hours-json",
        str(FIXTURES / "detail/hours.json"),
        "--offline",
    )
    assert detail["base"]["name"] == "샘플 제주 게스트하우스"

    booking = run_legacy(
        "scripts/scrape_naver_booking.py",
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
    assert booking["source"] == "naver_booking_exploration"
    assert booking["places"][0]["booking"]["items"]
