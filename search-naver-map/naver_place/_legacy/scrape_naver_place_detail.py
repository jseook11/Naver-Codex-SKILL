#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

try:
    from .scrape_naver_map import (
        build_mobile_map_headers,
        extract_balanced_json,
        public_get,
        response_text,
        strip_html_text,
    )
except ImportError:  # Direct script execution.
    from scrape_naver_map import (
        build_mobile_map_headers,
        extract_balanced_json,
        public_get,
        response_text,
        strip_html_text,
    )


PLACE_URL_TEMPLATE = "https://m.place.naver.com/place/{place_id}/{section}"
PLACE_GRAPHQL_URL = "https://api.place.naver.com/graphql"
PHOTO_KEYS = (
    "url",
    "origin",
    "imageUrl",
    "image_url",
    "thumbnail",
    "thumbnailUrl",
    "landingUrl",
)

BUSINESS_HOURS_GRAPHQL_QUERY = """
query getDetail($id: String!, $deviceType: String) {
  business: placeDetail(input: { id: $id, isNx: false, deviceType: $deviceType }) {
    base {
      id
      name
    }
    newBusinessHours(format: restaurant) {
      name
      businessStatusDescription {
        status
        blindDescription
        description
        showEndsNextDay
      }
      businessHours {
        day
        businessHours {
          start
          end
        }
        breakHours {
          start
          end
        }
        description
        lastOrderTimes {
          type
          time
        }
        showEndsNextDay
      }
      comingIrregularClosedDays {
        name
        startDate
        endDate
        displayInShowMore
      }
      comingRegularClosedDays
      freeText
    }
  }
}
""".strip()


def extract_place_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return value

    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("네이버 플레이스 URL 또는 숫자 place_id를 입력하세요.") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not (host == "naver.com" or host.endswith(".naver.com"))
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("네이버 플레이스 URL 또는 숫자 place_id를 입력하세요.")

    match = re.search(r"/place/(\d+)(?:/|$)", parsed.path)
    if not match:
        raise ValueError("네이버 플레이스 URL에서 place_id를 찾지 못했습니다.")
    return match.group(1)


def build_place_url(place_id: str, section: str = "home") -> str:
    return PLACE_URL_TEMPLATE.format(place_id=place_id, section=section)


