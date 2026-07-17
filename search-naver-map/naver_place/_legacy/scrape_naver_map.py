#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html as html_lib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from naver_place.contracts import RequestPolicy
from naver_place.transport import Transport, TransportError


DEFAULT_REVIEW_CSV_PATH = "naver_map_review_results.csv"

MOBILE_MAP_SEARCH_URL = "https://m.map.naver.com/search2/search.naver"
MOBILE_PLACE_DETAIL_URL = "https://m.place.naver.com/place/{place_id}/home"
DEFAULT_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)
MOBILE_MAP_HEADERS = {
    "User-Agent": DEFAULT_MOBILE_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
    "Referer": "https://m.map.naver.com/",
}
REVIEW_CSV_HEADER = [
    "date",
    "query",
    "rank",
    "place_id",
    "place_name",
    "visitor_review_count",
    "blog_review_count",
    "place_url",
]


@dataclass
class MapPlace:
    rank: int
    id: str
    name: str
    category: str = ""
    address: str = ""
    road_address: str = ""
    tel: str = ""
    virtual_tel: str = ""
    latitude: float | None = None
    longitude: float | None = None
    place_url: str = ""
    reservation_url: str = ""
    has_menu_info: bool = False
    has_npay: bool = False


@dataclass
class MapSearchResult:
    query: str
    url: str
    fetched_at: str
    total_count: int | None
    returned_count: int
    search_type: str
    location_query_info: str
    page_info: dict[str, Any]
    places: list[MapPlace]


@dataclass(frozen=True)
class ReviewCounts:
    visitor_review_count: int | None
    blog_review_count: int | None


def build_mobile_map_headers(user_agent: str | None = None) -> dict[str, str]:
    headers = dict(MOBILE_MAP_HEADERS)
    override = (user_agent or os.environ.get("NAVER_MAP_USER_AGENT") or "").strip()
    if override:
        headers["User-Agent"] = override
    return headers


def build_map_url(query: str, sort: str) -> str:
    params = {
        "query": query,
        "sm": "hty",
        "style": "v5",
    }
    if sort:
        params["siteSort"] = sort
    return f"{MOBILE_MAP_SEARCH_URL}?{urlencode(params)}"


def public_get(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int | float,
) -> requests.Response:
    """Stateless legacy GET routed through the shared safe redirect policy."""

    transport = Transport(
        policy=RequestPolicy(
            connect_timeout_seconds=min(15.0, float(timeout)),
            read_timeout_seconds=float(timeout),
            max_attempts=1,
        )
    )
    try:
        return transport.request(
            "GET", url, operation="legacy.public_get", read_only=True, headers=headers
        )
    except TransportError as exc:
        if exc.response is not None:
            raise requests.HTTPError(
                exc.error.message, response=exc.response
            ) from exc
        raise requests.RequestException(exc.error.message) from exc


def fetch_html(
    query: str,
    timeout: int,
    sort: str,
    headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    url = build_map_url(query, sort)
    response = public_get(
        url,
        headers=headers or build_mobile_map_headers(),
        timeout=timeout,
    )
    response.raise_for_status()
    return response_text(response), url


def fetch_place_detail_html(
    place_id: str,
    timeout: int,
    headers: dict[str, str] | None = None,
) -> str:
    url = MOBILE_PLACE_DETAIL_URL.format(place_id=place_id)
    response = public_get(
        url,
        headers=headers or build_mobile_map_headers(),
        timeout=timeout,
    )
    response.raise_for_status()
    return response_text(response)


def response_text(response: requests.Response) -> str:
    encoding = response.encoding or ""
    if not encoding or encoding.lower() in {"iso-8859-1", "latin-1"}:
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            encoding = response.apparent_encoding or "utf-8"
    return response.content.decode(encoding, errors="replace")


def parse_review_count(html: str, label_pattern: str) -> int | None:
    decoded_html = html_lib.unescape(html)
    match = re.search(rf"{label_pattern}\s*([0-9][0-9,]*)", decoded_html)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def parse_review_counts(html: str) -> ReviewCounts:
    return ReviewCounts(
        visitor_review_count=parse_review_count(html, r"방문\s*자리뷰"),
        blog_review_count=parse_review_count(html, r"블로그\s*리뷰"),
    )


def extract_balanced_json(text: str, start: int) -> tuple[str, int]:
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] not in "{[":
        return "", start

    depth = 0
    in_string = False
    escape = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1], index + 1

    return "", start


def extract_streaming_payloads(html: str) -> list[dict[str, Any]]:
    marker = "window.__RQ_STREAMING_STATE__.push("
    payloads: list[dict[str, Any]] = []
    cursor = 0

    while True:
        marker_index = html.find(marker, cursor)
        if marker_index == -1:
            break

        payload_text, cursor = extract_balanced_json(html, marker_index + len(marker))
        if not payload_text:
            cursor = marker_index + len(marker)
            continue

        try:
            payloads.append(json.loads(payload_text))
        except json.JSONDecodeError:
            continue

    return payloads


