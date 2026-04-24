"""Tests for /update gateway slash command.

Tests both the _handle_update_command handler (spawns update process) and
the _send_update_notification startup hook (sends results after restart).
"""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/update", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner without calling __init__."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    return runner


# ---------------------------------------------------------------------------
# _handle_update_command
# ---------------------------------------------------------------------------


class TestHandleUpdateCommand:
    """Tests for GatewayRunner._handle_update_command."""

    @pytest.mark.asyncio
    async def test_managed_install_returns_package_manager_guidance(self, monkeypatch):
        runner = _make_runner()
        event = _make_event()
        monkeypatch.setenv("HERMES_MANAGED", "homebrew")

        result = await runner._handle_update_command(event)

        assert "managed by Homebrew" in result
        assert "brew upgrade hermes-agent" in result

    @pytest.mark.asyncio
    async def test_no_git_directory(self, tmp_path):
        """Returns an error when .git does not exist."""
        runner = _make_runner()
        event = _make_event()
        # Point _hermes_home to tmp_path and project_root to a dir without .git
        fake_root = tmp_path / "project"
        fake_root.mkdir()
        with patch("gateway.run._hermes_home", tmp_path), \
             patch("gateway.run.Path") as MockPath:
            # Path(__file__).parent.parent.resolve() -> fake_root
            MockPath.return_value = MagicMock()
            MockPath.__truediv__ = Path.__truediv__
            # Easier: just patch the __file__ resolution in the method
            pass

        # Simpler approach — mock at method level using a wrapper
        from gateway.run import GatewayRunner
        runner = _make_runner()

        with patch("gateway.run._hermes_home", tmp_path):
            # The handler does Path(__file__).parent.parent.resolve()
            # We need to make project_root / '.git' not exist.
            # Since Path(__file__) resolves to the real gateway/run.py,
            # project_root will be the real hermes-agent dir (which HAS .git).
            # Patch Path to control this.
            original_path = Path

            class FakePath(type(Path())):
                pass

            # Actually, simplest: just patch the specific file attr
            fake_file = str(fake_root / "gateway" / "run.py")
            (fake_root / "gateway").mkdir(parents=True)
            (fake_root / "gateway" / "run.py").touch()

            with patch("gateway.run.__file__", fake_file):
                result = await runner._handle_update_command(event)

        assert "Not a git repository" in result

    @pytest.mark.asyncio
    async def test_no_hermes_binary(self, tmp_path):
        """Returns error when hermes is not on PATH and hermes_cli is not importable."""
        runner = _make_runner()
        event = _make_event()

        # Create project dir WITH .git
        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")

        with patch("gateway.run._hermes_home", tmp_path), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", return_value=None), \
             patch("importlib.util.find_spec", return_value=None):
            result = await runner._handle_update_command(event)

        assert "Could not locate" in result
        assert "hermes update" in result

    @pytest.mark.asyncio
    async def test_fallback_to_sys_executable(self, tmp_path):
        """Falls back to sys.executable -m hermes_cli.main when hermes not on PATH."""
        runner = _make_runner()
        event = _make_event()

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        mock_popen = MagicMock()
        fake_spec = MagicMock()

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", return_value=None), \
             patch("importlib.util.find_spec", return_value=fake_spec), \
             patch("subprocess.Popen", mock_popen):
            result = await runner._handle_update_command(event)

        assert "Starting Hermes update" in result
        call_args = mock_popen.call_args[0][0]
        # The update_cmd uses sys.executable -m hermes_cli.main
        joined = " ".join(call_args) if isinstance(call_args, list) else call_args
        assert "hermes_cli.main" in joined or "bash" in call_args[0]

    @pytest.mark.asyncio
    async def test_resolve_hermes_bin_prefers_which(self, tmp_path):
        """_resolve_hermes_bin returns argv parts from shutil.which when available."""
        from gateway.run import _resolve_hermes_bin

        with patch("shutil.which", return_value="/custom/path/hermes"):
            result = _resolve_hermes_bin()

        assert result == ["/custom/path/hermes"]

    @pytest.mark.asyncio
    async def test_resolve_hermes_bin_fallback(self):
        """_resolve_hermes_bin falls back to sys.executable argv when which fails."""
        import sys
        from gateway.run import _resolve_hermes_bin

        fake_spec = MagicMock()
        with patch("shutil.which", return_value=None), \
             patch("importlib.util.find_spec", return_value=fake_spec):
            result = _resolve_hermes_bin()

        assert result == [sys.executable, "-m", "hermes_cli.main"]

    @pytest.mark.asyncio
    async def test_resolve_hermes_bin_returns_none_when_both_fail(self):
        """_resolve_hermes_bin returns None when both strategies fail."""
        from gateway.run import _resolve_hermes_bin

        with patch("shutil.which", return_value=None), \
             patch("importlib.util.find_spec", return_value=None):
            result = _resolve_hermes_bin()

        assert result is None

    @pytest.mark.asyncio
    async def test_writes_pending_marker(self, tmp_path):
        """Writes .update_pending.json with correct platform and chat info."""
        runner = _make_runner()
        event = _make_event(platform=Platform.TELEGRAM, chat_id="99999")

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", side_effect=lambda x: "/usr/bin/hermes" if x == "hermes" else "/usr/bin/setsid"), \
             patch("subprocess.Popen"):
            result = await runner._handle_update_command(event)

        pending_path = hermes_home / ".update_pending.json"
        assert pending_path.exists()
        data = json.loads(pending_path.read_text())
        assert data["platform"] == "telegram"
        assert data["chat_id"] == "99999"
        assert "timestamp" in data
        assert not (hermes_home / ".update_exit_code").exists()

    @pytest.mark.asyncio
    async def test_spawns_setsid(self, tmp_path):
        """Uses setsid when available."""
        runner = _make_runner()
        event = _make_event()

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        mock_popen = MagicMock()
        with patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"), \
             patch("subprocess.Popen", mock_popen):
            result = await runner._handle_update_command(event)

        # Verify setsid was used
        call_args = mock_popen.call_args[0][0]
        assert call_args[0] == "/usr/bin/setsid"
        assert call_args[1] == "bash"
        assert ".update_exit_code" in call_args[-1]
        assert "Starting Hermes update" in result

    @pytest.mark.asyncio
    async def test_fallback_when_no_setsid(self, tmp_path):
        """Falls back to start_new_session=True when setsid is not available."""
        runner = _make_runner()
        event = _make_event()

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        mock_popen = MagicMock()

        def which_no_setsid(x):
            if x == "hermes":
                return "/usr/bin/hermes"
            if x == "setsid":
                return None
            return None

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", side_effect=which_no_setsid), \
             patch("subprocess.Popen", mock_popen):
            result = await runner._handle_update_command(event)

        # Verify plain bash -c fallback (no nohup, no setsid)
        call_args = mock_popen.call_args[0][0]
        assert call_args[0] == "bash"
        assert "nohup" not in call_args[2]
        assert ".update_exit_code" in call_args[2]
        # start_new_session=True should be in kwargs
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("start_new_session") is True
        assert "Starting Hermes update" in result

    @pytest.mark.asyncio
    async def test_popen_failure_cleans_up(self, tmp_path):
        """Cleans up pending file and returns error on Popen failure."""
        runner = _make_runner()
        event = _make_event()

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"), \
             patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            result = await runner._handle_update_command(event)

        assert "Failed to start update" in result
        # Pending file should be cleaned up
        assert not (hermes_home / ".update_pending.json").exists()
        assert not (hermes_home / ".update_exit_code").exists()

    @pytest.mark.asyncio
    async def test_returns_user_friendly_message(self, tmp_path):
        """The success response is user-friendly."""
        runner = _make_runner()
        event = _make_event()

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"), \
             patch("subprocess.Popen"):
            result = await runner._handle_update_command(event)

        assert "stream progress" in result