def fetch_place_html(
    place_id: str,
    section: str,
    timeout: int,
    headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    url = build_place_url(place_id, section)
    response = public_get(
        url,
        headers=headers or build_mobile_map_headers(),
        timeout=timeout,
    )
    response.raise_for_status()
    return response_text(response), url


def fetch_place_business_hours(
    place_id: str,
    timeout: int,
    device_type: str = "mobile",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {
        **(headers or build_mobile_map_headers()),
        "accept": "*/*",
        "accept-language": "ko",
        "content-type": "application/json",
        "origin": "https://m.place.naver.com",
        "referer": build_place_url(place_id, "home"),
    }
    payload = {
        "operationName": "getDetail",
        "variables": {"id": place_id, "deviceType": device_type},
        "query": BUSINESS_HOURS_GRAPHQL_QUERY,
    }
    session = requests.Session()
    session.trust_env = False
    response = session.post(
        PLACE_GRAPHQL_URL,
        headers=headers,
        json=payload,
        timeout=timeout,
        allow_redirects=False,
    )
    if 300 <= response.status_code < 400:
        raise requests.HTTPError(
            f"unexpected Place detail redirect (HTTP {response.status_code})",
            response=response,
        )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("네이버 플레이스 영업시간 응답이 객체가 아닙니다.")
    return data


def read_html(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def read_json(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} JSON 최상위 값이 객체가 아닙니다.")
    return data


def write_text(path: str, text: str) -> None:
    output_path = Path(path)
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")


def extract_apollo_state(html: str) -> dict[str, Any]:
    marker_match = re.search(r"window\.__APOLLO_STATE__\s*=", html)
    if not marker_match:
        raise ValueError("window.__APOLLO_STATE__를 찾지 못했습니다.")

    payload_text, _ = extract_balanced_json(html, marker_match.end())
    if not payload_text:
        raise ValueError("window.__APOLLO_STATE__ JSON을 추출하지 못했습니다.")

    try:
        state = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"window.__APOLLO_STATE__ JSON 파싱 실패: {exc}") from exc

    if not isinstance(state, dict):
        raise ValueError("window.__APOLLO_STATE__가 객체가 아닙니다.")
    return state


def clean_text(value: Any) -> str:
    return strip_html_text(value)


def extract_review_counts_from_html(html: str) -> dict[str, int]:
    matches: dict[str, int] = {}
    candidates: list[str] = []

    for pattern in (
        r'<meta[^>]+(?:property|name)=["\'](?:og:description|twitter:description)["\'][^>]+content=["\']([^"\']*)["\']',
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\'](?:og:description|twitter:description)["\']',
    ):
        candidates.extend(html_lib.unescape(match) for match in re.findall(pattern, html, flags=re.IGNORECASE))

    candidates.append(clean_text(html))
    patterns = (
        ("visitor_review_count", r"방문자\s*리뷰\s*([0-9,]+)|방문자리뷰\s*([0-9,]+)"),
        ("blog_review_count", r"블로그\s*리뷰\s*([0-9,]+)|블로그리뷰\s*([0-9,]+)"),
    )
    for candidate in candidates:
        for key, pattern in patterns:
            if key in matches:
                continue
            match = re.search(pattern, candidate)
            if not match:
                continue
            value = first_present(*(group for group in match.groups()))
            parsed = to_int(value)
            if parsed is not None:
                matches[key] = parsed
    return matches


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = re.sub(r"[^0-9-]", "", str(value))
    if not text or text == "-":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def get_ref(value: Any) -> str:
    if isinstance(value, dict):
        ref = value.get("__ref")
        if isinstance(ref, str):
            return ref
    return ""


def resolve_ref(state: dict[str, Any], value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and "__ref" not in value:
        return value
    ref = get_ref(value)
    if not ref:
        return {}
    resolved = state.get(ref)
    return resolved if isinstance(resolved, dict) else {}


def iter_records(state: dict[str, Any], typename: str | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for value in state.values():
        if not isinstance(value, dict):
            continue
        if typename is None or value.get("__typename") == typename:
            records.append(value)
    return records


def root_place_detail(state: dict[str, Any]) -> dict[str, Any]:
    root = state.get("ROOT_QUERY")
    if isinstance(root, dict):
        for key, value in root.items():
            if key.startswith("placeDetail") and isinstance(value, dict):
                return value

    for value in state.values():
        if isinstance(value, dict) and value.get("__typename") == "PlaceDetail":
            return value
    return {}


def root_feeds_result(state: dict[str, Any]) -> dict[str, Any]:
    root = state.get("ROOT_QUERY")
    if isinstance(root, dict):
        for key, value in root.items():
            if key.startswith("feeds") and isinstance(value, dict):
                return value
    return {}


def base_record(state: dict[str, Any], place_id: str, detail: dict[str, Any]) -> dict[str, Any]:
    base = resolve_ref(state, detail.get("base"))
    if base:
        return base

    for typename in ("PlaceDetailBase", "PlaceDetail"):
        for record in iter_records(state, typename):
            if str(record.get("id", "")).strip() == place_id:
                return record

    return {}


def detail_value(detail: dict[str, Any], prefix: str) -> Any:
    if prefix in detail:
        return detail[prefix]
    for key, value in detail.items():
        if key.startswith(f"{prefix}("):
            return value
    return None


def normalize_link(record: dict[str, Any]) -> dict[str, str] | None:
    url = clean_text(first_present(record.get("url"), record.get("landingUrl"), record.get("href")))
    if not url:
        return None

    payload: dict[str, str] = {"url": url}
    link_type = clean_text(first_present(record.get("type"), record.get("typeI18n"), record.get("name")))
    name = clean_text(first_present(record.get("name"), record.get("title"), record.get("typeI18n"), record.get("type")))
    if link_type:
        payload["type"] = link_type
    if name:
        payload["name"] = name
    return payload


def normalize_links(base: dict[str, Any], detail: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, Any]] = []

    base_links = base.get("links")
    if isinstance(base_links, list):
        candidates.extend(item for item in base_links if isinstance(item, dict))

    homepage_blocks = [
        detail_value(detail, "homepages"),
        detail.get("shopWindow", {}).get("homepages") if isinstance(detail.get("shopWindow"), dict) else None,
    ]
    for homepages in homepage_blocks:
        if not isinstance(homepages, dict):
            continue
        repr_link = homepages.get("repr")
        if isinstance(repr_link, dict):
            candidates.append(repr_link)
        etc_links = homepages.get("etc")
        if isinstance(etc_links, list):
            candidates.extend(item for item in etc_links if isinstance(item, dict))

    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        link = normalize_link(candidate)
        if not link or link["url"] in seen:
            continue
        links.append(link)
        seen.add(link["url"])
    return links


def homepage_url(base: dict[str, Any], links: list[dict[str, str]]) -> str | None:
    homepage = clean_text(first_present(base.get("homepage"), base.get("homePage"), base.get("url")))
    if homepage:
        return homepage

    for link in links:
        link_type = link.get("type", "")
        if link_type in {"홈페이지", "homepage"} or "홈페이지" in link_type.lower() or "homepage" in link_type.lower():
            return link["url"]
    return None


def normalize_image(value: Any, **extra: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        url = clean_text(value)
        if not url:
            return None
        payload: dict[str, Any] = {"url": url}
    elif isinstance(value, dict):
        url = clean_text(first_present(*(value.get(key) for key in PHOTO_KEYS)))
        if not url:
            return None
        payload = {"url": url}
        field_map = {
            "origin": "origin",
            "width": "width",
            "height": "height",
            "desc": "description",
            "description": "description",
            "infoTitle": "info_title",
            "title": "title",
            "source": "source",
            "mediaType": "media_type",
            "thumbnailRatio": "thumbnail_ratio",
            "isVideo": "is_video",
            "isVideoThumbnail": "is_video",
            "representative": "representative",
        }
        for source_key, output_key in field_map.items():
            field_value = value.get(source_key)
            if field_value not in (None, ""):
                payload[output_key] = clean_text(field_value) if isinstance(field_value, str) else field_value
    else:
        return None

    for key, field_value in extra.items():
        if field_value not in (None, ""):
            payload[key] = field_value
    return payload


def append_image(target: list[dict[str, Any]], value: Any, seen: set[str], **extra: Any) -> None:
    image = normalize_image(value, **extra)
    if not image:
        return
    url = image["url"]
    if url in seen:
        return
    target.append(image)
    seen.add(url)


def image_list(values: Any, **extra: Any) -> list[dict[str, Any]]:
    items = values if isinstance(values, list) else [values]
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        append_image(output, item, seen, **extra)
    return output


def collect_place_images(base: dict[str, Any], detail: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()

    image_blocks = [
        base.get("images"),
        detail.get("images"),
    ]
    for key, value in detail.items():
        if key.startswith("images("):
            image_blocks.append(value)

    for block in image_blocks:
        if isinstance(block, dict):
            values = block.get("images")
        else:
            values = block
        if not isinstance(values, list):
            continue
        for index, item in enumerate(values):
            append_image(output, item, seen, index=index)

    return output


def collect_menu_board_images(state: dict[str, Any], detail: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in detail.get("menuImages") or []:
        append_image(output, item, seen)

    for record in iter_records(state):
        typename = str(record.get("__typename", ""))
        if typename in {"MenuImage", "MenuBoardImage"}:
            append_image(output, record, seen)

    return output


def menu_records(state: dict[str, Any], detail: dict[str, Any]) -> list[dict[str, Any]]:
    refs = detail.get("menus")
    if isinstance(refs, list):
        records = [resolve_ref(state, ref) for ref in refs]
        return [record for record in records if record]
    return iter_records(state, "Menu")


def normalize_menu(record: dict[str, Any]) -> dict[str, Any]:
    images = image_list(record.get("images") or record.get("image") or record.get("imageUrl"))
    payload: dict[str, Any] = {
        "id": clean_text(record.get("id")) or None,
        "name": clean_text(record.get("name")),
        "price": clean_text(record.get("price")),
        "description": clean_text(first_present(record.get("description"), record.get("desc"))),
        "images": images,
    }

    for source_key, output_key in (
        ("priceType", "price_type"),
        ("recommend", "recommend"),
        ("index", "index"),
        ("priority", "priority"),
    ):
        value = record.get(source_key)
        if value not in (None, ""):
            payload[output_key] = value

    return payload


def collect_menu_images(menus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for menu in menus:
        menu_name = menu.get("name")
        for image in menu.get("images", []):
            append_image(output, image, seen, menu_name=menu_name)
    return output


def normalize_business_hours(detail: dict[str, Any], base: dict[str, Any]) -> list[dict[str, Any]]:
    hours = first_present(
        detail.get("newBusinessHours"),
        detail_value(detail, "businessHours"),
        base.get("businessHours"),
        base.get("openingHours"),
    )
    if not isinstance(hours, list):
        return []
    return normalize_business_hour_items(hours)


def format_business_hours_range(start: Any, end: Any, show_ends_next_day: Any = None) -> str | None:
    start_text = clean_text(start)
    end_text = clean_text(end)
    if not start_text or not end_text:
        return None
    next_day = bool(show_ends_next_day)
    return f"{start_text} - {'다음 날 ' if next_day else ''}{end_text}"


def normalize_time_ranges(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    output: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        start = clean_text(value.get("start"))
        end = clean_text(value.get("end"))
        if not start and not end:
            continue
        output.append(
            {
                "start": start,
                "end": end,
                "hours": format_business_hours_range(start, end),
            }
        )
    return output


def normalize_last_order_times(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []
    output: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        item = {
            "type": clean_text(value.get("type")),
            "time": clean_text(value.get("time")),
        }
        if item["type"] or item["time"]:
            output.append(item)
    return output


def normalize_business_hour_items(hours: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []

    for item in hours:
        if not isinstance(item, dict):
            continue
        if item.get("businessHours") and isinstance(item.get("businessHours"), list):
            status = item.get("businessStatusDescription") if isinstance(item.get("businessStatusDescription"), dict) else {}
            entries = []
            for entry in item.get("businessHours", []):
                if not isinstance(entry, dict):
                    continue
                business_hours = entry.get("businessHours") if isinstance(entry.get("businessHours"), dict) else {}
                start = clean_text(business_hours.get("start"))
                end = clean_text(business_hours.get("end"))
                description = clean_text(entry.get("description"))
                show_ends_next_day = entry.get("showEndsNextDay")
                hours_text = description or format_business_hours_range(start, end, show_ends_next_day)
                payload: dict[str, Any] = {
                    "day": clean_text(entry.get("day")),
                    "start": start or None,
                    "end": end or None,
                    "hours": hours_text,
                    "description": description,
                    "show_ends_next_day": show_ends_next_day,
                    "is_day_off": bool(description and not (start or end)),
                }
                break_hours = normalize_time_ranges(entry.get("breakHours"))
                if break_hours:
                    payload["break_hours"] = break_hours
                last_order_times = normalize_last_order_times(entry.get("lastOrderTimes"))
                if last_order_times:
                    payload["last_order_times"] = last_order_times
                entries.append(payload)

            irregular_closed_days = item.get("comingIrregularClosedDays")
            if isinstance(irregular_closed_days, list):
                irregular_closed_days = [
                    {
                        "name": clean_text(value.get("name")),
                        "start_date": clean_text(value.get("startDate")),
                        "end_date": clean_text(value.get("endDate")),
                        "display_in_show_more": value.get("displayInShowMore"),
                    }
                    for value in irregular_closed_days
                    if isinstance(value, dict)
                ]
            else:
                irregular_closed_days = []

            normalized.append(
                {
                    "name": clean_text(item.get("name")) or "영업시간",
                    "status": clean_text(status.get("status")),
                    "blind_description": clean_text(status.get("blindDescription")),
                    "description": clean_text(status.get("description")),
                    "show_ends_next_day": status.get("showEndsNextDay"),
                    "regular_closed_days": clean_text(item.get("comingRegularClosedDays")),
                    "irregular_closed_days": irregular_closed_days,
                    "free_text": clean_text(item.get("freeText")),
                    "hours": entries,
                }
            )
        else:
            normalized.append(
                {
                    key: clean_text(value) if isinstance(value, str) else value
                    for key, value in item.items()
                    if key != "__typename"
                }
            )
    return normalized


def normalize_business_hours_payload(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    business = payload.get("data", {}).get("business") if isinstance(payload.get("data"), dict) else None
    if not isinstance(business, dict):
        return []
    hours = business.get("newBusinessHours")
    if not isinstance(hours, list):
        return []
    return normalize_business_hour_items(hours)


def normalize_base(
    state: dict[str, Any],
    place_id: str,
    base: dict[str, Any],
    detail: dict[str, Any],
    html_review_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    links = normalize_links(base, detail)
    html_review_counts = html_review_counts or {}

    visitor_stats = state.get(f"VisitorReviewStatsResult:{place_id}")
    if not isinstance(visitor_stats, dict):
        visitor_stats = {}

    fsas_reviews = detail.get("fsasReviews") if isinstance(detail.get("fsasReviews"), dict) else {}

    return {
        "name": clean_text(base.get("name")),
        "category": clean_text(base.get("category")),
        "road_address": clean_text(first_present(base.get("roadAddress"), base.get("road_address"), base.get("road"))),
        "address": clean_text(base.get("address")),
        "description": clean_text(first_present(base.get("description"), base.get("desc"), detail_value(detail, "description"))),
        "visitor_review_count": to_int(
            first_present(
                base.get("visitorReviewCount"),
                base.get("visitorReviewsTotal"),
                base.get("visitor_review_count"),
                visitor_stats.get("visitorReviewsTotal"),
                visitor_stats.get("review", {}).get("totalCount") if isinstance(visitor_stats.get("review"), dict) else None,
                html_review_counts.get("visitor_review_count"),
            )
        ),
        "blog_review_count": to_int(
            first_present(
                base.get("blogReviewCount"),
                base.get("blogReviewsTotal"),
                base.get("blog_review_count"),
                fsas_reviews.get("total") if isinstance(fsas_reviews, dict) else None,
                html_review_counts.get("blog_review_count"),
            )
        ),
        "homepage": homepage_url(base, links),
        "links": links,
    }


def feed_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    feeds_result = root_feeds_result(state)
    refs = feeds_result.get("feeds")
    if isinstance(refs, list):
        records = [resolve_ref(state, ref) for ref in refs]
        return [record for record in records if record]
    return iter_records(state, "Feed")


def normalize_feed(record: dict[str, Any]) -> dict[str, Any]:
    media = image_list(first_present(record.get("media"), record.get("images"), record.get("imageUrl")))
    payload: dict[str, Any] = {
        "id": clean_text(record.get("id")) or None,
        "feed_id": record.get("feedId"),
        "type": clean_text(record.get("type")),
        "title": clean_text(record.get("title")),
        "description": clean_text(first_present(record.get("description"), record.get("desc"), record.get("contents"))),
        "category": clean_text(record.get("category")),
        "period": clean_text(record.get("period")),
        "created": clean_text(first_present(record.get("created"), record.get("date"), record.get("createdString"))),
        "relative_created": clean_text(record.get("relativeCreated")),
        "is_pinned": record.get("isPinned"),
        "media": media,
    }

    thumbnail = normalize_image(record.get("thumbnail"))
    if thumbnail:
        payload["thumbnail"] = thumbnail
    url = clean_text(record.get("url"))
    if url:
        payload["url"] = url
    blog_id = clean_text(record.get("blogId"))
    if blog_id:
        payload["blog_id"] = blog_id
    return payload


def normalize_blog_review(record: dict[str, Any], source: str) -> dict[str, Any]:
    payload = {
        "id": clean_text(first_present(record.get("reviewId"), record.get("id"), record.get("feedId"))) or None,
        "source": source,
        "type": clean_text(record.get("type")),
        "title": clean_text(record.get("title")),
        "url": clean_text(record.get("url")),
        "author": clean_text(first_present(record.get("author"), record.get("authorName"), record.get("blogId"))),
        "date": clean_text(first_present(record.get("date"), record.get("created"), record.get("createdString"))),
        "contents": clean_text(first_present(record.get("contents"), record.get("description"), record.get("desc"))),
        "thumbnail_url": clean_text(first_present(record.get("thumbnailUrl"), record.get("thumbnail", {}).get("url") if isinstance(record.get("thumbnail"), dict) else None)),
    }

    thumbnail_list = record.get("thumbnailUrlList")
    if isinstance(thumbnail_list, list):
        payload["thumbnail_url_list"] = [clean_text(item) for item in thumbnail_list if clean_text(item)]
    return payload


def add_blog_review(
    output: list[dict[str, Any]],
    record: dict[str, Any],
    seen: set[str],
    source: str,
) -> None:
    review = normalize_blog_review(record, source)
    key = review.get("url") or review.get("id") or review.get("title")
    if not key or key in seen:
        return
    output.append(review)
    seen.add(str(key))


def collect_blog_reviews(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()

    for state in states:
        for record in iter_records(state, "FsasReview"):
            add_blog_review(output, record, seen, "fsas")
        for record in feed_records(state):
            if clean_text(record.get("type")).upper() == "BLOG":
                add_blog_review(output, record, seen, "feed")

    return output


def collect_blog_thumbnails(blog_reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for review in blog_reviews:
        title = review.get("title")
        append_image(output, review.get("thumbnail_url"), seen, title=title)
        for thumbnail in review.get("thumbnail_url_list", []):
            append_image(output, thumbnail, seen, title=title)
    return output


def collect_feeds(feed_state: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for record in feed_records(feed_state):
        if clean_text(record.get("type")).upper() == "FEED":
            output.append(normalize_feed(record))
    return output


def collect_feed_images(feeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for feed in feeds:
        feed_title = feed.get("title")
        for image in feed.get("media", []):
            append_image(output, image, seen, feed_title=feed_title)
        if "thumbnail" in feed:
            append_image(output, feed["thumbnail"], seen, feed_title=feed_title)
    return output


def parse_place_detail_html(
    home_html: str,
    place_id: str,
    feed_html: str | None = None,
    business_hours_payload: dict[str, Any] | None = None,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    home_state = extract_apollo_state(home_html)
    feed_state = extract_apollo_state(feed_html) if feed_html else {}
    html_review_counts = extract_review_counts_from_html(home_html)

    detail = root_place_detail(home_state)
    base = base_record(home_state, place_id, detail)
    menus = [normalize_menu(record) for record in menu_records(home_state, detail) if clean_text(record.get("name"))]
    feeds = collect_feeds(feed_state) if feed_state else []
    blog_reviews = collect_blog_reviews([home_state, feed_state] if feed_state else [home_state])

    photos = {
        "place_images": collect_place_images(base, detail),
        "menu_board_images": collect_menu_board_images(home_state, detail),
        "menu_item_images": collect_menu_images(menus),
        "feed_images": collect_feed_images(feeds),
        "blog_thumbnails": collect_blog_thumbnails(blog_reviews),
    }
    business_hours = normalize_business_hours_payload(business_hours_payload)
    if not business_hours:
        business_hours = normalize_business_hours(detail, base)

    return {
        "place_id": place_id,
        "place_url": build_place_url(place_id, "home"),
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "base": normalize_base(home_state, place_id, base, detail, html_review_counts),
        "business_hours": business_hours,
        "photos": photos,
        "menus": menus,
        "feeds": feeds,
        "blog_reviews": blog_reviews,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="네이버 플레이스 상세 정보, 영업시간, 사진, 메뉴, 소식, 블로그 리뷰를 JSON으로 출력합니다."
    )
    parser.add_argument("place", help="네이버 place_id 또는 플레이스 URL")
    parser.add_argument("--output", help="JSON 저장 경로. 생략하면 stdout으로 출력")
    parser.add_argument("--html", help="저장된 /home HTML 파일을 파싱")
    parser.add_argument("--feed-html", help="저장된 /feed HTML 파일을 파싱")
    parser.add_argument("--hours-json", dest="business_hours_json", help="저장된 영업시간 원본 JSON 파일을 함께 사용")
    parser.add_argument("--business-hours-json", dest="business_hours_json", help=argparse.SUPPRESS)
    parser.add_argument("--save-html", help="조회한 /home HTML 저장 경로")
    parser.add_argument("--save-feed-html", help="조회한 /feed HTML 저장 경로")
    parser.add_argument("--save-hours-json", dest="save_business_hours_json", help="조회한 영업시간 원본 JSON 저장 경로")
    parser.add_argument("--save-business-hours-json", dest="save_business_hours_json", help=argparse.SUPPRESS)
    parser.add_argument("--no-feed", action="store_true", help="/feed 추가 조회를 생략")
    parser.add_argument("--offline", action="store_true", help="저장된 파일만 사용하고 추가 네이버 요청을 하지 않음")
    parser.add_argument("--no-business-hours", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=int, default=10, help="HTTP 요청 타임아웃 초")
    parser.add_argument(
        "--user-agent",
        "--ua",
        default="",
        help="HTTP User-Agent를 덮어씁니다. 생략하면 NAVER_MAP_USER_AGENT 환경변수 또는 기본 모바일 Safari UA를 사용합니다.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    headers = build_mobile_map_headers(args.user_agent)

    try:
        place_id = extract_place_id(args.place)

        if args.html:
            home_html = read_html(args.html)
        elif args.offline:
            raise ValueError("--offline은 --html 저장 파일과 함께 사용하세요.")
        else:
            home_html, _ = fetch_place_html(place_id, "home", args.timeout, headers)
            if args.save_html:
                write_text(args.save_html, home_html)

        feed_html: str | None = None
        if args.feed_html:
            feed_html = read_html(args.feed_html)
        elif not args.no_feed and not args.html:
            try:
                feed_html, _ = fetch_place_html(place_id, "feed", args.timeout, headers)
                if args.save_feed_html:
                    write_text(args.save_feed_html, feed_html)
            except requests.RequestException as exc:
                print(f"warning: feed fetch failed: {exc}", file=sys.stderr)

        business_hours_payload: dict[str, Any] | None = None
        if args.business_hours_json:
            business_hours_payload = read_json(args.business_hours_json)
        elif not args.no_business_hours and not args.offline:
            try:
                business_hours_payload = fetch_place_business_hours(place_id, args.timeout, headers=headers)
                if args.save_business_hours_json:
                    write_text(
                        args.save_business_hours_json,
                        json.dumps(business_hours_payload, ensure_ascii=False, indent=2) + "\n",
                    )
            except (requests.RequestException, ValueError) as exc:
                print(f"warning: business hours fetch failed: {exc}", file=sys.stderr)

        payload = parse_place_detail_html(home_html, place_id, feed_html, business_hours_payload)
        json_text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.output:
            write_text(args.output, json_text + "\n")
        else:
            print(json_text)
    except (requests.RequestException, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
