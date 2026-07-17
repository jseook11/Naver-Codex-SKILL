#!/usr/bin/env python3
"""Create the isolated runtime used by this skill.

The bootstrap is intentionally idempotent and never installs into the system
Python environment. Run with ``--dev`` to include the test runner.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
from typing import Callable


MINIMUM_PYTHON = (3, 10)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap the search-naver-map skill runtime.")
    parser.add_argument("--dev", action="store_true", help="Install development and test dependencies.")
    parser.add_argument("--check", action="store_true", help="Verify an existing environment without changing it.")
    return parser.parse_args(argv)


def fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


Runner = Callable[..., None]


def run(command: list[str], *, cwd: Path) -> None:
    quiet = command[-3:] == ["naver_place.cli", "capabilities", "--json"]
    subprocess.run(
        command,
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL if quiet else None,
    )


def requirement_paths(root: Path, *, dev: bool) -> list[Path]:
    paths = [root / "requirements.txt"]
    if dev:
        paths.append(root / "requirements-dev.txt")
    return paths


def ensure_runtime(
    root: Path,
    *,
    dev: bool,
    check: bool,
    host_python: str = sys.executable,
    runner: Runner = run,
) -> Path:
    """Create or verify the local runtime and return its Python executable.

    ``runner`` and ``root`` are injectable so installation behavior can be
    tested without changing the developer's real environment.
    """

    venv_dir = root / ".venv"
    venv_python = venv_dir / "bin" / "python"
    requirements = requirement_paths(root, dev=dev)
    expected = fingerprint(requirements)
    stamp = venv_dir / ".requirements.sha256"

    if check:
        if not venv_python.exists():
            raise RuntimeError("skill environment is missing; run scripts/bootstrap.py")
        if not stamp.exists() or stamp.read_text(encoding="utf-8").strip() != expected:
            raise RuntimeError("skill dependencies are stale; rerun scripts/bootstrap.py")
    else:
        if not venv_python.exists():
            runner([host_python, "-m", "venv", str(venv_dir)], cwd=root)
        if not venv_python.exists():
            raise RuntimeError("virtual environment creation did not produce .venv/bin/python")
        if not stamp.exists() or stamp.read_text(encoding="utf-8").strip() != expected:
            runner(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--requirement",
                    str(requirements[-1]),
                ],
                cwd=root,
            )
            stamp.write_text(expected + "\n", encoding="utf-8")

    runner([str(venv_python), "-m", "naver_place.cli", "capabilities", "--json"], cwd=root)
    return venv_python


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if sys.version_info < MINIMUM_PYTHON:
        print(
            f"error: Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}+ is required; "
            f"found {sys.version_info.major}.{sys.version_info.minor}",
            file=sys.stderr,
        )
        return 3

    root = Path(__file__).resolve().parents[1]
    try:
        venv_python = ensure_runtime(root, dev=args.dev, check=args.check)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    print(f"ready: {venv_python}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