# ---------------------------------------------------------------------------
# _send_update_notification
# ---------------------------------------------------------------------------


class TestSendUpdateNotification:
    """Tests for GatewayRunner._send_update_notification."""

    @pytest.mark.asyncio
    async def test_no_pending_file_is_noop(self, tmp_path):
        """Does nothing when no pending file exists."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        with patch("gateway.run._hermes_home", hermes_home):
            # Should not raise
            await runner._send_update_notification()

    @pytest.mark.asyncio
    async def test_defers_notification_while_update_still_running(self, tmp_path):
        """Returns False and keeps marker files when the update has not exited yet."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (hermes_home / ".update_output.txt").write_text("still running")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is False
        mock_adapter.send.assert_not_called()
        assert pending_path.exists()

    @pytest.mark.asyncio
    async def test_recovers_from_claimed_pending_file(self, tmp_path):
        """A claimed pending file from a crashed notifier is still deliverable."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        claimed_path = hermes_home / ".update_pending.claimed.json"
        claimed_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (hermes_home / ".update_output.txt").write_text("done")
        (hermes_home / ".update_exit_code").write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is True
        mock_adapter.send.assert_called_once()
        assert not claimed_path.exists()

    @pytest.mark.asyncio
    async def test_sends_notification_with_output(self, tmp_path):
        """Sends update output to the correct platform and chat."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        # Write pending marker
        pending = {
            "platform": "telegram",
            "chat_id": "67890",
            "user_id": "12345",
            "timestamp": "2026-03-04T21:00:00",
        }
        (hermes_home / ".update_pending.json").write_text(json.dumps(pending))
        (hermes_home / ".update_output.txt").write_text(
            "→ Found 3 new commit(s)\n✓ Code updated!\n✓ Update complete!"
        )
        (hermes_home / ".update_exit_code").write_text("0")

        # Mock the adapter
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._send_update_notification()

        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        assert call_args[0][0] == "67890"  # chat_id
        assert "Update complete" in call_args[0][1] or "update finished" in call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_strips_ansi_codes(self, tmp_path):
        """ANSI escape codes are removed from output."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "telegram", "chat_id": "111", "user_id": "222"}
        (hermes_home / ".update_pending.json").write_text(json.dumps(pending))
        (hermes_home / ".update_output.txt").write_text(
            "\x1b[32m✓ Code updated!\x1b[0m\n\x1b[1mDone\x1b[0m"
        )
        (hermes_home / ".update_exit_code").write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._send_update_notification()

        sent_text = mock_adapter.send.call_args[0][1]
        assert "\x1b[" not in sent_text
        assert "Code updated" in sent_text

    @pytest.mark.asyncio
    async def test_truncates_long_output(self, tmp_path):
        """Output longer than 3500 chars is truncated."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "telegram", "chat_id": "111", "user_id": "222"}
        (hermes_home / ".update_pending.json").write_text(json.dumps(pending))
        (hermes_home / ".update_output.txt").write_text("x" * 5000)
        (hermes_home / ".update_exit_code").write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._send_update_notification()

        sent_text = mock_adapter.send.call_args[0][1]
        # Should start with truncation marker
        assert "…" in sent_text
        # Total message should not be absurdly long
        assert len(sent_text) < 4500

    @pytest.mark.asyncio
    async def test_sends_failure_message_when_update_fails(self, tmp_path):
        """Non-zero exit codes produce a failure notification with captured output."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "telegram", "chat_id": "111", "user_id": "222"}
        (hermes_home / ".update_pending.json").write_text(json.dumps(pending))
        (hermes_home / ".update_output.txt").write_text("Traceback: boom")
        (hermes_home / ".update_exit_code").write_text("1")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is True
        sent_text = mock_adapter.send.call_args[0][1]
        assert "update failed" in sent_text.lower()
        assert "Traceback: boom" in sent_text

    @pytest.mark.asyncio
    async def test_sends_generic_message_when_no_output(self, tmp_path):
        """Sends a success message even if the output file is missing."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "telegram", "chat_id": "111", "user_id": "222"}
        (hermes_home / ".update_pending.json").write_text(json.dumps(pending))
        # No .update_output.txt created
        (hermes_home / ".update_exit_code").write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._send_update_notification()

        sent_text = mock_adapter.send.call_args[0][1]
        assert "finished successfully" in sent_text

    @pytest.mark.asyncio
    async def test_cleans_up_files_after_notification(self, tmp_path):
        """Both marker and output files are deleted after notification."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "111", "user_id": "222",
        }))
        output_path.write_text("✓ Done")
        exit_code_path.write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._send_update_notification()

        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_cleans_up_on_error(self, tmp_path):
        """Files are cleaned up even if notification fails."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "111", "user_id": "222",
        }))
        output_path.write_text("✓ Done")
        exit_code_path.write_text("0")

        # Adapter send raises
        mock_adapter = AsyncMock()
        mock_adapter.send.side_effect = RuntimeError("network error")
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._send_update_notification()

        # Files should still be cleaned up (finally block)
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_handles_corrupt_pending_file(self, tmp_path):
        """Gracefully handles a malformed pending JSON file."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text("{corrupt json!!")

        with patch("gateway.run._hermes_home", hermes_home):
            # Should not raise
            await runner._send_update_notification()

        # File should be cleaned up
        assert not pending_path.exists()

    @pytest.mark.asyncio
    async def test_no_adapter_for_platform(self, tmp_path):
        """Does not crash if the platform adapter is not connected."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "discord", "chat_id": "111", "user_id": "222"}
        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps(pending))
        output_path.write_text("Done")
        exit_code_path.write_text("0")

        # Only telegram adapter available, but pending says discord
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._send_update_notification()

        # send should not have been called (wrong platform)
        mock_adapter.send.assert_not_called()
        # Files should still be cleaned up
        assert not pending_path.exists()
        assert not exit_code_path.exists()


