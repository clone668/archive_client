from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


TASK_NAME = "SMSI Archive Client Daily Sync"


def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    if os.name != "nt":
        raise RuntimeError("自动计划仅支持 Windows")
    try:
        return subprocess.run(
            ["schtasks.exe", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            creationflags=_creation_flags(),
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "计划任务操作失败").strip()
        raise RuntimeError(detail) from exc


def scheduled_command(entrypoint: Path | None = None) -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" --run-once --all-profiles'
    script = (entrypoint or Path(__file__).resolve().parents[1] / "main.py").resolve()
    python = Path(sys.executable).resolve()
    pythonw = python.with_name("pythonw.exe")
    executable = pythonw if pythonw.is_file() else python
    return f'"{executable}" "{script}" --run-once --all-profiles'


def install_task(entrypoint: Path | None = None) -> None:
    _run(
        [
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            scheduled_command(entrypoint),
            "/SC",
            "MINUTE",
            "/MO",
            "30",
            "/F",
        ]
    )


def remove_task() -> None:
    _run(["/Delete", "/TN", TASK_NAME, "/F"])


def task_installed() -> bool:
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", TASK_NAME],
        capture_output=True,
        creationflags=_creation_flags(),
    )
    return result.returncode == 0
