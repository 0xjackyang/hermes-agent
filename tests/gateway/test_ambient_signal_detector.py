from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
import hermes_cli.tools_config as tools_config


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._background_tasks = set()
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._load_reasoning_config = lambda: {}
    runner._resolve_turn_agent_config = lambda prompt, model, runtime: {
        "model": model or "test-model",
        "runtime": runtime,
    }
    return runner


def _make_source(platform=Platform.DISCORD):
    return SimpleNamespace(
        platform=platform,
        chat_type="group",
        chat_id="chat-123",
        thread_id="thread-456",
        user_id="user-789",
        user_name="Jack",
    )


def _install_gbrain_skills(tmp_path):
    signal_dir = tmp_path / "skills" / "gbrain" / "signal-detector"
    signal_dir.mkdir(parents=True)
    (signal_dir / "SKILL.md").write_text("# signal-detector\n")

    brain_ops_dir = tmp_path / "skills" / "gbrain" / "brain-ops"
    brain_ops_dir.mkdir(parents=True)
    (brain_ops_dir / "SKILL.md").write_text("# brain-ops\n")


def test_ambient_signal_detector_enabled_requires_discord_gbrain_and_skills(tmp_path, monkeypatch):
    runner = _make_runner()
    _install_gbrain_skills(tmp_path)

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda config, platform: {"gbrain", "skills"},
    )

    assert runner._ambient_signal_detector_enabled(_make_source()) is True
    assert runner._ambient_signal_detector_enabled(_make_source(platform=Platform.SLACK)) is False


def test_operational_turn_detection_strips_sender_prefix():
    runner = _make_runner()

    assert runner._is_purely_operational_signal_turn("[Jack] thanks") is True
    assert runner._is_purely_operational_signal_turn("[Jack] ✅") is True
    assert (
        runner._is_purely_operational_signal_turn(
            "[Jack] We should split utilization into uptime and absorption before more SQL."
        )
        is False
    )


def test_build_ambient_signal_prompt_includes_metadata_history_and_current_turn():
    runner = _make_runner()
    source = _make_source()
    history = [
        {"role": "user", "content": "Earlier idea about attribution."},
        {"role": "assistant", "content": "Previous response summary."},
        {"role": "tool", "content": "Ignored tool output."},
    ]

    prompt = runner._build_ambient_signal_prompt(
        message="Current message with a new thesis.",
        history=history,
        source=source,
        session_id="session-1",
        session_key="agent:main:discord:group:thread-456",
    )

    assert "signal-detector" in prompt
    assert "brain-ops" in prompt
    assert "Earlier idea about attribution." in prompt
    assert "Previous response summary." in prompt
    assert "Current message with a new thesis." in prompt
    assert "session_id: session-1" in prompt
    assert "thread_id: thread-456" in prompt


@pytest.mark.asyncio
async def test_schedule_ambient_signal_detector_creates_background_task(monkeypatch):
    runner = _make_runner()
    runner._ambient_signal_detector_enabled = lambda source: True
    runner._is_purely_operational_signal_turn = lambda message: False
    runner._build_ambient_signal_prompt = lambda **kwargs: "prompt"
    runner._run_ambient_signal_detector = AsyncMock()

    created = []

    class DummyTask:
        def __init__(self, coro):
            self.coro = coro
            self.callbacks = []

        def add_done_callback(self, cb):
            self.callbacks.append(cb)

    def fake_create_task(coro):
        created.append(coro)
        coro.close()
        return DummyTask(coro)

    monkeypatch.setattr(gateway_run.asyncio, "create_task", fake_create_task)

    runner._schedule_ambient_signal_detector(
        message="This is a substantive turn.",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key="session-key",
    )

    assert len(created) == 1
    assert len(runner._background_tasks) == 1


@pytest.mark.asyncio
async def test_schedule_ambient_signal_detector_skips_operational_turn(monkeypatch):
    runner = _make_runner()
    runner._ambient_signal_detector_enabled = lambda source: True
    runner._is_purely_operational_signal_turn = lambda message: True

    def fail_create_task(_coro):
        raise AssertionError("ambient task should not be created for operational turns")

    monkeypatch.setattr(gateway_run.asyncio, "create_task", fail_create_task)

    runner._schedule_ambient_signal_detector(
        message="ok",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key="session-key",
    )

    assert runner._background_tasks == set()
