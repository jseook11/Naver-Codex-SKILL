from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
FORBIDDEN = (
    re.compile(r"(?i)\b(?:authorization|proxy-authorization)\s*[:=]"),
    re.compile(r"(?i)\b(?:cookie|set-cookie)\s*[:=]"),
    re.compile(r"\bNID_(?:AUT|SES)\b"),
    re.compile(r"(?i)\b(?:access|refresh)[_-]?token\s*[:=]"),
)


def test_fixtures_contain_no_authentication_secrets() -> None:
    violations: list[str] = []
    for path in FIXTURES.rglob("*") if FIXTURES.exists() else ():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in FORBIDDEN:
            if pattern.search(text):
                violations.append(f"{path.relative_to(ROOT)}: {pattern.pattern}")
    assert violations == []
