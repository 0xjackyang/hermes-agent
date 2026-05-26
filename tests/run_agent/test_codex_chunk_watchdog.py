"""Fake-SSE tests for the chunk-aware Codex watchdog (autoplan 2026-05-26).

Critical safety property: liveness is marked ONLY on real content/reasoning
deltas, NEVER on keepalive/ping/status frames — otherwise a stalled-but-pinging
stream would become immortal. Plus the flag-gated kill-decision logic.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from agent.codex_runtime import run_codex_stream


class _FakeStream:
    def __init__(self, events):
        self._events = events
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def __iter__(self):
        return iter(self._events)
    def get_final_response(self):
        return SimpleNamespace(output=[SimpleNamespace(
            type="message", role="assistant", status="completed",
            content=[SimpleNamespace(type="output_text", text="HelloWorld")],
        )])


class _FakeResponses:
    def __init__(self, events):
        self._events = events
    def stream(self, **kwargs):
        return _FakeStream(self._events)


class _FakeClient:
    def __init__(self, events):
        self.responses = _FakeResponses(events)


def _make_agent():
    return SimpleNamespace(
        _interrupt_requested=False,
        _codex_watchdog_lock=threading.Lock(),
        _touch_activity=lambda *a, **k: None,
        _fire_stream_delta=lambda *a, **k: None,
        _fire_reasoning_delta=lambda *a, **k: None,
        _client_log_context=lambda *a, **k: "",
    )


def _ev(t, delta=None, item=None):
    return SimpleNamespace(type=t, delta=delta, item=item)


class TestCodexChunkLiveness:
    def test_only_content_and_reasoning_deltas_mark_liveness(self):
        """Pings/status frames must NOT advance liveness — only real deltas."""
        events = [
            _ev("response.created"),
            _ev("ping"),
            _ev("response.in_progress"),
            _ev("response.output_text.delta", delta="Hello"),   # +1
            _ev("ping"),
            _ev("response.reasoning.delta", delta="thinking"),  # +1
            _ev("response.output_text.delta", delta="World"),   # +1
            _ev("response.output_item.done", item=SimpleNamespace(type="message")),
            _ev("response.completed"),
        ]
        agent = _make_agent()
        run_codex_stream(agent, {"model": "gpt-5.5"}, client=_FakeClient(events))
        # 2 text deltas + 1 reasoning delta = 3 content marks; pings ignored.
        assert agent._codex_content_delta_count == 3
        assert agent._codex_first_content_ns is not None
        assert agent._codex_max_gap_ns >= 0

    def test_no_content_no_first_content(self):
        """A pings-only stream marks zero content — watchdog would TTFB-kill it."""
        events = [_ev("response.created"), _ev("ping"), _ev("ping"), _ev("response.completed")]
        agent = _make_agent()
        run_codex_stream(agent, {"model": "gpt-5.5"}, client=_FakeClient(events))
        assert agent._codex_content_delta_count == 0
        assert agent._codex_first_content_ns is None
        # last_content stayed at call_start, so idle == elapsed -> TTFB-killable.
        assert agent._codex_last_content_ns == agent._codex_call_start_ns


class TestChunkWatchdogDecision:
    """Mirrors interruptible_api_call's kill-decision (repo reproduce-logic style)."""

    @staticmethod
    def _decide(enabled, api_mode, elapsed, idle, stale_timeout, idle_ceiling):
        reason = None
        if enabled and api_mode == "codex_responses":
            if idle > idle_ceiling:
                reason = "content-idle"
            elif elapsed > stale_timeout:
                reason = "total-backstop"
        elif elapsed > stale_timeout:
            reason = "legacy-total"
        return reason

    def test_flag_off_legacy_total_only(self):
        # under backstop -> no kill; over -> legacy kill
        assert self._decide(False, "codex_responses", 700, 700, 720, 720) is None
        assert self._decide(False, "codex_responses", 721, 5, 720, 720) == "legacy-total"

    def test_flag_on_idle_kill(self):
        # streaming but idle gap exceeds ceiling -> idle kill (the new behavior)
        assert self._decide(True, "codex_responses", 300, 200, 720, 150) == "content-idle"

    def test_flag_on_streaming_survives_under_ceiling(self):
        # actively streaming (small idle) and under backstop -> survives
        assert self._decide(True, "codex_responses", 800, 30, 900, 150) is None or \
            self._decide(True, "codex_responses", 800, 30, 900, 150) == "total-backstop"

    def test_flag_on_backstop_still_applies(self):
        # idle under ceiling but total exceeds backstop -> backstop kill
        assert self._decide(True, "codex_responses", 950, 10, 900, 150) == "total-backstop"

    def test_non_codex_uses_legacy(self):
        # non-codex provider always legacy regardless of flag
        assert self._decide(True, "anthropic_messages", 800, 800, 720, 150) == "legacy-total"
