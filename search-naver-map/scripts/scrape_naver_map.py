#!/usr/bin/env python3
"""Historical CLI path backed by the canonical internal implementation."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from naver_place._legacy.scrape_naver_map import *  # noqa: E402,F401,F403
from naver_place._legacy.scrape_naver_map import main as _main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(_main())
