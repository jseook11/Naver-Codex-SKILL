"""Compatibility formatting for historical script-shaped JSON."""

from __future__ import annotations

from typing import Any, Mapping

from ..contracts import CapabilityResult
from .v1 import to_primitive


def _legacy_place(place: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(place)
    output["id"] = output.pop("place_id", "")
    output["tel"] = output.pop("telephone", "")
    output["virtual_tel"] = output.pop("virtual_telephone", "")
    return output


class LegacyFormatter:
    """Return old top-level payloads without re-running a capability."""

    def serialize(self, result: CapabilityResult) -> Any:
        data = to_primitive(result.data)
        if not isinstance(data, dict):
            return data
        if result.capability == "map.search":
            places = [
                _legacy_place(place)
                for place in data.get("places", [])
                if isinstance(place, Mapping)
            ]
            target = data.get("target")
            if isinstance(target, Mapping):
                target = dict(target)
                target["id"] = target.pop("place_id", "") or ""
                if isinstance(target.get("place"), Mapping):
                    target["place"] = _legacy_place(target["place"])
            return {
                "query": data.get("query", ""),
                "source": "naver_map_mobile",
                "url": data.get("url", ""),
                "fetched_at": result.provenance[0].fetched_at if result.provenance else None,
                "total_count": data.get("total_count"),
                "returned_count": data.get("upstream_returned_count", len(places)),
                "shown_count": len(places),
                "search_type": data.get("search_type", ""),
                "location_query_info": data.get("location_query_info", ""),
                "page_info": data.get("page_info", {}),
                "target": target,
                "places": places,
            }
        if result.capability == "place.reviews":
            owner_reply = result.request.get("owner_reply", "all")
            return {
                "place_id": data.get("place_id"),
                "place_url": data.get("place_url"),
                "fetched_at": result.provenance[0].fetched_at if result.provenance else None,
                "source": "naver_place_visitor_review_snapshots",
                "limit": result.request.get("limit"),
                "filters": {"exclude_owner_replied": owner_reply == "exclude_replied"},
                "pagination": {
                    "pages_fetched": result.completeness.pages_fetched,
                    "total": data.get("total_available"),
                    "stop_reason": result.completeness.stop_reason,
                },
                "review_count": data.get("returned_count", 0),
                "reviews": data.get("reviews", []),
            }
        return data


__all__ = ["LegacyFormatter"]
