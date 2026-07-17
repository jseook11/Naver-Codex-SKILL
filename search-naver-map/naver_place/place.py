"""Stable Place identity types shared across source-specific adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PlaceRef:
    place_id: str
    name: str = ""
    place_url: str = ""
    reservation_url: str = ""

    def __post_init__(self) -> None:
        if not str(self.place_id).strip():
            raise ValueError("place_id cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlaceSummary:
    place_id: str
    name: str
    rank: int
    category: str = ""
    address: str = ""
    road_address: str = ""
    telephone: str = ""
    virtual_telephone: str = ""
    latitude: float | None = None
    longitude: float | None = None
    place_url: str = ""
    reservation_url: str = ""
    has_menu_info: bool = False
    has_npay: bool = False

    def __post_init__(self) -> None:
        if not str(self.place_id).strip() or not str(self.name).strip():
            raise ValueError("Place summaries require place_id and name")
        if self.rank < 1:
            raise ValueError("Place rank must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
