from __future__ import annotations

import subprocess
from pathlib import Path

from hermes_cli import gateway_rollout


def test_resolve_executor_prefers_env(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_ROLLOUT_EXECUTOR", "/tmp/custom-rollout.py")
    monkeypatch.setattr(gateway_rollout, "DEFAULT_SPARK_EXECUTOR", Path("/definitely/missing"))
    assert gateway_rollout.resolve_bounded_gateway_rollout_executor() == "/tmp/custom-rollout.py"


def test_run_bounded_gateway_rollout_returns_unavailable_without_executor(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_ROLLOUT_EXECUTOR", raising=False)
    monkeypatch.setattr(gateway_rollout, "DEFAULT_SPARK_EXECUTOR", Path("/definitely/missing"))

    result = gateway_rollout.run_bounded_gateway_rollout(
        "hermes-gateway-satori-hermes.service",
        scope="user",
        reason="unit test",
        staged=True,
    )

    assert result["available"] is False
    assert result["ok"] is False
    assert result["command"] == []


def test_run_bounded_gateway_rollout_invokes_executor(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_ROLLOUT_EXECUTOR", "/tmp/custom-rollout.py")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok=true\nsummary=bounded restart verified\n", stderr="")

    monkeypatch.setattr(gateway_rollout.subprocess, "run", fake_run)

    result = gateway_rollout.run_bounded_gateway_rollout(
        "hermes-gateway-satori-hermes.service",
        scope="user",
        reason="hermes update",
        staged=True,
        timeout=42,
    )

    assert result["available"] is True
    assert result["ok"] is True
    assert calls[0][0] == [
        "/tmp/custom-rollout.py",
        "--service",
        "hermes-gateway-satori-hermes.service",
        "--scope",
        "user",
        "--reason",
        "hermes update",
        "--staged",
    ]
    assert calls[0][1]["timeout"] == 42
    assert "bounded restart verified" in result["stdout"]
