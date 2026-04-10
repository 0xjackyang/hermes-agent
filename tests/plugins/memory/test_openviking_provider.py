import json
import subprocess

import pytest

from plugins.memory.openviking import OpenVikingMemoryProvider, _format_openkb_context


class FakeClient:
    def __init__(self, endpoint: str, api_key: str = "", account: str = "", user: str = ""):
        self.endpoint = endpoint
        self.api_key = api_key
        self.account = account
        self.user = user
        self.posts = []

    def health(self) -> bool:
        return True

    def get(self, path: str, **kwargs):
        if path == "/api/v1/fs/ls":
            return {"result": [{"uri": "viking://resources/"}]}
        return {"result": {}}

    def post(self, path: str, payload: dict = None, **kwargs):
        self.posts.append({"path": path, "payload": payload or {}})
        if path == "/api/v1/search/find":
            return {
                "result": {
                    "memories": [
                        {
                            "uri": "viking://user/memories/rowboat",
                            "abstract": "Rowboat is part of the active research context.",
                            "score": 0.91,
                        }
                    ],
                    "resources": [],
                }
            }
        return {"result": {}}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENVIKING_ENDPOINT", "http://127.0.0.1:1933")
    monkeypatch.setenv("OPENKB_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("OPENKB_BRIDGE_COMMAND", "python3")
    monkeypatch.setenv("OPENKB_BRIDGE_EXPORT_ENABLED", "1")
    monkeypatch.setenv("OPENKB_BRIDGE_WRITEBACK_ENABLED", "1")
    monkeypatch.setenv("OPENKB_BRIDGE_PUBLIC_URL", "https://kb.jackyang.com")
    monkeypatch.setattr("plugins.memory.openviking._VikingClient", FakeClient)

    calls = []

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        calls.append({"cmd": cmd, "input": input, "timeout": timeout})
        if "recall" in cmd:
            stdout = json.dumps(
                [
                    {
                        "title": "Rowboat",
                        "slug": "rowboat",
                        "summary": "A durable OpenKB concept about the rowboat lane.",
                        "score": 0.88,
                        "last_updated": "2026-04-10",
                    }
                ]
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("plugins.memory.openviking.subprocess.run", fake_run)

    p = OpenVikingMemoryProvider()
    p.initialize("session-1", hermes_home="/tmp/hermes", platform="cli")
    p._openkb_last_export_at = 0.0
    return p, calls


def test_format_openkb_context_includes_public_reference():
    block = _format_openkb_context(
        [
            {
                "title": "OpenKB",
                "slug": "openkb",
                "summary": "Compiled markdown knowledge base.",
                "score": 0.77,
                "last_updated": "2026-04-10",
            }
        ],
        "https://kb.jackyang.com",
    )
    assert "<openkb-knowledge-base>" in block
    assert "https://kb.jackyang.com" in block
    assert "OpenKB" in block


def test_prefetch_merges_openviking_and_openkb(provider):
    p, calls = provider
    p.queue_prefetch("rowboat")
    assert p._prefetch_thread is not None
    p._prefetch_thread.join(timeout=1)
    result = p.prefetch("rowboat")
    assert "## OpenViking Context" in result
    assert "Rowboat is part of the active research context." in result
    assert "<openkb-knowledge-base>" in result
    assert "Rowboat" in result
    assert any("bridge-export" in call["cmd"] for call in calls)
    assert any("recall" in call["cmd"] for call in calls)


def test_on_memory_write_mirrors_to_openkb(provider):
    p, calls = provider
    p.on_memory_write("add", "memory", "Durable fact for OpenKB")
    assert p._write_thread is not None
    p._write_thread.join(timeout=1)

    bridge_calls = [call for call in calls if "bridge-import-openviking" in call["cmd"]]
    assert bridge_calls, "expected bridge-import-openviking call"
    payload = json.loads(bridge_calls[0]["input"])
    assert payload["memory_action"] == "add"
    assert payload["memory_target"] == "memory"
    assert payload["content"] == "Durable fact for OpenKB"
    assert payload["session_id"] == "session-1"
