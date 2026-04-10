#!/usr/bin/env python3
from __future__ import annotations

import base64
import os
import shlex
import subprocess
import sys


EXEC_MODE = os.environ.get("OPENKB_EXEC_MODE", "local").strip().lower() or "local"
EXEC_HOST = os.environ.get("OPENKB_EXEC_HOST", "").strip()
EXEC_BIN = os.environ.get("OPENKB_EXEC_BIN", "openkb").strip() or "openkb"
EXEC_VENV = os.environ.get("OPENKB_EXEC_VENV_ACTIVATE", "").strip()
EXEC_KB_HOME = os.environ.get("OPENKB_EXEC_KB_HOME", "~/openkb-kb").strip() or "~/openkb-kb"
ORIENTATION_LOG_LINES = int(os.environ.get("OPENKB_ORIENTATION_LOG_LINES", "30") or "30")
EXEC_PATH_PREFIX = os.environ.get("OPENKB_EXEC_PATH_PREFIX", "$HOME/.local/bin:$HOME/.bun/bin").strip()


def _shell_path(path: str) -> str:
    if path.startswith("~/"):
        return "$HOME/" + path[2:]
    return path


def _local_command_parts() -> list[str]:
    parts = shlex.split(EXEC_BIN)
    if len(parts) == 1 and parts[0].endswith(".py"):
        return [sys.executable, parts[0]]
    return parts


def _run_local(cmd_args: list[str], stdin_data: str | None = None) -> None:
    result = subprocess.run(
        [*_local_command_parts(), *cmd_args],
        input=stdin_data,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
        raise SystemExit(result.returncode)
    sys.stdout.write(result.stdout)


def _run_ssh(cmd_args: list[str], stdin_data: str | None = None) -> None:
    if not EXEC_HOST:
        raise SystemExit("OPENKB_EXEC_HOST is required when OPENKB_EXEC_MODE=ssh")

    prologue_parts = []
    if EXEC_VENV:
        prologue_parts.append(f"source {shlex.quote(_shell_path(EXEC_VENV))} >/dev/null 2>&1 || true")
    if EXEC_PATH_PREFIX:
        prologue_parts.append(f'export PATH="{EXEC_PATH_PREFIX}:$PATH"')
    prologue = "; ".join(prologue_parts)

    if stdin_data is None:
        remote_cmd = f"{shlex.quote(_shell_path(EXEC_BIN))} {' '.join(shlex.quote(arg) for arg in cmd_args)}".strip()
        remote = f"{prologue}; {remote_cmd}" if prologue else remote_cmd
        result = subprocess.run(["ssh", EXEC_HOST, remote], capture_output=True, text=True)
    else:
        payload_b64 = base64.b64encode(stdin_data.encode("utf-8")).decode("ascii")
        pycode = (
            "import base64,os,subprocess,sys;"
            "data=base64.b64decode(sys.argv[1]);"
            "cmd=[os.path.expandvars(os.path.expanduser(x)) for x in sys.argv[2:]];"
            "r=subprocess.run(cmd,input=data,capture_output=True);"
            "sys.stdout.buffer.write(r.stdout);"
            "sys.stderr.buffer.write(r.stderr);"
            "raise SystemExit(r.returncode)"
        )
        remote_cmd = [
            "python3",
            "-c",
            pycode,
            payload_b64,
            EXEC_BIN,
            *cmd_args,
        ]
        remote = f"{prologue}; {' '.join(shlex.quote(part) for part in remote_cmd)}" if prologue else " ".join(shlex.quote(part) for part in remote_cmd)
        result = subprocess.run(["ssh", EXEC_HOST, remote], capture_output=True, text=True)

    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
        raise SystemExit(result.returncode)
    sys.stdout.write(result.stdout)


def _run(cmd_args: list[str], stdin_data: str | None = None) -> None:
    if EXEC_MODE == "ssh":
        _run_ssh(cmd_args, stdin_data=stdin_data)
        return
    _run_local(cmd_args, stdin_data=stdin_data)


def _read_orient_local() -> None:
    kb_home = os.path.expanduser(EXEC_KB_HOME)
    for label, path in (
        ("SCHEMA.md", os.path.join(kb_home, "SCHEMA.md")),
        ("index.md", os.path.join(kb_home, "index.md")),
    ):
        print(f"--- {label} ---")
        with open(path, "r", encoding="utf-8") as handle:
            print(handle.read())
    print("--- recent log.md ---")
    log_path = os.path.join(kb_home, "log.md")
    with open(log_path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    print("".join(lines[-ORIENTATION_LOG_LINES:]))


def _read_orient_ssh() -> None:
    if not EXEC_HOST:
        raise SystemExit("OPENKB_EXEC_HOST is required when OPENKB_EXEC_MODE=ssh")
    prologue_parts = []
    if EXEC_VENV:
        prologue_parts.append(f"source {shlex.quote(_shell_path(EXEC_VENV))} >/dev/null 2>&1 || true")
    if EXEC_PATH_PREFIX:
        prologue_parts.append(f'export PATH="{EXEC_PATH_PREFIX}:$PATH"')
    kb_home = shlex.quote(_shell_path(EXEC_KB_HOME))
    remote = "; ".join(
        [
            *prologue_parts,
            "echo '--- SCHEMA.md ---'",
            f"sed -n '1,220p' {kb_home}/SCHEMA.md",
            "echo '--- index.md ---'",
            f"sed -n '1,220p' {kb_home}/index.md",
            "echo '--- recent log.md ---'",
            f"tail -n {ORIENTATION_LOG_LINES} {kb_home}/log.md",
        ]
    )
    result = subprocess.run(["ssh", EXEC_HOST, remote], capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
        raise SystemExit(result.returncode)
    sys.stdout.write(result.stdout)


def read_orient() -> None:
    if EXEC_MODE == "ssh":
        _read_orient_ssh()
        return
    _read_orient_local()


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: openkb_bridge.py <command> [args]")

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "read-orient":
        read_orient()
        return
    if cmd == "recall":
        _run(["recall", *args])
        return
    if cmd == "bridge-export":
        _run(["bridge", "export-openviking", *args])
        return
    if cmd == "bridge-import-openviking":
        _run(["bridge", "import-openviking", *args], stdin_data=sys.stdin.read())
        return
    if cmd == "verify":
        _run(["verify", *args])
        return
    if cmd == "doctor":
        _run(["doctor", *args])
        return
    if cmd == "lint":
        _run(["lint", *args])
        return
    if cmd == "maintain":
        _run(["maintain", *args])
        return
    if cmd == "ingest-url":
        if not args:
            raise SystemExit("ingest-url requires a URL")
        _run(["ingest", "--url", args[0]])
        return
    if cmd == "ingest-scan":
        _run(["ingest", "--scan"])
        return
    if cmd == "ingest":
        _run(["ingest", *args])
        return
    if cmd == "file-query":
        _run(["file-query", *args], stdin_data=sys.stdin.read())
        return

    raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
