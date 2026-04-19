from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_SPARK_EXECUTOR = Path("/home/jackyujieyang/spark-ops/bin/guarded_gateway_rollout.py")


def resolve_bounded_gateway_rollout_executor() -> str:
    configured = os.getenv("HERMES_GATEWAY_ROLLOUT_EXECUTOR", "").strip()
    if configured:
        return configured
    if os.getenv("PYTEST_CURRENT_TEST"):
        return ""
    if DEFAULT_SPARK_EXECUTOR.exists():
        return str(DEFAULT_SPARK_EXECUTOR)
    return ""


def run_bounded_gateway_rollout(
    unit: str,
    *,
    scope: str = "user",
    reason: str = "hermes update",
    staged: bool = False,
    timeout: int = 300,
) -> dict[str, Any]:
    executor = resolve_bounded_gateway_rollout_executor()
    if not executor:
        return {
            "available": False,
            "ok": False,
            "command": [],
            "summary": "bounded gateway rollout executor not configured",
            "stdout": "",
            "stderr": "",
            "returncode": None,
        }

    command = [executor, "--service", unit, "--scope", scope, "--reason", reason]
    if staged:
        command.append("--staged")

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "available": True,
            "ok": False,
            "command": command,
            "summary": f"bounded gateway rollout timed out after {timeout}s",
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "returncode": None,
        }

    summary = (proc.stdout or "").strip().splitlines()
    summary_line = summary[-1] if summary else (proc.stderr or "").strip()
    return {
        "available": True,
        "ok": proc.returncode == 0,
        "command": command,
        "summary": summary_line,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "returncode": proc.returncode,
    }