# ---------------------------------------------------------------------------
# /update in help and known_commands
# ---------------------------------------------------------------------------


class TestUpdateInHelp:
    """Verify /update appears in help text and known commands set."""

    @pytest.mark.asyncio
    async def test_update_in_help_output(self):
        """The /help output includes /update."""
        runner = _make_runner()
        event = _make_event(text="/help")
        result = await runner._handle_help_command(event)
        assert "/update" in result

    def test_update_is_known_command(self):
        """The /update command is in the help text (proxy for _known_commands)."""
        # _known_commands is local to _handle_message, so we verify by
        # checking the help output includes it.
        from gateway.run import GatewayRunner
        import inspect
        source = inspect.getsource(GatewayRunner._handle_message)
        assert '"update"' in source

# ---------------------------------------------------------------------------
# CSO-3: enable_update_command gate (Phase 2 Sub-packet D, 2026-04-24)
# ---------------------------------------------------------------------------


class TestEnableUpdateCommandGate:
    """Gate is at _handle_message dispatch (run.py) and _register_slash_commands
    (platforms/discord.py). Default True preserves upstream behavior; production
    profiles set gateway.enable_update_command: false to route code changes
    through explicit deploy workflow.

    These tests freeze the gate\'s presence so it can\'t be accidentally
    removed in future refactors; live gate behavior is verified via smoke
    test against the running gateway after deploy.
    """

    def test_gate_present_in_chat_dispatcher(self):
        """Dispatcher (gateway/run.py _handle_message) must check enable_update_command
        before routing /update to _handle_update_command."""
        import inspect
        from gateway.run import GatewayRunner
        src = inspect.getsource(GatewayRunner._handle_message)
        assert "enable_update_command" in src, "CSO-3 gate missing from _handle_message dispatch"
        assert "disabled" in src.lower() or "deploy workflow" in src, \
            "CSO-3 gate fallback message missing or changed"

    def test_gate_message_points_to_deploy_workflow(self):
        """When disabled, the gate message must direct operators to the explicit
        deploy workflow (not silently drop or show generic error)."""
        import inspect
        from gateway.run import GatewayRunner
        src = inspect.getsource(GatewayRunner._handle_message)
        # Must mention either the runtime-ops trinity entry or the deploy workflow
        assert "runtime-ops.md" in src or "source/deploy/live trinity" in src or "deploy workflow" in src, \
            "CSO-3 gate disabled message should point to deploy workflow docs"

    def test_update_commanddef_tagged_with_disable_gate(self):
        """The /update CommandDef must carry the gateway_disable_when_false
        dotpath so every gateway surface filter (telegram menu, slack
        subcommand map, gateway_help_lines) picks up the disable uniformly."""
        from hermes_cli.commands import COMMAND_REGISTRY
        update_cmd = next((c for c in COMMAND_REGISTRY if c.name == "update"), None)
        assert update_cmd is not None, "/update CommandDef missing from COMMAND_REGISTRY"
        assert update_cmd.gateway_disable_when_false == "gateway.enable_update_command", \
            f"/update must declare disable gate; got {update_cmd.gateway_disable_when_false!r}"

    def test_resolve_disabled_gates_returns_update_when_flag_false(self, tmp_path, monkeypatch):
        """When gateway.enable_update_command is explicitly False in config.yaml,
        _resolve_disabled_gates() must return {'update'}."""
        from hermes_cli.commands import _resolve_disabled_gates

        # Mock read_raw_config to return the disabled-by-flag config
        def fake_config():
            return {"gateway": {"enable_update_command": False}}

        monkeypatch.setattr("hermes_cli.config.read_raw_config", fake_config)
        assert "update" in _resolve_disabled_gates()

    def test_resolve_disabled_gates_empty_when_flag_absent(self, monkeypatch):
        """Default case (key absent) must not disable /update (upstream compat)."""
        from hermes_cli.commands import _resolve_disabled_gates

        def fake_config():
            return {"gateway": {}}

        monkeypatch.setattr("hermes_cli.config.read_raw_config", fake_config)
        assert "update" not in _resolve_disabled_gates()

    def test_resolve_disabled_gates_empty_when_flag_true(self, monkeypatch):
        """Explicit True must not disable /update (upstream compat)."""
        from hermes_cli.commands import _resolve_disabled_gates

        def fake_config():
            return {"gateway": {"enable_update_command": True}}

        monkeypatch.setattr("hermes_cli.config.read_raw_config", fake_config)
        assert "update" not in _resolve_disabled_gates()

    def test_is_gateway_available_respects_disable_gate(self, monkeypatch):
        """_is_gateway_available must return False for /update when disabled."""
        from hermes_cli.commands import COMMAND_REGISTRY, _is_gateway_available
        update_cmd = next(c for c in COMMAND_REGISTRY if c.name == "update")
        assert _is_gateway_available(update_cmd, disabled={"update"}) is False
        assert _is_gateway_available(update_cmd, disabled=set()) is True

    def test_gateway_config_has_enable_update_command_field(self):
        """GatewayConfig must have enable_update_command as a typed field
        (default True for upstream compat)."""
        from gateway.config import GatewayConfig
        cfg = GatewayConfig()
        assert hasattr(cfg, "enable_update_command"), \
            "GatewayConfig missing enable_update_command typed field"
        assert cfg.enable_update_command is True, \
            f"default must be True; got {cfg.enable_update_command!r}"

    def test_gate_present_in_discord_slash_registration(self):
        """Discord slash registration must consult _resolve_disabled_gates
        before registering /update."""
        import inspect
        from gateway.run import GatewayRunner
        discord_path = Path(inspect.getfile(GatewayRunner)).parent / "platforms" / "discord.py"
        src = discord_path.read_text()
        update_region_start = src.find('name="update"')
        assert update_region_start != -1, "discord.py /update slash command not found"
        preceding_600 = src[max(0, update_region_start - 600):update_region_start]
        # New mechanism: uses _resolve_disabled_gates rather than PlatformConfig.extra
        assert "_resolve_disabled_gates" in preceding_600, \
            "CSO-3 gate wrapper (via _resolve_disabled_gates) missing before discord.py /update slash"


    def test_load_gateway_config_reads_nested_gateway_enable_update_command(self, tmp_path, monkeypatch):
        """End-to-end: writing `gateway.enable_update_command: false` to a config.yaml
        and calling load_gateway_config() must produce a GatewayConfig with
        enable_update_command=False. This catches loader/dotpath schema drift
        (round 3 regression: top-level vs nested key).
        """
        import importlib
        import yaml as _yaml

        fake_home = tmp_path / ".hermes" / "profiles" / "test-profile"
        fake_home.mkdir(parents=True)
        config_yaml = fake_home / "config.yaml"
        config_yaml.write_text(_yaml.safe_dump({
            "gateway": {
                "enable_update_command": False,
            },
        }))

        # Redirect loader to our fake profile dir
        from gateway import config as gateway_config
        monkeypatch.setattr(gateway_config, "get_hermes_home", lambda: fake_home, raising=False)
        # Fall-back: many loaders compute home at call-time via env
        monkeypatch.setenv("HERMES_PROFILE_DIR", str(fake_home))

        cfg = gateway_config.load_gateway_config()
        assert cfg.enable_update_command is False, \
            f"loader failed to read nested gateway.enable_update_command; got {cfg.enable_update_command!r}"

    def test_load_gateway_config_default_is_true_when_key_absent(self, tmp_path, monkeypatch):
        """No gateway.enable_update_command key → default True (upstream compat)."""
        import yaml as _yaml

        fake_home = tmp_path / ".hermes" / "profiles" / "test-profile"
        fake_home.mkdir(parents=True)
        config_yaml = fake_home / "config.yaml"
        config_yaml.write_text(_yaml.safe_dump({"gateway": {}}))

        from gateway import config as gateway_config
        monkeypatch.setattr(gateway_config, "get_hermes_home", lambda: fake_home, raising=False)
        monkeypatch.setenv("HERMES_PROFILE_DIR", str(fake_home))

        cfg = gateway_config.load_gateway_config()
        assert cfg.enable_update_command is True

    def test_gateway_config_to_dict_round_trip_preserves_flag(self):
        """to_dict() must include enable_update_command so round-tripping
        (e.g. serialization for debugging, hermes dump) does not lose the flag."""
        from gateway.config import GatewayConfig
        cfg = GatewayConfig(enable_update_command=False)
        d = cfg.to_dict()
        assert "enable_update_command" in d
        assert d["enable_update_command"] is False

        # from_dict reciprocal:
        rehydrated = GatewayConfig.from_dict(d)
        assert rehydrated.enable_update_command is False
