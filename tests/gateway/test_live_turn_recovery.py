from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore


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


def _event(text="hello"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="m1",
        timestamp=datetime.now(),
    )


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


@pytest.mark.asyncio
async def test_gateway_replays_safe_pending_turn(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir, _config())
    source = _source()
    event = _event("resume me")
    entry = store.get_or_create_session(source)

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    adapter = RecoveryAdapter()
    adapter.handle_message = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}

    store.set_pending_recovery(
        entry.session_key,
        GatewayRunner._build_turn_recovery_payload(
            session_id=entry.session_id,
            source=source,
            event=event,
        ),
    )

    await runner._recover_pending_turns()

    adapter.handle_message.assert_awaited_once()
    recovery = store.list_pending_recoveries()[0]["recovery"]
    assert recovery["resume_attempts"] == 1


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
    store.set_pending_recovery(entry.session_key, payload)

    await runner._recover_pending_turns()

    adapter.handle_message.assert_not_called()
    assert adapter.sent
    assert "approval_pending" in adapter.sent[0][1]
    assert store.list_pending_recoveries() == []
