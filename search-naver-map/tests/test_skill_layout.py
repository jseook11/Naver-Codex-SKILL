from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_skill_frontmatter_uses_only_trigger_fields() -> None:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, _ = text.split("---", 2)
    keys = {
        line.split(":", 1)[0].strip()
        for line in frontmatter.splitlines()
        if line.strip() and not line.startswith((" ", "\t"))
    }
    assert keys == {"name", "description"}


def test_skill_markdown_links_resolve() -> None:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    targets = re.findall(r"\[[^]]+\]\(([^)]+\.md)\)", text)
    assert targets
    missing = [target for target in targets if not (ROOT / target).is_file()]
    assert missing == []


def test_public_launcher_is_executable() -> None:
    launcher = ROOT / "bin/naver-place"
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK)


def test_repository_tracks_no_python_cache_artifacts() -> None:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--", str(ROOT)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    artifacts = [
        path
        for raw_path in completed.stdout.decode().split("\0")
        if raw_path
        for path in (Path(raw_path),)
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}
    ]
    assert artifacts == []


def test_requirements_are_exactly_pinned() -> None:
    for name in ("requirements.txt", "requirements-dev.txt"):
        for raw_line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "-r ")):
                continue
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", line), (name, line)


def test_launcher_without_runtime_emits_dependency_envelope(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    (skill / "bin").mkdir(parents=True)
    launcher = skill / "bin/naver-place"
    shutil.copy2(ROOT / "bin/naver-place", launcher)

    completed = subprocess.run(
        [str(launcher), "capabilities", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 3
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["status"] == "error"
    assert payload["errors"][0]["code"] == "dependency_missing"
