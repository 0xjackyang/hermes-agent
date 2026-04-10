"""OpenViking memory plugin — full bidirectional MemoryProvider interface.

Context database by Volcengine (ByteDance) that organizes agent knowledge
into a filesystem hierarchy (viking:// URIs) with tiered context loading,
automatic memory extraction, and session management.

Original PR #3369 by Mibayy, rewritten to use the full OpenViking session
lifecycle instead of read-only search endpoints.

Config via environment variables (profile-scoped via each profile's .env):
  OPENVIKING_ENDPOINT  — Server URL (default: http://127.0.0.1:1933)
  OPENVIKING_API_KEY   — API key (required for authenticated servers)
  OPENVIKING_ACCOUNT   — Tenant account (default: root)
  OPENVIKING_USER      — Tenant user (default: default)

Capabilities:
  - Automatic memory extraction on session commit (6 categories)
  - Tiered context: L0 (~100 tokens), L1 (~2k), L2 (full)
  - Semantic search with hierarchical directory retrieval
  - Filesystem-style browsing via viking:// URIs
  - Resource ingestion (URLs, docs, code)
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "http://127.0.0.1:1933"
_TIMEOUT = 30.0
_DEFAULT_OPENKB_BRIDGE_ENABLED = False
_DEFAULT_OPENKB_BRIDGE_EXPORT_ENABLED = True
_DEFAULT_OPENKB_BRIDGE_WRITEBACK_ENABLED = True
_DEFAULT_OPENKB_BRIDGE_REFRESH_SECONDS = 900
_DEFAULT_OPENKB_BRIDGE_RECALL_LIMIT = 4
_DEFAULT_OPENKB_BRIDGE_PUBLIC_URL = ""


# ---------------------------------------------------------------------------
# Process-level atexit safety net — ensures pending sessions are committed
# even if shutdown_memory_provider is never called (e.g. gateway crash,
# SIGKILL, or exception in _async_flush_memories preventing shutdown).
# ---------------------------------------------------------------------------
_last_active_provider: Optional["OpenVikingMemoryProvider"] = None


def _atexit_commit_sessions():
    """Fire on_session_end for the last active provider on process exit."""
    global _last_active_provider
    provider = _last_active_provider
    if provider is None:
        return
    _last_active_provider = None
    try:
        provider.on_session_end([])
    except Exception:
        pass  # best-effort at shutdown time


atexit.register(_atexit_commit_sessions)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _default_openkb_bridge_command() -> str:
    return str(
        Path(__file__).resolve().parents[3]
        / "optional-skills"
        / "research"
        / "openkb"
        / "scripts"
        / "openkb_bridge.py"
    )


def _trim_openkb_summary(text: str, max_chars: int = 240) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 3].rstrip() + "..."


def _format_openkb_context(items: List[Dict[str, Any]], public_url: str = "") -> str:
    if not items:
        return ""
    header = "The following OpenKB entries may be relevant."
    if public_url:
        header += f" Public reference: {public_url}"
    lines: List[str] = []
    for item in items:
        title = str(item.get("title") or item.get("slug") or "Untitled")
        summary = _trim_openkb_summary(str(item.get("summary") or item.get("path") or title))
        meta: List[str] = []
        slug = str(item.get("slug") or "").strip()
        if slug:
            meta.append(slug)
        last_updated = str(item.get("last_updated") or "").strip()
        if last_updated:
            meta.append(f"updated {last_updated}")
        score = item.get("score")
        if isinstance(score, (int, float)):
            meta.append(f"score {score:.2f}")
        if meta:
            title = f"{title} ({'; '.join(meta)})"
        lines.append(f"- {title}: {summary}")
    return "<openkb-knowledge-base>\n" + header + "\n" + "\n".join(lines) + "\n</openkb-knowledge-base>"


# ---------------------------------------------------------------------------
# HTTP helper — uses httpx to avoid requiring the openviking SDK
# ---------------------------------------------------------------------------

def _get_httpx():
    """Lazy import httpx."""
    try:
        import httpx
        return httpx
    except ImportError:
        return None


class _VikingClient:
    """Thin HTTP client for the OpenViking REST API."""

    def __init__(self, endpoint: str, api_key: str = "",
                 account: str = "", user: str = ""):
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._account = account or os.environ.get("OPENVIKING_ACCOUNT", "root")
        self._user = user or os.environ.get("OPENVIKING_USER", "default")
        self._httpx = _get_httpx()
        if self._httpx is None:
            raise ImportError("httpx is required for OpenViking: pip install httpx")

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "X-OpenViking-Account": self._account,
            "X-OpenViking-User": self._user,
        }
        if self._api_key:
            h["X-API-Key"] = self._api_key
        return h

    def _url(self, path: str) -> str:
        return f"{self._endpoint}{path}"

    def get(self, path: str, **kwargs) -> dict:
        resp = self._httpx.get(
            self._url(path), headers=self._headers(), timeout=_TIMEOUT, **kwargs
        )
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, payload: dict = None, **kwargs) -> dict:
        resp = self._httpx.post(
            self._url(path), json=payload or {}, headers=self._headers(),
            timeout=_TIMEOUT, **kwargs
        )
        resp.raise_for_status()
        return resp.json()

    def health(self) -> bool:
        try:
            resp = self._httpx.get(
                self._url("/health"), timeout=3.0
            )
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "viking_search",
    "description": (
        "Semantic search over the OpenViking knowledge base. "
        "Returns ranked results with viking:// URIs for deeper reading. "
        "Use mode='deep' for complex queries that need reasoning across "
        "multiple sources, 'fast' for simple lookups."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "mode": {
                "type": "string", "enum": ["auto", "fast", "deep"],
                "description": "Search depth (default: auto).",
            },
            "scope": {
                "type": "string",
                "description": "Viking URI prefix to scope search (e.g. 'viking://resources/docs/').",
            },
            "limit": {"type": "integer", "description": "Max results (default: 10)."},
        },
        "required": ["query"],
    },
}

READ_SCHEMA = {
    "name": "viking_read",
    "description": (
        "Read content at a viking:// URI. Three detail levels:\n"
        "  abstract — ~100 token summary (L0)\n"
        "  overview — ~2k token key points (L1)\n"
        "  full — complete content (L2)\n"
        "Start with abstract/overview, only use full when you need details."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uri": {"type": "string", "description": "viking:// URI to read."},
            "level": {
                "type": "string", "enum": ["abstract", "overview", "full"],
                "description": "Detail level (default: overview).",
            },
        },
        "required": ["uri"],
    },
}

BROWSE_SCHEMA = {
    "name": "viking_browse",
    "description": (
        "Browse the OpenViking knowledge store like a filesystem.\n"
        "  list — show directory contents\n"
        "  tree — show hierarchy\n"
        "  stat — show metadata for a URI"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string", "enum": ["tree", "list", "stat"],
                "description": "Browse action.",
            },
            "path": {
                "type": "string",
                "description": "Viking URI path (default: viking://). Examples: 'viking://resources/', 'viking://user/memories/'.",
            },
        },
        "required": ["action"],
    },
}

REMEMBER_SCHEMA = {
    "name": "viking_remember",
    "description": (
        "Explicitly store a fact or memory in the OpenViking knowledge base. "
        "Use for important information the agent should remember long-term. "
        "The system automatically categorizes and indexes the memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to remember."},
            "category": {
                "type": "string",
                "enum": ["preference", "entity", "event", "case", "pattern"],
                "description": "Memory category (default: auto-detected).",
            },
        },
        "required": ["content"],
    },
}

ADD_RESOURCE_SCHEMA = {
    "name": "viking_add_resource",
    "description": (
        "Add a URL or document to the OpenViking knowledge base. "
        "Supports web pages, GitHub repos, PDFs, markdown, code files. "
        "The system automatically parses, indexes, and generates summaries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL or path of the resource to add."},
            "reason": {
                "type": "string",
                "description": "Why this resource is relevant (improves search).",
            },
        },
        "required": ["url"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class OpenVikingMemoryProvider(MemoryProvider):
    """Full bidirectional memory via OpenViking context database."""

    def __init__(self):
        self._client: Optional[_VikingClient] = None
        self._endpoint = ""
        self._api_key = ""
        self._session_id = ""
        self._turn_count = 0
        self._sync_thread: Optional[threading.Thread] = None
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._write_thread: Optional[threading.Thread] = None
        self._openkb_bridge_enabled = False
        self._openkb_bridge_command = ""
        self._openkb_bridge_export_enabled = True
        self._openkb_bridge_writeback_enabled = True
        self._openkb_bridge_refresh_seconds = _DEFAULT_OPENKB_BRIDGE_REFRESH_SECONDS
        self._openkb_bridge_recall_limit = _DEFAULT_OPENKB_BRIDGE_RECALL_LIMIT
        self._openkb_bridge_public_url = _DEFAULT_OPENKB_BRIDGE_PUBLIC_URL
        self._openkb_last_export_at = 0.0

    @property
    def name(self) -> str:
        return "openviking"

    def is_available(self) -> bool:
        """Check if OpenViking endpoint is configured. No network calls."""
        return bool(os.environ.get("OPENVIKING_ENDPOINT"))

    def get_config_schema(self):
        return [
            {
                "key": "endpoint",
                "description": "OpenViking server URL",
                "required": True,
                "default": _DEFAULT_ENDPOINT,
                "env_var": "OPENVIKING_ENDPOINT",
            },
            {
                "key": "api_key",
                "description": "OpenViking API key",
                "secret": True,
                "env_var": "OPENVIKING_API_KEY",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._endpoint = os.environ.get("OPENVIKING_ENDPOINT", _DEFAULT_ENDPOINT)
        self._api_key = os.environ.get("OPENVIKING_API_KEY", "")
        self._session_id = session_id
        self._turn_count = 0
        self._openkb_bridge_enabled = _env_bool("OPENKB_BRIDGE_ENABLED", _DEFAULT_OPENKB_BRIDGE_ENABLED)
        self._openkb_bridge_command = (
            os.environ.get("OPENKB_BRIDGE_COMMAND", "").strip() or _default_openkb_bridge_command()
        )
        self._openkb_bridge_export_enabled = _env_bool(
            "OPENKB_BRIDGE_EXPORT_ENABLED",
            _DEFAULT_OPENKB_BRIDGE_EXPORT_ENABLED,
        )
        self._openkb_bridge_writeback_enabled = _env_bool(
            "OPENKB_BRIDGE_WRITEBACK_ENABLED",
            _DEFAULT_OPENKB_BRIDGE_WRITEBACK_ENABLED,
        )
        self._openkb_bridge_refresh_seconds = _env_int(
            "OPENKB_BRIDGE_REFRESH_SECONDS",
            _DEFAULT_OPENKB_BRIDGE_REFRESH_SECONDS,
            minimum=60,
            maximum=86_400,
        )
        self._openkb_bridge_recall_limit = _env_int(
            "OPENKB_BRIDGE_RECALL_LIMIT",
            _DEFAULT_OPENKB_BRIDGE_RECALL_LIMIT,
            minimum=1,
            maximum=12,
        )
        self._openkb_bridge_public_url = (
            os.environ.get("OPENKB_BRIDGE_PUBLIC_URL", "").strip() or _DEFAULT_OPENKB_BRIDGE_PUBLIC_URL
        )

        try:
            self._client = _VikingClient(self._endpoint, self._api_key)
            if not self._client.health():
                logger.warning("OpenViking server at %s is not reachable", self._endpoint)
                self._client = None
        except ImportError:
            logger.warning("httpx not installed — OpenViking plugin disabled")
            self._client = None

        # Register as the last active provider for atexit safety net
        global _last_active_provider
        _last_active_provider = self

        if self._openkb_bridge_enabled and not self._openkb_command_exists():
            logger.warning("OpenKB bridge enabled but command not found: %s", self._openkb_bridge_command)
            self._openkb_bridge_enabled = False
        if self._openkb_bridge_enabled and self._openkb_bridge_export_enabled:
            try:
                self._refresh_openkb_export("initialize", force=True)
            except Exception as exc:
                logger.warning("OpenKB bridge export refresh failed during initialize: %s", exc)

    def _openkb_command_parts(self) -> List[str]:
        parts = shlex.split(self._openkb_bridge_command)
        if len(parts) == 1 and parts[0].endswith(".py"):
            return [sys.executable, parts[0]]
        return parts

    def _openkb_command_exists(self) -> bool:
        parts = self._openkb_command_parts()
        if not parts:
            return False
        first = parts[0]
        if first == sys.executable:
            return len(parts) >= 2 and Path(parts[1]).exists()
        if os.path.sep in first or (os.path.altsep and os.path.altsep in first):
            return Path(first).exists()
        return shutil.which(first) is not None

    def _run_openkb_command(
        self,
        *args: str,
        input_text: str = "",
        timeout: float = _TIMEOUT,
    ) -> str:
        if not self._openkb_bridge_enabled:
            return ""
        cmd = [*self._openkb_command_parts(), *args]
        result = subprocess.run(
            cmd,
            input=input_text if input_text else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"OpenKB bridge command failed: {' '.join(cmd)}")
        return result.stdout.strip()

    def _refresh_openkb_export(self, reason: str, *, force: bool = False) -> None:
        if not self._openkb_bridge_enabled or not self._openkb_bridge_export_enabled:
            return
        now = time.monotonic()
        if not force and self._openkb_last_export_at:
            if now - self._openkb_last_export_at < self._openkb_bridge_refresh_seconds:
                return
        self._run_openkb_command(
            "bridge-export",
            timeout=max(_TIMEOUT, float(self._openkb_bridge_refresh_seconds)),
        )
        self._openkb_last_export_at = now
        logger.info("OpenKB bridge export refreshed (%s)", reason)

    def _recall_openkb(self, query: str) -> str:
        if not self._openkb_bridge_enabled or not query.strip():
            return ""
        self._refresh_openkb_export("prefetch")
        raw = self._run_openkb_command("recall", "--json", query, timeout=_TIMEOUT)
        try:
            parsed = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenKB bridge returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, list):
            return ""
        items = [item for item in parsed if isinstance(item, dict)]
        return _format_openkb_context(items[: self._openkb_bridge_recall_limit], self._openkb_bridge_public_url)

    def system_prompt_block(self) -> str:
        if not self._client:
            return ""
        # Provide brief info about the knowledge base
        try:
            # Check what's in the knowledge base via a root listing
            resp = self._client.get("/api/v1/fs/ls", params={"uri": "viking://"})
            result = resp.get("result", [])
            children = len(result) if isinstance(result, list) else 0
            if children == 0:
                return ""
            block = (
                "# OpenViking Knowledge Base\n"
                f"Active. Endpoint: {self._endpoint}\n"
                "Use viking_search to find information, viking_read for details "
                "(abstract/overview/full), viking_browse to explore.\n"
                "Use viking_remember to store facts, viking_add_resource to index URLs/docs."
            )
            if self._openkb_bridge_enabled:
                bridge_line = "\nOpenKB bridge active for explicit KB recall/writeback."
                if self._openkb_bridge_public_url:
                    bridge_line += f" Public KB: {self._openkb_bridge_public_url}"
                block += bridge_line
            return block
        except Exception:
            block = (
                "# OpenViking Knowledge Base\n"
                f"Active. Endpoint: {self._endpoint}\n"
                "Use viking_search, viking_read, viking_browse, "
                "viking_remember, viking_add_resource."
            )
            if self._openkb_bridge_enabled:
                bridge_line = "\nOpenKB bridge active for explicit KB recall/writeback."
                if self._openkb_bridge_public_url:
                    bridge_line += f" Public KB: {self._openkb_bridge_public_url}"
                block += bridge_line
            return block

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return prefetched results from the background thread."""
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return f"## OpenViking Context\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire a background search to pre-load relevant context."""
        if not self._client or not query:
            return

        def _run():
            parts = []
            try:
                client = _VikingClient(self._endpoint, self._api_key)
                resp = client.post("/api/v1/search/find", {
                    "query": query,
                    "top_k": 5,
                })
                result = resp.get("result", {})
                for ctx_type in ("memories", "resources"):
                    items = result.get(ctx_type, [])
                    for item in items[:3]:
                        uri = item.get("uri", "")
                        abstract = item.get("abstract", "")
                        score = item.get("score", 0)
                        if abstract:
                            parts.append(f"- [{score:.2f}] {abstract} ({uri})")
            except Exception as e:
                logger.debug("OpenViking prefetch failed: %s", e)
            if self._openkb_bridge_enabled:
                try:
                    openkb_context = self._recall_openkb(query)
                    if openkb_context:
                        parts.append(openkb_context)
                except Exception as e:
                    logger.debug("OpenKB bridge recall failed: %s", e)
            if parts:
                with self._prefetch_lock:
                    self._prefetch_result = "\n".join(parts)

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="openviking-prefetch"
        )
        self._prefetch_thread.start()

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Record the conversation turn in OpenViking's session (non-blocking)."""
        if not self._client:
            return

        self._turn_count += 1

        def _sync():
            try:
                client = _VikingClient(self._endpoint, self._api_key)
                sid = self._session_id

                # Add user message
                client.post(f"/api/v1/sessions/{sid}/messages", {
                    "role": "user",
                    "content": user_content[:4000],  # trim very long messages
                })
                # Add assistant message
                client.post(f"/api/v1/sessions/{sid}/messages", {
                    "role": "assistant",
                    "content": assistant_content[:4000],
                })
            except Exception as e:
                logger.debug("OpenViking sync_turn failed: %s", e)

        # Wait for any previous sync to finish before starting a new one
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(
            target=_sync, daemon=True, name="openviking-sync"
        )
        self._sync_thread.start()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Commit the session to trigger memory extraction.

        OpenViking automatically extracts 6 categories of memories:
        profile, preferences, entities, events, cases, and patterns.
        """
        if not self._client:
            return

        # Wait for any pending sync to finish first — do this before the
        # turn_count check so the last turn's messages are flushed even if
        # the count hasn't been incremented yet.
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)

        if self._turn_count == 0:
            return

        try:
            self._client.post(f"/api/v1/sessions/{self._session_id}/commit")
            logger.info("OpenViking session %s committed (%d turns)", self._session_id, self._turn_count)
        except Exception as e:
            logger.warning("OpenViking session commit failed: %s", e)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in memory writes to OpenViking as explicit memories."""
        if not self._client or action != "add" or not content:
            return

        def _write():
            try:
                client = _VikingClient(self._endpoint, self._api_key)
                # Add as a user message with memory context so the commit
                # picks it up as an explicit memory during extraction
                client.post(f"/api/v1/sessions/{self._session_id}/messages", {
                    "role": "user",
                    "parts": [
                        {"type": "text", "text": f"[Memory note — {target}] {content}"},
                    ],
                })
                if self._openkb_bridge_enabled and self._openkb_bridge_writeback_enabled:
                    payload = {
                        "title": f"OpenViking memory: {_trim_openkb_summary(content, 80)}",
                        "content": content,
                        "memory_action": action,
                        "memory_target": target,
                        "session_id": self._session_id,
                        "source": "openviking",
                    }
                    self._run_openkb_command(
                        "bridge-import-openviking",
                        "--stdin",
                        input_text=json.dumps(payload, ensure_ascii=False),
                        timeout=max(_TIMEOUT, 60.0),
                    )
            except Exception as e:
                logger.debug("OpenViking memory mirror failed: %s", e)

        self._write_thread = threading.Thread(target=_write, daemon=True, name="openviking-memwrite")
        self._write_thread.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, READ_SCHEMA, BROWSE_SCHEMA, REMEMBER_SCHEMA, ADD_RESOURCE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if not self._client:
            return tool_error("OpenViking server not connected")

        try:
            if tool_name == "viking_search":
                return self._tool_search(args)
            elif tool_name == "viking_read":
                return self._tool_read(args)
            elif tool_name == "viking_browse":
                return self._tool_browse(args)
            elif tool_name == "viking_remember":
                return self._tool_remember(args)
            elif tool_name == "viking_add_resource":
                return self._tool_add_resource(args)
            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            return tool_error(str(e))

    def shutdown(self) -> None:
        # Wait for background threads to finish
        for t in (self._sync_thread, self._prefetch_thread, self._write_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        # Clear atexit reference so it doesn't double-commit
        global _last_active_provider
        if _last_active_provider is self:
            _last_active_provider = None

    # -- Tool implementations ------------------------------------------------

    def _tool_search(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return tool_error("query is required")

        payload: Dict[str, Any] = {"query": query}
        mode = args.get("mode", "auto")
        if mode != "auto":
            payload["mode"] = mode
        if args.get("scope"):
            payload["target_uri"] = args["scope"]
        if args.get("limit"):
            payload["top_k"] = args["limit"]

        resp = self._client.post("/api/v1/search/find", payload)
        result = resp.get("result", {})

        # Format results for the model — keep it concise
        formatted = []
        for ctx_type in ("memories", "resources", "skills"):
            items = result.get(ctx_type, [])
            for item in items:
                entry = {
                    "uri": item.get("uri", ""),
                    "type": ctx_type.rstrip("s"),
                    "score": round(item.get("score", 0), 3),
                    "abstract": item.get("abstract", ""),
                }
                if item.get("relations"):
                    entry["related"] = [r.get("uri") for r in item["relations"][:3]]
                formatted.append(entry)

        return json.dumps({
            "results": formatted,
            "total": result.get("total", len(formatted)),
        }, ensure_ascii=False)

    def _tool_read(self, args: dict) -> str:
        uri = args.get("uri", "")
        if not uri:
            return tool_error("uri is required")

        level = args.get("level", "overview")
        # Map our level names to OpenViking GET endpoints
        if level == "abstract":
            resp = self._client.get("/api/v1/content/abstract", params={"uri": uri})
        elif level == "full":
            resp = self._client.get("/api/v1/content/read", params={"uri": uri})
        else:  # overview
            resp = self._client.get("/api/v1/content/overview", params={"uri": uri})

        result = resp.get("result", "")
        # result is a plain string from the content endpoints
        content = result if isinstance(result, str) else result.get("content", "")

        # Truncate very long content to avoid flooding the context
        if len(content) > 8000:
            content = content[:8000] + "\n\n[... truncated, use a more specific URI or abstract level]"

        return json.dumps({
            "uri": uri,
            "level": level,
            "content": content,
        }, ensure_ascii=False)

    def _tool_browse(self, args: dict) -> str:
        action = args.get("action", "list")
        path = args.get("path", "viking://")

        # Map action to the correct fs endpoint (all GET with uri= param)
        endpoint_map = {"tree": "/api/v1/fs/tree", "list": "/api/v1/fs/ls", "stat": "/api/v1/fs/stat"}
        endpoint = endpoint_map.get(action, "/api/v1/fs/ls")
        resp = self._client.get(endpoint, params={"uri": path})
        result = resp.get("result", {})

        # Format list/tree results for readability
        if action in ("list", "tree") and isinstance(result, list):
            entries = []
            for e in result[:50]:  # cap at 50 entries
                entries.append({
                    "name": e.get("rel_path", e.get("name", "")),
                    "uri": e.get("uri", ""),
                    "type": "dir" if e.get("isDir") else "file",
                    "abstract": e.get("abstract", ""),
                })
            return json.dumps({"path": path, "entries": entries}, ensure_ascii=False)

        return json.dumps(result, ensure_ascii=False)

    def _tool_remember(self, args: dict) -> str:
        content = args.get("content", "")
        if not content:
            return tool_error("content is required")

        # Store as a session message that will be extracted during commit.
        # The category hint helps OpenViking's extraction classify correctly.
        category = args.get("category", "")
        text = f"[Remember] {content}"
        if category:
            text = f"[Remember — {category}] {content}"

        self._client.post(f"/api/v1/sessions/{self._session_id}/messages", {
            "role": "user",
            "parts": [
                {"type": "text", "text": text},
            ],
        })

        return json.dumps({
            "status": "stored",
            "message": "Memory recorded. Will be extracted and indexed on session commit.",
        })

    def _tool_add_resource(self, args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return tool_error("url is required")

        payload: Dict[str, Any] = {"path": url}
        if args.get("reason"):
            payload["reason"] = args["reason"]

        resp = self._client.post("/api/v1/resources", payload)
        result = resp.get("result", {})

        return json.dumps({
            "status": "added",
            "root_uri": result.get("root_uri", ""),
            "message": "Resource queued for processing. Use viking_search after a moment to find it.",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register OpenViking as a memory provider plugin."""
    ctx.register_memory_provider(OpenVikingMemoryProvider())
