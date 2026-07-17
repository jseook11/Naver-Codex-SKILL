from __future__ import annotations

from pathlib import Path

import pytest

from scripts import bootstrap


def write_requirements(root: Path) -> None:
    (root / "requirements.txt").write_text("requests==2.34.2\n", encoding="utf-8")
    (root / "requirements-dev.txt").write_text(
        "-r requirements.txt\npytest==9.1.1\n", encoding="utf-8"
    )


def test_ensure_runtime_is_local_and_idempotent(tmp_path: Path) -> None:
    write_requirements(tmp_path)
    commands: list[list[str]] = []

    def fake_runner(command: list[str], *, cwd: Path) -> None:
        assert cwd == tmp_path
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            python = tmp_path / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")

    python = bootstrap.ensure_runtime(
        tmp_path,
        dev=False,
        check=False,
        host_python="/host/python3",
        runner=fake_runner,
    )
    assert python == tmp_path / ".venv/bin/python"
    assert commands[0] == ["/host/python3", "-m", "venv", str(tmp_path / ".venv")]
    assert commands[1][-2:] == ["--requirement", str(tmp_path / "requirements.txt")]
    assert commands[2][-3:] == ["naver_place.cli", "capabilities", "--json"]

    commands.clear()
    bootstrap.ensure_runtime(tmp_path, dev=False, check=False, runner=fake_runner)
    assert len(commands) == 1
    assert commands[0][-3:] == ["naver_place.cli", "capabilities", "--json"]


def test_check_rejects_missing_or_stale_runtime(tmp_path: Path) -> None:
    write_requirements(tmp_path)
    with pytest.raises(RuntimeError, match="environment is missing"):
        bootstrap.ensure_runtime(tmp_path, dev=False, check=True)

    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (tmp_path / ".venv/.requirements.sha256").write_text("stale\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="dependencies are stale"):
        bootstrap.ensure_runtime(tmp_path, dev=False, check=True)


def test_dev_fingerprint_includes_runtime_and_dev_requirements(tmp_path: Path) -> None:
    write_requirements(tmp_path)
    runtime = bootstrap.fingerprint(bootstrap.requirement_paths(tmp_path, dev=False))
    development = bootstrap.fingerprint(bootstrap.requirement_paths(tmp_path, dev=True))
    assert runtime != development
