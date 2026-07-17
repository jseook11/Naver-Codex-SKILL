#!/usr/bin/env python3
from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import re
import sys
import time
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


DEFAULT_LIMIT = 4
DEFAULT_EXCLUDE_OWNER_REPLIED = False
PLACE_REVIEW_URL_TEMPLATE = "https://m.place.naver.com/place/{place_id}/review/visitor"
PLACE_GRAPHQL_URL = "https://api.place.naver.com/graphql"
DEFAULT_PAGE_SIZE = 10
VISITOR_REVIEWS_QUERY = r"""
query getVisitorReviews($input: VisitorReviewsInput) {
  visitorReviews(input: $input) {
    total
    items {
      id cursor reviewId rating body thumbnail status visitCount viewCount
      visited created originType receiptInfoUrl nickname showPaymentInfo
      representativeVisitDateTime showRepresentativeVisitDateTime
      author {
        id nickname objectId
        review { totalCount imageCount avgRating }
      }
      media { type thumbnail class videoId videoUrl }
      votedKeywords { code name }
      reply { body created date replyTitle status isSuspended }
      item { name code options }
    }
  }
}
"""
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
SKIP_TEXT_TAGS = {"script", "style", "svg"}

REVIEW_TEXT_KEYS = (
    "body",
    "reviewBody",
    "reviewText",
    "contents",
    "content",
    "text",
    "description",
    "comment",
)
NICKNAME_KEYS = (
    "nickname",
    "userName",
    "username",
    "reviewerName",
    "reviewerNickname",
    "author",
    "authorName",
    "writer",
    "writerName",
    "displayName",
)
DATE_KEYS = (
    "date",
    "visited",
    "created",
    "createdAt",
    "createdString",
    "reviewDate",
    "registeredDate",
    "visitDate",
    "visitDateText",
)
VISIT_COUNT_KEYS = (
    "visitCount",
    "visit_count",
    "visitedCount",
    "visitorVisitCount",
    "reviewerVisitCount",
)
TAG_KEYS = (
    "tags",
    "tagList",
    "keywords",
    "keywordList",
    "reviewKeywords",
    "votedKeywords",
)
IMAGE_COUNT_KEYS = (
    "imageCount",
    "photoCount",
    "mediaCount",
    "reviewImageCount",
    "reviewPhotoCount",
)
IMAGE_LIST_KEYS = (
    "images",
    "imageList",
    "photos",
    "photoList",
    "media",
    "mediaList",
    "thumbnails",
    "reviewImages",
    "reviewPhotos",
)
OWNER_REPLY_KEYS = (
    "ownerReply",
    "owner_reply",
    "ownerReplies",
    "businessOwnerReply",
    "businessReply",
    "managerComment",
    "reply",
    "replies",
    "hasOwnerReply",
    "ownerReplied",
    "isOwnerReplied",
    "ownerReplyCount",
)
BAD_REVIEW_TYPENAME_PARTS = ("fsas", "blog", "feed", "stat", "summary", "score")


class Node:
    def __init__(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]] | None = None,
        parent: "Node | None" = None,
    ) -> None:
        self.tag = tag
        self.attrs = {key: value or "" for key, value in (attrs or [])}
        self.children: list[Node | str] = []
        self.parent = parent


class MiniDOMParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.skip_text_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)

        if tag in SKIP_TEXT_TAGS:
            self.skip_text_depth += 1
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TEXT_TAGS and self.skip_text_depth:
            self.skip_text_depth -= 1

        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.skip_text_depth or not data.strip():
            return
        self.stack[-1].children.append(data)


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def walk(node: Node):
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from walk(child)


def text_content(node: Node) -> str:
    parts: list[str] = []

    def collect(current: Node | str) -> None:
        if isinstance(current, str):
            parts.append(current)
            return
        for child in current.children:
            collect(child)

    collect(node)
    return compact(" ".join(parts))


def descendants(node: Node, predicate) -> list[Node]:
    return [child for child in walk(node) if child is not node and predicate(child)]


def parse_dom(html: str) -> Node:
    parser = MiniDOMParser()
    parser.feed(html)
    return parser.root


