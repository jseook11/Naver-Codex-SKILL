"""Versioned, context-aware public result serialization."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
import math
from pathlib import Path
from typing import Any, Mapping

from ..contracts import CapabilityResult, RequestBudget


SCHEMA_VERSION = "1"
VIEWS = {"compact", "standard", "full"}


def to_primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: to_primitive(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_primitive(item) for item in value]
    if isinstance(value, (datetime, date, Path)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _pick(value: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: deepcopy(value[key]) for key in keys if key in value}


def _search_view(data: dict[str, Any], view: str) -> dict[str, Any]:
    if view != "compact":
        return data
    place_keys = (
        "place_id",
        "name",
        "rank",
        "category",
        "address",
        "road_address",
        "place_url",
        "reservation_url",
    )
    data["places"] = [
        _pick(place, place_keys) for place in data.get("places", []) if isinstance(place, Mapping)
    ]
    target = data.get("target")
    if isinstance(target, Mapping) and isinstance(target.get("place"), Mapping):
        target = dict(target)
        target["place"] = _pick(target["place"], place_keys)
        data["target"] = target
    return data


def _hours_view(hours: Any, *, compact: bool) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in hours if isinstance(hours, list) else []:
        if not isinstance(item, Mapping):
            continue
        keys = ("name", "status", "description", "hours")
        if compact:
            keys = ("name", "status")
        output.append(_pick(item, keys))
    return output


def _detail_view(data: dict[str, Any], view: str) -> dict[str, Any]:
    if view == "full":
        return data
    compact = view == "compact"
    base = data.get("base")
    if isinstance(base, Mapping):
        base_keys = (
            "name",
            "category",
            "road_address",
            "address",
            "visitor_review_count",
            "blog_review_count",
            "homepage",
            "links",
        )
        if compact:
            base_keys = (
                "name",
                "category",
                "road_address",
                "address",
                "visitor_review_count",
                "blog_review_count",
            )
        data["base"] = _pick(base, base_keys)
    if "business_hours" in data:
        data["business_hours"] = _hours_view(
            data.get("business_hours"), compact=compact
        )
    menu_keys = ("id", "name", "price", "recommend", "index")
    if compact:
        menu_keys = ("name", "price")
    if "menus" in data:
        data["menus"] = [
            _pick(menu, menu_keys)
            for menu in data.get("menus", [])
            if isinstance(menu, Mapping)
        ]
    if compact:
        data.pop("feeds", None)
        data.pop("blog_reviews", None)
    else:
        feed_keys = ("id", "title", "url", "author", "date", "created_at")
        if "feeds" in data:
            data["feeds"] = [
                _pick(feed, feed_keys)
                for feed in data.get("feeds", [])
                if isinstance(feed, Mapping)
            ]
        blog_keys = ("id", "source", "type", "title", "url", "author", "date")
        if "blog_reviews" in data:
            data["blog_reviews"] = [
                _pick(review, blog_keys)
                for review in data.get("blog_reviews", [])
                if isinstance(review, Mapping)
            ]
    data.pop("photos", None)
    return data


def _drop_keys(value: Any, forbidden: set[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _drop_keys(item, forbidden)
            for key, item in value.items()
            if key.casefold() not in forbidden
        }
    if isinstance(value, list):
        return [_drop_keys(item, forbidden) for item in value]
    return value


def _reviews_view(data: dict[str, Any], view: str) -> dict[str, Any]:
    if view == "full":
        return data
    return _drop_keys(
        data,
        {
            "reviewer_id",
            "receipt_info_url",
            "receipt_url",
            "profile_image_url",
            "cursor",
        },
    )


def _booking_view(data: dict[str, Any], view: str) -> dict[str, Any]:
    if view == "full":
        return data
    trimmed = _drop_keys(
        data,
        {
            "description",
            "descriptions",
            "image",
            "images",
            "image_url",
            "image_urls",
            "thumbnail",
            "thumbnails",
            "resources",
        },
    )
    if view == "compact":
        trimmed = _drop_keys(trimmed, {"raw", "metadata", "notices"})
    return trimmed


def apply_view(capability: str, data: Any, view: str) -> Any:
    primitive = to_primitive(data)
    if not isinstance(primitive, dict):
        return primitive
    if capability == "map.search":
        return _search_view(primitive, view)
    if capability == "place.detail":
        return _detail_view(primitive, view)
    if capability == "place.reviews":
        return _reviews_view(primitive, view)
    if capability == "booking.availability":
        return _booking_view(primitive, view)
    return primitive


class V1Serializer:
    def __init__(self, *, view: str = "standard") -> None:
        if view not in VIEWS:
            raise ValueError(f"unsupported view: {view}")
        self.view = view

    def serialize(self, result: CapabilityResult) -> dict[str, Any]:
        request = to_primitive(result.request)
        if isinstance(request, dict):
            request.setdefault("view", self.view)
        budget = result.budget
        budget_payload = (
            budget.snapshot()
            if isinstance(budget, RequestBudget)
            else {
                "requests_used": 0,
                "request_limit": None,
                "elapsed_seconds": 0.0,
                "elapsed_limit_seconds": None,
            }
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": result.status.value,
            "capability": result.capability,
            "request": request,
            "data": apply_view(result.capability, result.data, self.view),
            "provenance": to_primitive(result.provenance),
            "completeness": to_primitive(result.completeness),
            "budget": budget_payload,
            "warnings": to_primitive(result.warnings),
            "errors": to_primitive(result.errors),
        }


__all__ = ["SCHEMA_VERSION", "V1Serializer", "VIEWS", "apply_view", "to_primitive"]