def strip_html_text(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"<[^>]+>", "", str(value))
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def find_place_data(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []

    for payload in payloads:
        for query in payload.get("queries", []):
            state = query.get("state", {})
            data = state.get("data", {})
            items = data.get("items")
            if not isinstance(items, list):
                continue
            if not any(isinstance(item, dict) and item.get("name") for item in items):
                continue
            candidates.append(data)

    if not candidates:
        raise ValueError("지도 검색 결과 JSON을 찾지 못했습니다.")

    return max(candidates, key=lambda data: len(data.get("items", [])))


def parse_bool(value: Any) -> bool:
    return bool(value) if value is not None else False


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_places(data: dict[str, Any]) -> list[MapPlace]:
    places: list[MapPlace] = []

    for rank, item in enumerate(data.get("items", []), start=1):
        if not isinstance(item, dict):
            continue

        place_id = str(item.get("id", "")).strip()
        name = strip_html_text(item.get("name"))
        if not place_id or not name:
            continue

        places.append(
            MapPlace(
                rank=rank,
                id=place_id,
                name=name,
                category=strip_html_text(item.get("category")),
                address=strip_html_text(item.get("address")),
                road_address=strip_html_text(item.get("roadAddress")),
                tel=strip_html_text(item.get("tel")),
                virtual_tel=strip_html_text(item.get("virtualTel")),
                latitude=to_float(item.get("latitude")),
                longitude=to_float(item.get("longitude")),
                place_url=f"https://m.place.naver.com/place/{place_id}/home",
                reservation_url=strip_html_text(item.get("reservationUrl")),
                has_menu_info=parse_bool(item.get("hasMenuInfo")),
                has_npay=parse_bool(item.get("hasNPay")),
            )
        )

    return places


def parse_map_html(html: str, query: str, url: str) -> MapSearchResult:
    payloads = extract_streaming_payloads(html)
    data = find_place_data(payloads)
    places = parse_places(data)

    return MapSearchResult(
        query=query,
        url=url,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        total_count=data.get("totalCount"),
        returned_count=len(places),
        search_type=strip_html_text(data.get("searchType")),
        location_query_info=strip_html_text(data.get("locationQueryInfo")),
        page_info=data.get("pageInfo") if isinstance(data.get("pageInfo"), dict) else {},
        places=places,
    )


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def find_target(
    places: list[MapPlace],
    target_id: str,
    target_name: str,
) -> MapPlace | None:
    target_id = target_id.strip()
    normalized_target_name = normalize_name(target_name)

    for place in places:
        if target_id and place.id == target_id:
            return place
        if normalized_target_name and normalized_target_name in normalize_name(place.name):
            return place

    return None


def local_date() -> str:
    return datetime.now().astimezone().date().isoformat()


def count_text(value: int | None) -> str:
    return str(value) if value is not None else ""


def review_csv_row(
    query: str,
    target: MapPlace,
    review_counts: ReviewCounts,
    date: str | None = None,
) -> list[str]:
    return [
        date or local_date(),
        query,
        str(target.rank),
        target.id,
        target.name,
        count_text(review_counts.visitor_review_count),
        count_text(review_counts.blog_review_count),
        target.place_url,
    ]


def append_review_csv(
    query: str,
    target: MapPlace,
    review_counts: ReviewCounts,
    path: str,
) -> None:
    output_path = Path(path)
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)

    write_header = not output_path.exists() or output_path.stat().st_size == 0
    with output_path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        if write_header:
            writer.writerow(REVIEW_CSV_HEADER)
        writer.writerow(review_csv_row(query, target, review_counts))


def review_counts_payload(review_counts: ReviewCounts) -> dict[str, int | None]:
    return {
        "visitor_review_count": review_counts.visitor_review_count,
        "blog_review_count": review_counts.blog_review_count,
    }


def result_payload(
    result: MapSearchResult,
    limit: int,
    target_id: str,
    target_name: str,
    review_counts: ReviewCounts | None = None,
) -> dict[str, Any]:
    places = result.places if limit <= 0 else result.places[:limit]
    target = find_target(result.places, target_id, target_name)

    target_payload: dict[str, Any] = {
        "id": target_id,
        "name": target_name,
        "found": target is not None,
        "rank": target.rank if target else None,
        "place": asdict(target) if target else None,
    }
    if review_counts is not None:
        target_payload["review_counts"] = review_counts_payload(review_counts)

    return {
        "query": result.query,
        "source": "naver_map_mobile",
        "url": result.url,
        "fetched_at": result.fetched_at,
        "total_count": result.total_count,
        "returned_count": result.returned_count,
        "shown_count": len(places),
        "search_type": result.search_type,
        "location_query_info": result.location_query_info,
        "page_info": result.page_info,
        "target": target_payload,
        "places": [asdict(place) for place in places],
    }