def build_place_url(place_id: str) -> str:
    return PLACE_REVIEW_URL_TEMPLATE.format(place_id=place_id)


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


def fetch_review_html(
    place_id: str,
    timeout: int,
    headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    url = build_place_url(place_id)
    response = public_get(
        url,
        headers=headers or build_mobile_map_headers(),
        timeout=timeout,
    )
    response.raise_for_status()
    return response_text(response), url


def graphql_headers(place_id: str, headers: dict[str, str]) -> dict[str, str]:
    return {
        **headers,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://m.place.naver.com",
        "Referer": build_place_url(place_id),
    }


def post_review_page(
    session: requests.Session,
    place_id: str,
    cursor: str | None,
    page_size: int,
    timeout: int,
    headers: dict[str, str],
    retries: int,
    retry_delay: float,
) -> dict[str, Any]:
    input_data: dict[str, Any] = {
        "businessId": place_id,
        "businessType": "place",
        "item": "0",
        "size": page_size,
        "includeContent": True,
        "getUserStats": True,
        "includeReceiptPhotos": True,
        "getReactions": True,
        "getTrailer": True,
    }
    if cursor:
        input_data["after"] = cursor
    payload = {
        "operationName": "getVisitorReviews",
        "variables": {"input": input_data},
        "query": VISITOR_REVIEWS_QUERY,
    }

    for attempt in range(retries + 1):
        clear_cookies = getattr(getattr(session, "cookies", None), "clear", None)
        if callable(clear_cookies):
            clear_cookies()
        try:
            response = session.post(
                PLACE_GRAPHQL_URL,
                headers=graphql_headers(place_id, headers),
                json=payload,
                timeout=timeout,
                allow_redirects=False,
            )
        finally:
            if callable(clear_cookies):
                clear_cookies()
        if 300 <= response.status_code < 400:
            raise requests.HTTPError(
                f"unexpected Place reviews redirect (HTTP {response.status_code})",
                response=response,
            )
        if response.status_code != 429:
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("네이버 리뷰 GraphQL 응답 루트가 객체가 아닙니다.")
            if data.get("errors"):
                raise ValueError(f"네이버 리뷰 GraphQL 오류: {json.dumps(data['errors'], ensure_ascii=False)}")
            reviews = data.get("data", {}).get("visitorReviews")
            if not isinstance(reviews, dict):
                raise ValueError("네이버 리뷰 GraphQL 응답에 visitorReviews가 없습니다.")
            return reviews
        if attempt >= retries:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        wait = float(retry_after) if retry_after and retry_after.isdigit() else retry_delay * (2**attempt)
        time.sleep(wait)
    raise ValueError("네이버 리뷰 GraphQL 재시도 횟수를 초과했습니다.")


def fetch_paginated_review_records(
    place_id: str,
    limit: int,
    timeout: int,
    headers: dict[str, str],
    page_size: int = DEFAULT_PAGE_SIZE,
    request_delay: float = 1.0,
    retries: int = 3,
    retry_delay: float = 5.0,
    save_raw_dir: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if limit < 0:
        raise ValueError("--limit은 0 이상이어야 합니다.")
    if not 1 <= page_size <= 50:
        raise ValueError("--page-size는 1 이상 50 이하여야 합니다.")
    session = requests.Session()
    session.trust_env = False
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor: str | None = None
    total_available: int | None = None
    pages_fetched = 0
    stop_reason = "limit_reached" if limit == 0 else ""

    while len(records) < limit:
        requested = min(page_size, limit - len(records))
        try:
            page = post_review_page(
                session, place_id, cursor, requested, timeout, headers, retries, retry_delay
            )
        except requests.RequestException as exc:
            if not records:
                raise
            stop_reason = f"request_error:{type(exc).__name__}"
            break
        pages_fetched += 1
        if save_raw_dir:
            raw_path = Path(save_raw_dir) / f"page-{pages_fetched:03d}.json"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps(page, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if total_available is None:
            total_available = to_int(page.get("total"))
        items = page.get("items") or []
        if not isinstance(items, list) or not items:
            stop_reason = "no_more_items"
            break
        added = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            key = clean_text(first_present(item.get("id"), item.get("reviewId"), item.get("cursor")))
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            records.append(item)
            added += 1
            if len(records) >= limit:
                break
        next_cursor = clean_text(items[-1].get("cursor")) if isinstance(items[-1], dict) else ""
        if not added:
            stop_reason = "duplicate_page"
            break
        if not next_cursor or next_cursor == cursor:
            stop_reason = "no_next_cursor"
            break
        cursor = next_cursor
        if total_available is not None and len(records) >= total_available:
            stop_reason = "total_reached"
            break
        if len(records) < limit and request_delay > 0:
            time.sleep(request_delay)

    if not stop_reason:
        stop_reason = "limit_reached" if len(records) >= limit else "stopped"
    return records[:limit], {
        "mode": "graphql_cursor",
        "page_size": page_size,
        "pages_fetched": pages_fetched,
        "total_available": total_available,
        "stop_reason": stop_reason,
        "complete": len(records) >= limit or stop_reason == "total_reached",
    }


def read_html(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path: str, text: str) -> None:
    output_path = Path(path)
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")


def clean_text(value: Any) -> str:
    return strip_html_text(value)


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def to_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
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


def value_from_path(record: dict[str, Any], path: str) -> Any:
    current: Any = record
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def first_record_value(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = value_from_path(record, key)
        if value not in (None, ""):
            return value
    return None


def extract_apollo_state(html: str) -> dict[str, Any] | None:
    marker_match = re.search(r"window\.__APOLLO_STATE__\s*=", html)
    if not marker_match:
        return None

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
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)

    return payloads


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


def typename_is_review(record: dict[str, Any]) -> bool:
    typename = clean_text(record.get("__typename"))
    if not typename:
        return False

    lower = typename.lower()
    return lower == "visitorreview"


def has_review_signal(record: dict[str, Any]) -> bool:
    if first_record_value(record, REVIEW_TEXT_KEYS):
        return True
    if first_record_value(record, NICKNAME_KEYS) and first_record_value(record, DATE_KEYS):
        return True
    if first_record_value(record, VISIT_COUNT_KEYS):
        return True
    if any(key in record for key in TAG_KEYS):
        return True
    return False


def is_review_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    if clean_text(record.get("__typename")):
        return typename_is_review(record)
    return has_review_signal(record) and (
        first_record_value(record, NICKNAME_KEYS)
        or first_record_value(record, DATE_KEYS)
        or first_record_value(record, VISIT_COUNT_KEYS)
        or any(key in record for key in OWNER_REPLY_KEYS)
    )


def collect_review_records(
    value: Any,
    output: list[dict[str, Any]],
    state: dict[str, Any] | None = None,
    seen_refs: set[str] | None = None,
    seen_objects: set[int] | None = None,
) -> None:
    seen_refs = seen_refs if seen_refs is not None else set()
    seen_objects = seen_objects if seen_objects is not None else set()

    if isinstance(value, list):
        for item in value:
            collect_review_records(item, output, state, seen_refs, seen_objects)
        return

    if not isinstance(value, dict):
        return

    ref = get_ref(value)
    if ref and state is not None:
        if ref in seen_refs:
            return
        seen_refs.add(ref)
        collect_review_records(resolve_ref(state, value), output, state, seen_refs, seen_objects)
        return

    object_id = id(value)
    if object_id in seen_objects:
        return
    seen_objects.add(object_id)

    if is_review_record(value):
        output.append(value)
        return

    for child in value.values():
        collect_review_records(child, output, state, seen_refs, seen_objects)


def apollo_review_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    seen_objects: set[int] = set()

    root = state.get("ROOT_QUERY")
    if isinstance(root, dict):
        collect_review_records(root, records, state, seen_refs, seen_objects)

    for value in state.values():
        collect_review_records(value, records, state, seen_refs, seen_objects)

    return records


def streaming_review_records(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for payload in payloads:
        collect_review_records(payload, records)
    return records


def normalize_tag_item(value: Any) -> str:
    if isinstance(value, dict):
        value = first_present(
            value.get("name"),
            value.get("text"),
            value.get("label"),
            value.get("title"),
            value.get("keyword"),
            value.get("displayName"),
        )
    return clean_text(value)


def extend_tags(tags: list[str], seen: set[str], value: Any) -> None:
    if value in (None, ""):
        return

    if isinstance(value, dict):
        nested = first_present(value.get("items"), value.get("tags"), value.get("keywords"), value.get("list"))
        if nested is not None:
            extend_tags(tags, seen, nested)
            return
        tag = normalize_tag_item(value)
        if tag and tag not in seen:
            tags.append(tag)
            seen.add(tag)
        return

    if isinstance(value, list):
        for item in value:
            extend_tags(tags, seen, item)
        return

    tag = clean_text(value)
    if tag and tag not in seen:
        tags.append(tag)
        seen.add(tag)


def normalize_tags(record: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for key in TAG_KEYS:
        extend_tags(tags, seen, record.get(key))
    return tags


def count_list_images(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, dict):
        nested = first_present(value.get("items"), value.get("images"), value.get("photos"), value.get("list"))
        if nested is not None and nested is not value:
            return count_list_images(nested)
        return 1 if first_present(value.get("url"), value.get("imageUrl"), value.get("thumbnailUrl"), value.get("thumbnail")) else 0
    if isinstance(value, list):
        return sum(count_list_images(item) for item in value)
    return 1 if clean_text(value) else 0


def image_count(record: dict[str, Any]) -> int:
    for key in IMAGE_COUNT_KEYS:
        value = to_int(record.get(key))
        if value is not None:
            return value

    return sum(count_list_images(record.get(key)) for key in IMAGE_LIST_KEYS)


def value_has_reply(value: Any) -> bool:
    if value in (None, "", False, [], {}):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        return bool(clean_text(value))
    if isinstance(value, list):
        return any(value_has_reply(item) for item in value)
    if isinstance(value, dict):
        return any(value_has_reply(item) for key, item in value.items() if key != "__typename")
    return bool(value)


def reply_has_content(value: Any) -> bool:
    if not isinstance(value, dict):
        return value_has_reply(value)

    for key in ("body", "contents", "content", "text", "replyTitle", "date", "created"):
        if value_has_reply(value.get(key)):
            return True
    status = clean_text(value.get("status")).upper()
    return status not in {"", "NONE", "DELETED", "SUSPENDED"}


def has_owner_reply(record: dict[str, Any]) -> bool:
    for key in OWNER_REPLY_KEYS:
        if key in record:
            value = record.get(key)
            if key in {"reply", "ownerReply", "owner_reply", "businessOwnerReply", "businessReply", "managerComment"}:
                if reply_has_content(value):
                    return True
            elif value_has_reply(value):
                return True
    return False


def resolve_record_ref(state: dict[str, Any] | None, value: Any) -> dict[str, Any]:
    if not state:
        return {}
    return resolve_ref(state, value)


def normalize_author_nickname(record: dict[str, Any], state: dict[str, Any] | None = None) -> str:
    author = resolve_record_ref(state, record.get("author"))
    reviewer = resolve_record_ref(state, record.get("reviewer"))
    profile = resolve_record_ref(state, record.get("profile"))
    member = resolve_record_ref(state, record.get("member"))
    user = resolve_record_ref(state, record.get("user"))
    direct_nickname = first_record_value(record, NICKNAME_KEYS)
    if isinstance(direct_nickname, (dict, list)):
        direct_nickname = None

    return clean_text(
        first_present(
            direct_nickname,
            author.get("nickname"),
            author.get("name"),
            reviewer.get("nickname"),
            reviewer.get("name"),
            profile.get("nickname"),
            member.get("nickname"),
            user.get("nickname"),
            value_from_path(record, "user.nickname"),
            value_from_path(record, "member.nickname"),
            value_from_path(record, "profile.nickname"),
            value_from_path(record, "reviewer.nickname"),
        )
    )


def normalize_review(record: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any] | None:
    review_id = clean_text(first_present(record.get("id"), record.get("reviewId"), record.get("visitorReviewId"))) or None
    text = clean_text(first_record_value(record, REVIEW_TEXT_KEYS))
    nickname = normalize_author_nickname(record, state)
    date = clean_text(first_record_value(record, DATE_KEYS))
    visit_count = to_int(first_record_value(record, VISIT_COUNT_KEYS))
    author = resolve_record_ref(state, record.get("author")) if state else record.get("author") or {}
    reply = resolve_record_ref(state, record.get("reply")) if state else record.get("reply") or {}
    media = record.get("media") if isinstance(record.get("media"), list) else []

    if not any([review_id, text, nickname, date, visit_count]):
        return None

    return {
        "id": review_id,
        "nickname": nickname,
        "date": date,
        "created_at": clean_text(first_present(record.get("created"), record.get("createdAt"))),
        "visited_at": clean_text(
            first_present(record.get("representativeVisitDateTime"), record.get("visited"), record.get("visitDate"))
        ),
        "visited_display": clean_text(first_present(record.get("visited"), record.get("visitDate"))),
        "visit_count": visit_count,
        "text": text,
        "tags": normalize_tags(record),
        "image_count": image_count(record),
        "has_owner_reply": has_owner_reply(record),
        "owner_reply_created_at": clean_text(first_present(reply.get("created"), reply.get("date"))),
        "origin_type": clean_text(record.get("originType")),
        "authentication_method": clean_text(record.get("originType")),
        "receipt_info_url": clean_text(record.get("receiptInfoUrl")),
        "show_payment_info": bool(record.get("showPaymentInfo")),
        "reviewer_id": clean_text(first_present(author.get("objectId"), author.get("id"), record.get("userIdno"))),
        "reviewer_total_reviews": to_int(value_from_path(author, "review.totalCount")),
        "reviewer_total_photos": to_int(value_from_path(author, "review.imageCount")),
        "media_count": len(media),
        "cursor": clean_text(record.get("cursor")),
    }


def review_key(review: dict[str, Any]) -> str:
    if review.get("id"):
        return f"id:{review['id']}"
    return "content:" + "|".join(
        clean_text(review.get(key))
        for key in ("nickname", "date", "text")
    )


def normalize_reviews(records: list[dict[str, Any]], state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        review = normalize_review(record, state)
        if not review:
            continue
        key = review_key(review)
        if key in seen:
            continue
        reviews.append(review)
        seen.add(key)
    return reviews


def review_node_score(node_text: str, attr_text: str) -> int:
    text = clean_text(node_text)
    attrs = attr_text.lower()
    score = 0
    if "review" in attrs:
        score += 1
    if re.search(r"\d{2,4}\.\d{1,2}\.\d{1,2}", text):
        score += 1
    if re.search(r"\d+\s*번째\s*방문", text):
        score += 1
    if any(keyword in text for keyword in ("사장님", "답글", "친절해요", "싱싱해요", "좋아요")):
        score += 1
    return score


def dom_review_record(text: str, image_count_value: int) -> dict[str, Any]:
    date_match = re.search(r"\d{2,4}\.\d{1,2}\.\d{1,2}", text)
    visit_match = re.search(r"(\d+)\s*번째\s*방문", text)
    owner_reply = bool(re.search(r"사장님\s*(답글|댓글)|답글", text))

    cleaned = text
    for match in (date_match, visit_match):
        if match:
            cleaned = cleaned.replace(match.group(0), " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return {
        "date": date_match.group(0) if date_match else "",
        "visitCount": visit_match.group(1) if visit_match else None,
        "body": cleaned,
        "imageCount": image_count_value,
        "ownerReply": owner_reply,
    }


def dom_review_records(html: str) -> list[dict[str, Any]]:
    root = parse_dom(html)
    candidates: list[dict[str, Any]] = []

    for node in walk(root):
        if node.tag not in {"article", "li", "div"}:
            continue
        text = text_content(node)
        if not text or len(text) < 12:
            continue
        attr_text = " ".join(node.attrs.values())
        score = review_node_score(text, attr_text)
        if score < 2:
            continue
        child_has_review = any(
            review_node_score(text_content(child), " ".join(child.attrs.values())) >= 2
            for child in descendants(node, lambda child: child.tag in {"article", "li", "div"})
        )
        if child_has_review:
            continue
        candidates.append(dom_review_record(text, len(descendants(node, lambda child: child.tag == "img"))))

    return candidates


def extract_review_records(html: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    state = extract_apollo_state(html)
    if state:
        records = apollo_review_records(state)
        if records:
            return records, state

    payloads = extract_streaming_payloads(html)
    if payloads:
        records = streaming_review_records(payloads)
        if records:
            return records, None

    return dom_review_records(html), None


def parse_place_reviews_html(
    html: str,
    place_id: str,
    limit: int = DEFAULT_LIMIT,
    exclude_owner_replied: bool = DEFAULT_EXCLUDE_OWNER_REPLIED,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    if limit < 0:
        raise ValueError("--limit은 0 이상이어야 합니다.")

    records, state = extract_review_records(html)
    reviews = normalize_reviews(records, state)
    if exclude_owner_replied:
        reviews = [review for review in reviews if not review["has_owner_reply"]]
    reviews = reviews[:limit]

    return {
        "place_id": place_id,
        "place_url": build_place_url(place_id),
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "source": "naver_place_visitor_reviews",
        "limit": limit,
        "filters": {
            "exclude_owner_replied": exclude_owner_replied,
        },
        "review_count": len(reviews),
        "reviews": reviews,
    }


def build_live_payload(
    records: list[dict[str, Any]],
    place_id: str,
    limit: int,
    exclude_owner_replied: bool,
    pagination: dict[str, Any],
) -> dict[str, Any]:
    reviews = normalize_reviews(records)
    if exclude_owner_replied:
        reviews = [review for review in reviews if not review["has_owner_reply"]]
    reviews = reviews[:limit]
    return {
        "place_id": place_id,
        "place_url": build_place_url(place_id),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "naver_place_visitor_reviews_graphql",
        "limit": limit,
        "filters": {"exclude_owner_replied": exclude_owner_replied},
        "pagination": pagination,
        "review_count": len(reviews),
        "reviews": reviews,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="네이버 모바일 플레이스 방문자리뷰 HTML에서 최근 리뷰를 JSON으로 추출합니다."
    )
    parser.add_argument("place", help="네이버 place_id 또는 플레이스 URL")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="출력할 리뷰 개수입니다.")
    owner_reply_group = parser.add_mutually_exclusive_group()
    owner_reply_group.add_argument(
        "--exclude-owner-replied",
        dest="exclude_owner_replied",
        action="store_true",
        default=DEFAULT_EXCLUDE_OWNER_REPLIED,
        help="사장님 답글이 있는 리뷰를 제외합니다.",
    )
    owner_reply_group.add_argument(
        "--include-owner-replied",
        dest="exclude_owner_replied",
        action="store_false",
        help="사장님 답글이 있는 리뷰도 함께 출력합니다.",
    )
    parser.add_argument("--html", help="저장된 방문자리뷰 HTML 파일을 파싱")
    parser.add_argument("--save-html", help="조회한 방문자리뷰 HTML 저장 경로")
    parser.add_argument("--output", help="JSON 저장 경로. 생략하면 stdout으로 출력")
    parser.add_argument("--timeout", type=int, default=10, help="HTTP 요청 타임아웃 초")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="GraphQL 페이지당 리뷰 수(1~50)")
    parser.add_argument("--request-delay", type=float, default=1.0, help="페이지 요청 사이 대기 초")
    parser.add_argument("--retries", type=int, default=3, help="HTTP 429 페이지별 재시도 횟수")
    parser.add_argument("--retry-delay", type=float, default=5.0, help="HTTP 429 최초 재시도 대기 초")
    parser.add_argument("--save-raw-dir", help="GraphQL 페이지 원본 JSON 저장 디렉터리")
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
            html = read_html(args.html)
            payload = parse_place_reviews_html(
                html,
                place_id=place_id,
                limit=args.limit,
                exclude_owner_replied=args.exclude_owner_replied,
            )
        else:
            if args.save_html:
                html, _ = fetch_review_html(place_id, args.timeout, headers)
                write_text(args.save_html, html)
            records, pagination = fetch_paginated_review_records(
                place_id=place_id,
                limit=args.limit,
                timeout=args.timeout,
                headers=headers,
                page_size=args.page_size,
                request_delay=args.request_delay,
                retries=args.retries,
                retry_delay=args.retry_delay,
                save_raw_dir=args.save_raw_dir,
            )
            payload = build_live_payload(
                records, place_id, args.limit, args.exclude_owner_replied, pagination
            )
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
