import asyncio
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL
from gateway.session import SessionSource, SessionStore
from gateway.turn_recovery import TurnRecoveryHandle


class RecoveryAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SendResult(success=True, message_id="1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _config():
    return GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_type="dm",
        user_id="user-1",
        user_name="Jack",
    )


def _event(text="hello", message_id="m1"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(),
        message_id=message_id,
        timestamp=datetime.now(),
    )


def _recovery_runner(store: SessionStore) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_approvals = {}
    runner._pending_approvals_lock = threading.Lock()
    return runner


def test_session_store_persists_pending_recovery(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    entry = store.get_or_create_session(source)
    store.set_pending_recovery(
        entry.session_key,
        {
            "session_id": entry.session_id,
            "source": source.to_dict(),
            "event": {
                "text": "hello",
                "message_type": "text",
                "message_id": "m1",
                "media_urls": [],
                "media_types": [],
            },
            "resume_safe": True,
            "unsafe_reason": None,
            "resume_attempts": 0,
            "created_at": datetime.now().isoformat(),
        },
    )

    reloaded = SessionStore(sessions_dir, _config())
    recoveries = reloaded.list_pending_recoveries()
    assert len(recoveries) == 1
    assert recoveries[0]["recovery"]["event"]["text"] == "hello"


def test_turn_recovery_handle_stages_followup_resets_replay_count_for_new_turn(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    entry = store.get_or_create_session(source)
    first_event = _event("hello")
    followup_event = _event("follow up")

    handle = TurnRecoveryHandle(
        session_store=store,
        session_key=entry.session_key,
        session_id=entry.session_id,
        source=source,
        event=first_event,
    )
    handle.begin()
    handle.mark_replayed()
    handle.stage_followup(
        session_id=entry.session_id,
        source=source,
        event=followup_event,
    )

    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["event"]["text"] == "follow up"
    assert recovery["resume_attempts"] == 0


@pytest.mark.parametrize("second_text", ["repeatable prompt", "different prompt"])
def test_turn_recovery_handle_resets_replay_count_when_message_id_missing(
    tmp_path: Path,
    second_text: str,
):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    entry = store.get_or_create_session(source)
    first_event = _event("repeatable prompt", message_id=None)
    second_event = _event(second_text, message_id=None)

    handle = TurnRecoveryHandle(
        session_store=store,
        session_key=entry.session_key,
        session_id=entry.session_id,
        source=source,
        event=first_event,
    )
    handle.begin()
    handle.mark_replayed()
    handle.rebind(event=second_event)
    handle.begin()

    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["event"]["text"] == second_text
    assert recovery["event"]["message_id"] is None
    assert recovery["resume_attempts"] == 0


def test_turn_recovery_handle_preserves_replay_count_for_same_turn(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    entry = store.get_or_create_session(source)
    event = _event("resume me")

    handle = TurnRecoveryHandle(
        session_store=store,
        session_key=entry.session_key,
        session_id=entry.session_id,
        source=source,
        event=event,
    )
    handle.begin()
    handle.mark_replayed()
    handle.begin()

    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["resume_attempts"] == 1


@pytest.mark.asyncio
async def test_gateway_replays_safe_pending_turn(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    event = _event("resume me")
    entry = store.get_or_create_session(source)

    runner = _recovery_runner(store)
    adapter = RecoveryAdapter()
    adapter.handle_message = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    replay_calls = []

    async def fake_handle_message_with_agent(replay_event, replay_source, quick_key):
        replay_calls.append((replay_event, replay_source, quick_key))
        assert runner._running_agents[quick_key] is _AGENT_PENDING_SENTINEL
        return None

    runner._handle_message_with_agent = fake_handle_message_with_agent

    store.set_pending_recovery(
        entry.session_key,
        GatewayRunner._build_turn_recovery_payload(
            session_id=entry.session_id,
            source=source,
            event=event,
        ),
    )

    await runner._recover_pending_turns()

    adapter.handle_message.assert_not_called()
    assert len(replay_calls) == 1
    assert replay_calls[0][2] == entry.session_key
    assert runner._running_agents == {}
    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["resume_attempts"] == 1


@pytest.mark.asyncio
async def test_live_message_during_replay_is_queued_under_sentinel(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    replay_event = _event("resume me")
    live_event = _event("new live message")
    entry = store.get_or_create_session(source)

    runner = _recovery_runner(store)
    runner.config = _config()
    runner._running = True
    runner._pending_messages = {}
    runner._background_tasks = set()
    runner._agent_executor_tasks = set()
    runner._cron_stop_event = threading.Event()
    runner._shutdown_event = asyncio.Event()
    runner._exit_reason = None
    runner._session_model_overrides = {}
    runner._failed_platforms = {}
    runner._update_prompt_pending = {}
    runner._session_db = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner._is_user_authorized = lambda _source: True

    adapter = RecoveryAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}

    async def fake_handle_message_with_agent(replay_msg, replay_source, quick_key):
        assert runner._running_agents[quick_key] is _AGENT_PENDING_SENTINEL
        result = await GatewayRunner._handle_message(runner, live_event)
        assert result is None
        assert adapter._pending_messages[quick_key] is live_event
        return None

    runner._handle_message_with_agent = fake_handle_message_with_agent

    await runner._replay_pending_turn(entry.session_key, replay_event)

    assert runner._running_agents == {}


@pytest.mark.asyncio
async def test_gateway_skips_unsafe_pending_turn_and_notifies(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    event = _event("needs approval")
    entry = store.get_or_create_session(source)

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    adapter = RecoveryAdapter()
    adapter.handle_message = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}

    payload = GatewayRunner._build_turn_recovery_payload(
        session_id=entry.session_id,
        source=source,
        event=event,
    )
    payload["resume_safe"] = False
    payload["unsafe_reason"] = "approval_pending"
    payload["recovery_kind"] = "approval_pending"
    payload["summary"] = "Dangerous command approval was pending: recursive delete"
    payload["command_preview"] = "rm -rf /tmp/test"
    store.set_pending_recovery(entry.session_key, payload)

    await runner._recover_pending_turns()

    adapter.handle_message.assert_not_called()
    assert adapter.sent
    assert "waiting for approval" in adapter.sent[0][1]
    assert "rm -rf /tmp/test" in adapter.sent[0][1]
    assert "not auto-resumed" in adapter.sent[0][1]
    assert "verify current state" in adapter.sent[0][1]
    assert store.list_pending_recoveries() == []


@pytest.mark.asyncio
async def test_gateway_skips_tool_interrupted_turn_and_notifies(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    event = _event("run tool")
    entry = store.get_or_create_session(source)

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    adapter = RecoveryAdapter()
    adapter.handle_message = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}

    payload = GatewayRunner._build_turn_recovery_payload(
        session_id=entry.session_id,
        source=source,
        event=event,
    )
    payload["resume_safe"] = False
    payload["unsafe_reason"] = "tool_execution_interrupted"
    payload["recovery_kind"] = "tool_execution_interrupted"
    payload["summary"] = "The turn was interrupted while tool `terminal` was running."
    store.set_pending_recovery(entry.session_key, payload)

    await runner._recover_pending_turns()

    adapter.handle_message.assert_not_called()
    assert adapter.sent
    assert "tool was running" in adapter.sent[0][1]
    assert store.list_pending_recoveries() == []


def test_gateway_mark_turn_recovery_unsafe_uses_command_preview_as_fallback_text(
    tmp_path: Path,
):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    entry = store.get_or_create_session(source)
    runner = _recovery_runner(store)

    runner._mark_turn_recovery_unsafe(
        entry.session_key,
        recovery_kind="approval_pending",
        summary="Dangerous command approval was pending: delete temp files",
        command_preview="foo",
        source=source,
        session_id=entry.session_id,
    )

    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["unsafe_reason"] == "approval_pending"
    assert recovery["command_preview"] == "foo"
    assert recovery["event"]["text"] == "foo"
    assert recovery["event"]["text"] != "interrupted turn"


def test_gateway_mark_turn_recovery_unsafe_uses_summary_as_fallback_text(
    tmp_path: Path,
):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    entry = store.get_or_create_session(source)
    runner = _recovery_runner(store)
    summary = "The turn was interrupted while tool `terminal` was running."

    runner._mark_turn_recovery_unsafe(
        entry.session_key,
        recovery_kind="tool_execution_interrupted",
        summary=summary,
        source=source,
        session_id=entry.session_id,
    )

    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["unsafe_reason"] == "tool_execution_interrupted"
    assert recovery["event"]["text"] == summary


def test_gateway_remember_pending_approval_upserts_missing_recovery(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    event = _event("please delete it")
    entry = store.get_or_create_session(source)

    runner = _recovery_runner(store)
    runner._remember_pending_approval(
        entry.session_key,
        {
            "command": "rm -rf /tmp/test",
            "description": "recursive delete",
            "pattern_keys": ["rm_rf"],
        },
        source=source,
        session_id=entry.session_id,
        event=event,
    )

    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["unsafe_reason"] == "approval_pending"
    assert recovery["command_preview"] == "rm -rf /tmp/test"
    assert recovery["event"]["text"] == "please delete it"


@pytest.mark.asyncio
async def test_gateway_second_restart_exhausts_safe_replay(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    event = _event("resume me twice")
    entry = store.get_or_create_session(source)

    runner = _recovery_runner(store)
    adapter = RecoveryAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._handle_message_with_agent = AsyncMock(return_value=None)

    store.set_pending_recovery(
        entry.session_key,
        GatewayRunner._build_turn_recovery_payload(
            session_id=entry.session_id,
            source=source,
            event=event,
        ),
    )

    await runner._recover_pending_turns()
    await runner._recover_pending_turns()

    runner._handle_message_with_agent.assert_awaited_once()
    assert adapter.sent
    assert "recovery_retry_exhausted" in adapter.sent[0][1]
    assert store.list_pending_recoveries() == []


@pytest.mark.asyncio
async def test_gateway_approve_clears_approval_specific_recovery_state(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    event = _event("/approve")
    entry = store.get_or_create_session(source)

    runner = _recovery_runner(store)
    adapter = RecoveryAdapter()
    adapter.resume_typing_for_chat = MagicMock()
    runner.adapters = {Platform.TELEGRAM: adapter}

    payload = GatewayRunner._build_turn_recovery_payload(
        session_id=entry.session_id,
        source=source,
        event=_event("dangerous command"),
    )
    payload["resume_safe"] = False
    payload["unsafe_reason"] = "approval_pending"
    payload["recovery_kind"] = "approval_pending"
    payload["summary"] = "Dangerous command approval was pending: recursive delete"
    payload["command_preview"] = "rm -rf /tmp/test"
    store.set_pending_recovery(entry.session_key, payload)

    runner._pending_approvals[entry.session_key] = {
        "description": "recursive delete",
        "command_preview": "rm -rf /tmp/test",
    }

    with patch("tools.approval.has_blocking_approval", return_value=True), patch(
        "tools.approval.resolve_gateway_approval",
        return_value=1,
    ):
        result = await runner._handle_approve_command(event)

    assert "approved" in result
    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["unsafe_reason"] == "side_effect_boundary_unknown"
    assert recovery["recovery_kind"] == "side_effect_boundary_unknown"
    assert recovery["summary"].startswith("A dangerous command was approved")


@pytest.mark.asyncio
async def test_gateway_stop_marks_pending_approval_unsafe(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    event = _event("dangerous")
    entry = store.get_or_create_session(source)

    runner = object.__new__(GatewayRunner)
    runner.config = _config()
    runner.session_store = store
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._exit_reason = None
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._background_tasks = set()
    runner._agent_executor_tasks = set()
    runner._cron_stop_event = threading.Event()
    runner._shutdown_all_gateway_honcho = lambda: None
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_approvals_lock = threading.Lock()
    runner._pending_approvals[entry.session_key] = {
        "description": "recursive delete",
        "command_preview": "rm -rf /tmp/test",
    }

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["unsafe_reason"] == "approval_pending"
    assert recovery["command_preview"] == "rm -rf /tmp/test"


@pytest.mark.asyncio
async def test_gateway_stop_marks_tool_execution_unsafe(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    event = _event("run tool")
    entry = store.get_or_create_session(source)

    runner = object.__new__(GatewayRunner)
    runner.config = _config()
    runner.session_store = store
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._exit_reason = None
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._background_tasks = set()
    runner._agent_executor_tasks = set()
    runner._cron_stop_event = threading.Event()
    runner._shutdown_all_gateway_honcho = lambda: None
    runner.adapters = {}
    fake_agent = MagicMock()
    activity = {
        "current_tool": "terminal",
        "last_activity_desc": "executing tool: terminal",
    }
    fake_agent.get_activity_summary.side_effect = lambda: dict(activity)

    def clear_activity(_reason):
        activity["current_tool"] = None
        activity["last_activity_desc"] = "waiting for provider response (streaming)"

    fake_agent.interrupt.side_effect = clear_activity
    runner._running_agents = {entry.session_key: fake_agent}

    store.set_pending_recovery(
        entry.session_key,
        GatewayRunner._build_turn_recovery_payload(
            session_id=entry.session_id,
            source=source,
            event=event,
        ),
    )

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["unsafe_reason"] == "tool_execution_interrupted"
    assert "terminal" in recovery["summary"]


@pytest.mark.asyncio
async def test_gateway_stop_marks_post_tool_completion_unknown(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    event = _event("run tool")
    entry = store.get_or_create_session(source)

    runner = object.__new__(GatewayRunner)
    runner.config = _config()
    runner.session_store = store
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._exit_reason = None
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._background_tasks = set()
    runner._agent_executor_tasks = set()
    runner._cron_stop_event = threading.Event()
    runner._shutdown_all_gateway_honcho = lambda: None
    runner.adapters = {}
    runner._running_agents_ts = {}
    runner._pending_approvals_lock = threading.Lock()
    fake_agent = MagicMock()
    fake_agent.get_activity_summary.return_value = {
        "current_tool": None,
        "last_activity_desc": "tool completed: write_file (0.2s)",
    }
    runner._running_agents = {entry.session_key: fake_agent}

    store.set_pending_recovery(
        entry.session_key,
        GatewayRunner._build_turn_recovery_payload(
            session_id=entry.session_id,
            source=source,
            event=event,
        ),
    )

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["unsafe_reason"] == "side_effect_boundary_unknown"
    assert "tool completion" in recovery["summary"]


@pytest.mark.asyncio
async def test_reconnect_watcher_retries_pending_recovery_for_reconnected_platform(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    entry = store.get_or_create_session(source)

    runner = _recovery_runner(store)
    runner._running = True
    runner.config = _config()
    runner.delivery_router = MagicMock()
    runner.delivery_router.adapters = {}
    runner._failed_platforms = {
        Platform.TELEGRAM: {
            "config": PlatformConfig(enabled=True, token="***"),
            "attempts": 0,
            "next_retry": 0,
        }
    }
    runner._create_adapter = MagicMock(return_value=RecoveryAdapter())
    runner._sync_voice_mode_state_to_adapter = lambda _adapter: None
    runner._handle_message = AsyncMock()
    runner._handle_adapter_fatal_error = MagicMock()

    async def fake_recover_pending_turns(platforms=None):
        runner._running = False

    runner._recover_pending_turns = AsyncMock(side_effect=fake_recover_pending_turns)

    async def fast_sleep(_seconds):
        return None

    with patch("asyncio.sleep", new=fast_sleep), patch(
        "gateway.channel_directory.build_channel_directory"
    ):
        await GatewayRunner._platform_reconnect_watcher(runner)

    runner._recover_pending_turns.assert_awaited_once_with(platforms={Platform.TELEGRAM})
    assert Platform.TELEGRAM in runner.adapters
    assert runner.delivery_router.adapters == runner.adapters