def print_text_result(
    result: MapSearchResult,
    limit: int,
    target_id: str,
    target_name: str,
    review_counts: ReviewCounts | None = None,
) -> None:
    places = result.places if limit <= 0 else result.places[:limit]
    target = find_target(result.places, target_id, target_name)

    print(f"Query: {result.query}")
    print("Source: naver_map_mobile")
    print(f"URL: {result.url}")
    if result.location_query_info:
        print(f"Location query: {result.location_query_info}")
    print(f"Total count: {result.total_count}")
    print(f"Returned items: {result.returned_count}")
    print(f"Showing: {len(places)}")

    if target:
        target_line = f"Target: {target.name} ({target.id}) rank {target.rank}"
        if review_counts is not None:
            visitor_count = count_text(review_counts.visitor_review_count) or "n/a"
            blog_count = count_text(review_counts.blog_review_count) or "n/a"
            target_line += f" / 방문자리뷰 {visitor_count} / 블로그리뷰 {blog_count}"
        print(target_line)
    else:
        print(f"Target: {target_name} ({target_id}) not found in returned items")

    print()
    for place in places:
        details = [f"{place.rank}. {place.name}", f"[{place.id}]"]
        if place.category:
            details.append(place.category)
        address = place.road_address or place.address
        if address:
            details.append(address)
        if place.tel or place.virtual_tel:
            details.append(place.tel or place.virtual_tel)
        print(" / ".join(details))


def read_html(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def save_html(path: str, html: str) -> None:
    Path(path).write_text(html, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="네이버 지도 모바일 검색 결과를 20위 이상까지 파싱합니다."
    )
    parser.add_argument("query", help="검색어")
    parser.add_argument("--limit", type=int, default=20, help="출력할 개수입니다. 0 이하면 전체 출력")
    parser.add_argument("--json", action="store_true", help="JSON으로 출력")
    parser.add_argument("--html", help="저장된 지도 검색 HTML을 파싱")
    parser.add_argument("--save-html", help="요청 HTML을 파일로 저장")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument(
        "--user-agent",
        "--ua",
        default="",
        help="HTTP User-Agent를 덮어씁니다. 생략하면 NAVER_MAP_USER_AGENT 환경변수 또는 기본 모바일 Safari UA를 사용합니다.",
    )
    parser.add_argument(
        "--sort",
        default="",
        choices=["relativity", "distance", ""],
        help="지도 검색 정렬 파라미터입니다. 기본 요청에서는 비워둡니다.",
    )
    parser.add_argument("--target-id", default="", help="찾을 목표 업체의 네이버 장소 ID")
    parser.add_argument("--target-name", default="", help="찾을 목표 업체명")
    parser.add_argument("--track-reviews", action="store_true", help="목표 업체 상세 페이지에서 리뷰 수를 조회하고 CSV에 누적")
    parser.add_argument("--review-csv", default=DEFAULT_REVIEW_CSV_PATH, help="리뷰 수 결과를 append할 CSV 파일")
    args = parser.parse_args(argv)
    if not args.target_id.strip() and not args.target_name.strip():
        parser.error("--target-id 또는 --target-name 중 하나는 필요합니다.")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    headers = build_mobile_map_headers(args.user_agent)

    try:
        if args.html:
            html = read_html(args.html)
            url = f"file://{Path(args.html).resolve()}"
            result = parse_map_html(html, args.query, url)
        else:
            html, url = fetch_html(args.query, args.timeout, args.sort, headers)
            if args.save_html:
                save_html(args.save_html, html)
            try:
                result = parse_map_html(html, args.query, url)
            except ValueError:
                if not args.sort:
                    raise
                html, url = fetch_html(args.query, args.timeout, "", headers)
                if args.save_html:
                    save_html(args.save_html, html)
                result = parse_map_html(html, args.query, url)
    except (requests.RequestException, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    review_counts: ReviewCounts | None = None
    if args.track_reviews:
        target = find_target(result.places, args.target_id, args.target_name)
        if target:
            try:
                review_counts = parse_review_counts(fetch_place_detail_html(target.id, args.timeout, headers))
                append_review_csv(result.query, target, review_counts, args.review_csv)
            except (requests.RequestException, OSError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        else:
            review_counts = ReviewCounts(None, None)

    if args.json:
        print(
            json.dumps(
                result_payload(result, args.limit, args.target_id, args.target_name, review_counts),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print_text_result(result, args.limit, args.target_id, args.target_name, review_counts)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
