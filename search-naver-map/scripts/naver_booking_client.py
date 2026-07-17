"""Compatibility import for the historical Booking client module."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from naver_place._legacy.naver_booking_client import *  # noqa: E402,F401,F403
