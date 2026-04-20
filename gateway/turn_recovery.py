from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, SessionStore


def build_turn_recovery_payload(
    *,
    session_id: str,
    source: SessionSource,
    event: MessageEvent,
) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "source": source.to_dict(),
        "event": {
            "text": event.text,
            "message_type": event.message_type.value
            if hasattr(event.message_type, "value")
            else str(event.message_type),
            "message_id": event.message_id,
            "media_urls": list(event.media_urls),
            "media_types": list(event.media_types),
            "reply_to_message_id": event.reply_to_message_id,
            "reply_to_text": event.reply_to_text,
            "auto_skill": event.auto_skill,
            "internal": event.internal,
            "timestamp": event.timestamp.isoformat()
            if getattr(event, "timestamp", None)
            else None,
        },
        "resume_safe": True,
        "unsafe_reason": None,
        "resume_attempts": 0,
        "created_at": datetime.now().isoformat(),
    }


def restore_recovery_event(payload: Dict[str, Any]) -> Optional[MessageEvent]:
    try:
        source = SessionSource.from_dict(payload["source"])
        event_data = payload["event"]
    except Exception:
        return None

    message_type_raw = event_data.get("message_type", MessageType.TEXT.value)
    try:
        message_type = MessageType(message_type_raw)
    except Exception:
        message_type = MessageType.TEXT

    timestamp_raw = event_data.get("timestamp")
    try:
        timestamp = datetime.fromisoformat(timestamp_raw) if timestamp_raw else datetime.now()
    except Exception:
        timestamp = datetime.now()

    return MessageEvent(
        text=event_data.get("text", ""),
        message_type=message_type,
        source=source,
        message_id=event_data.get("message_id"),
        media_urls=list(event_data.get("media_urls", [])),
        media_types=list(event_data.get("media_types", [])),
        reply_to_message_id=event_data.get("reply_to_message_id"),
        reply_to_text=event_data.get("reply_to_text"),
        auto_skill=event_data.get("auto_skill"),
        internal=bool(event_data.get("internal", False)),
        timestamp=timestamp,
    )


@dataclass
class TurnRecoveryHandle:
    session_store: SessionStore
    session_key: str
    session_id: str
    source: SessionSource
    event: MessageEvent

    @classmethod
    def from_store(
        cls,
        session_store: SessionStore,
        session_key: str,
        *,
        source: Optional[SessionSource] = None,
        session_id: Optional[str] = None,
        event: Optional[MessageEvent] = None,
        fallback_text: str = "interrupted turn",
    ) -> Optional["TurnRecoveryHandle"]:
        context = session_store.get_session_recovery_context(session_key)
        if context is None and (source is None or session_id is None):
            return None

        source = source or context.get("origin")
        session_id = session_id or context.get("session_id")
        if source is None or session_id is None:
            return None

        if event is None:
            event = MessageEvent(
                text=fallback_text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=None,
                timestamp=datetime.now(),
            )

        return cls(
            session_store=session_store,
            session_key=session_key,
            session_id=session_id,
            source=source,
            event=event,
        )

    def rebind(
        self,
        *,
        source: Optional[SessionSource] = None,
        session_id: Optional[str] = None,
        event: Optional[MessageEvent] = None,
    ) -> "TurnRecoveryHandle":
        if source is not None:
            self.source = source
        if session_id is not None:
            self.session_id = session_id
        if event is not None:
            self.event = event
        return self

    def begin(self) -> None:
        payload = build_turn_recovery_payload(
            session_id=self.session_id,
            source=self.source,
            event=self.event,
        )
        existing = self.session_store.get_pending_recovery(self.session_key)
        if existing:
            existing_event = existing.get("event") or {}
            same_turn = (
                existing.get("session_id") == self.session_id
                and existing_event.get("message_id") == self.event.message_id
                and existing_event.get("text") == self.event.text
            )
            if same_turn:
                payload["resume_attempts"] = int(existing.get("resume_attempts", 0))
                if existing.get("last_replayed_at"):
                    payload["last_replayed_at"] = existing["last_replayed_at"]
        self.session_store.set_pending_recovery(self.session_key, payload)

    def stage_followup(
        self,
        *,
        session_id: str,
        source: SessionSource,
        event: MessageEvent,
    ) -> None:
        self.rebind(session_id=session_id, source=source, event=event)
        self.begin()

    def mark_replayed(self) -> None:
        self.session_store.mark_pending_recovery_replayed(self.session_key)

    def mark_unsafe(
        self,
        *,
        recovery_kind: str,
        summary: str,
        command_preview: str = "",
    ) -> None:
        fallback = build_turn_recovery_payload(
            session_id=self.session_id,
            source=self.source,
            event=self.event,
        )
        fallback.update(
            resume_safe=False,
            unsafe_reason=recovery_kind,
            recovery_kind=recovery_kind,
            summary=summary,
            command_preview=command_preview,
        )
        self.session_store.update_pending_recovery(
            self.session_key,
            recovery=fallback,
            resume_safe=False,
            unsafe_reason=recovery_kind,
            recovery_kind=recovery_kind,
            summary=summary,
            command_preview=command_preview,
        )

    def clear(self) -> None:
        self.session_store.clear_pending_recovery(self.session_key)
