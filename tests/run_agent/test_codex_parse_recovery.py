"""run_codex_stream recovers when the SDK's get_final_response() throws.

2026-05-27 outage: the ChatGPT-Codex backend changed its streamed-event
shape, making the OpenAI SDK's get_final_response() raise
`TypeError: 'NoneType' object is not iterable`. hermes only caught httpx
transport errors there, so every gpt-5.5 call died as a non-retryable
client error. run_codex_stream already captures output items + text deltas
mid-stream; it must rebuild the response from those instead of dying — and
must NOT fabricate a turn when nothing was captured.
"""
import threading
from types import SimpleNamespace

import pytest

from agent.codex_runtime import run_codex_stream


class _ThrowingStream:
    """Yields events but get_final_response() raises — mimics the SDK
    choking on a changed backend response shape."""

    def __init__(self, events, exc):
        self._events = events
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_response(self):
        raise self._exc


class _Responses:
    def __init__(self, stream_obj):
        self._stream_obj = stream_obj

    def stream(self, **kwargs):
        return self._stream_obj


class _Client:
    def __init__(self, stream_obj):
        self.responses = _Responses(stream_obj)


def _make_agent():
    return SimpleNamespace(
        _interrupt_requested=False,
        _codex_watchdog_lock=threading.Lock(),
        _touch_activity=lambda *a, **k: None,
        _fire_stream_delta=lambda *a, **k: None,
        _fire_reasoning_delta=lambda *a, **k: None,
        _client_log_context=lambda *a, **k: "",
    )


def _ev(t, **kw):
    return SimpleNamespace(type=t, **kw)


_NONE_ITER = TypeError("'NoneType' object is not iterable")


class TestCodexParseRecovery:
    def test_recovers_from_collected_output_items(self):
        item = SimpleNamespace(
            type="function_call", name="do_thing", arguments="{}",
            call_id="call_1", id="fc_1", status="completed",
        )
        events = [
            _ev("response.created"),
            _ev("response.output_item.done", item=item),
            _ev("response.completed"),
        ]
        agent = _make_agent()
        resp = run_codex_stream(
            agent, {"model": "gpt-5.5"},
            client=_Client(_ThrowingStream(events, _NONE_ITER)),
        )
        assert resp is not None
        assert resp.output == [item]  # rebuilt from collected items, not crashed

    def test_recovers_from_text_deltas(self):
        events = [
            _ev("response.created"),
            _ev("response.output_text.delta", delta="Hello "),
            _ev("response.output_text.delta", delta="world"),
            _ev("response.completed"),
        ]
        agent = _make_agent()
        resp = run_codex_stream(
            agent, {"model": "gpt-5.5"},
            client=_Client(_ThrowingStream(events, _NONE_ITER)),
        )
        assert resp is not None
        assert resp.output and resp.output[0].content[0].text == "Hello world"

    def test_reraises_when_nothing_recoverable(self):
        # No output items and no text deltas -> do not fabricate; re-raise.
        events = [_ev("response.created"), _ev("ping"), _ev("response.completed")]
        agent = _make_agent()
        with pytest.raises(TypeError):
            run_codex_stream(
                agent, {"model": "gpt-5.5"},
                client=_Client(_ThrowingStream(events, _NONE_ITER)),
            )
