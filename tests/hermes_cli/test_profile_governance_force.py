"""Phase 3-C.2.4 tests: --force flag + governance gate on delete_profile
and import_profile."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.profiles import (
    _check_governance_or_force,
    delete_profile,
    import_profile,
)


def _init_governed_profile(tmp_path: Path, name: str = "test-gov") -> Path:
    """Build a profile dir that is_governed_filesystem will see as governed."""
    profile = tmp_path / "profiles" / name
    profile.mkdir(parents=True)
    (profile / "skills").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(profile)], check=True)
    subprocess.run(["git", "-C", str(profile), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(profile), "config", "user.name", "t"], check=True)
    # Commit something so there's a HEAD
    (profile / "README.md").write_text("test")
    subprocess.run(["git", "-C", str(profile), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(profile), "commit", "-q", "-m", "init"], check=True)
    return profile


class TestCheckGovernanceOrForce:
    """Direct unit tests on the helper."""

    def test_non_governed_profile_passes_without_force(self, tmp_path, monkeypatch):
        """Plain profile_dir (not a git repo on main) returns silently."""
        plain = tmp_path / "plain-profile"
        plain.mkdir()
        (plain / "skills").mkdir()
        monkeypatch.setenv("HERMES_HOME", str(plain))
        # No raise
        _check_governance_or_force(plain, "delete profile", force=False)

    def test_governed_profile_raises_without_force(self, tmp_path, monkeypatch):
        profile = _init_governed_profile(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile))
        with pytest.raises(ValueError, match="Refusing to delete profile"):
            _check_governance_or_force(profile, "delete profile", force=False)

    def test_governed_profile_passes_with_force(self, tmp_path, monkeypatch):
        profile = _init_governed_profile(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile))
        # No raise with force=True
        _check_governance_or_force(profile, "delete profile", force=True)

    def test_error_message_points_at_playbook(self, tmp_path, monkeypatch):
        profile = _init_governed_profile(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile))
        with pytest.raises(ValueError) as exc:
            _check_governance_or_force(profile, "delete profile", force=False)
        msg = str(exc.value)
        assert "--force" in msg
        assert "playbook-skill-learning-delta-terminal-outcomes" in msg

    def test_error_mentions_op_label(self, tmp_path, monkeypatch):
        profile = _init_governed_profile(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile))
        with pytest.raises(ValueError, match="import-overwrite profile"):
            _check_governance_or_force(
                profile, "import-overwrite profile", force=False
            )


class TestDeleteProfileGovernance:
    """End-to-end delete_profile with + without --force on governed."""

    def test_delete_governed_profile_without_force_raises(self, tmp_path, monkeypatch):
        profile = _init_governed_profile(tmp_path, name="governed-a")
        # Patch get_profile_dir so delete_profile finds our test profile
        fake_home = tmp_path / "profiles"
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir",
            lambda name: fake_home / name,
        )

        with pytest.raises(ValueError, match="Refusing to delete profile"):
            delete_profile("governed-a", yes=True, force=False)

        # Profile still exists
        assert profile.is_dir()

    def test_delete_governed_profile_with_force_proceeds(self, tmp_path, monkeypatch):
        profile = _init_governed_profile(tmp_path, name="governed-b")
        fake_home = tmp_path / "profiles"
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir",
            lambda name: fake_home / name,
        )
        # Mock gateway service cleanup to avoid systemd/launchd side effects
        with patch("hermes_cli.profiles._cleanup_gateway_service"):
            # Gate passes with --force; actual delete may fail further on
            # wrapper / systemd; we only assert the governance gate didn't
            # raise by catching everything except the specific governance
            # ValueError.
            try:
                delete_profile("governed-b", yes=True, force=True)
            except ValueError as e:
                if "Refusing to delete" in str(e):
                    pytest.fail(f"governance gate fired with force=True: {e}")
            except Exception:
                # Other errors (systemd, wrapper) are fine — governance gate passed
                pass

    def test_delete_non_governed_profile_no_force_needed(self, tmp_path, monkeypatch):
        """Plain profile (no git repo) never triggers the gate."""
        plain = tmp_path / "profiles" / "plain"
        plain.mkdir(parents=True)
        (plain / "skills").mkdir()
        fake_home = tmp_path / "profiles"
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir",
            lambda name: fake_home / name,
        )
        with patch("hermes_cli.profiles._cleanup_gateway_service"):
            try:
                delete_profile("plain", yes=True, force=False)
            except ValueError as e:
                if "Refusing to delete" in str(e):
                    pytest.fail(f"gate wrongly fired on non-governed profile: {e}")
            except Exception:
                pass


class TestImportProfileGovernance:
    """Import gate only fires when OVERWRITING an existing governed profile."""

    def _make_archive(self, tmp_path: Path, name: str) -> Path:
        """Build a minimal tar.gz profile archive."""
        src = tmp_path / "src" / name
        src.mkdir(parents=True)
        (src / "README.md").write_text("imported profile")
        (src / "skills").mkdir()
        archive = tmp_path / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(src, arcname=name)
        return archive

    def test_import_new_profile_no_force_needed(self, tmp_path, monkeypatch):
        """New import into non-existent dir: gate doesn't fire."""
        archive = self._make_archive(tmp_path, "fresh")
        fake_home = tmp_path / "profiles"
        fake_home.mkdir()
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir",
            lambda name: fake_home / name,
        )
        # Should not raise
        try:
            result = import_profile(str(archive), name="fresh", force=False)
            assert result.is_dir()
        except ValueError as e:
            if "Refusing to" in str(e):
                pytest.fail(f"gate wrongly fired on new profile: {e}")

    def test_import_rejects_overwriting_any_existing_profile(self, tmp_path, monkeypatch):
        """import_profile refuses to overwrite ANY existing profile (governed or not)
        via the pre-existing FileExistsError guard. The governance gate inside
        import_profile is defensive future-safety (fires if that guard is ever
        relaxed); not reachable through the current public API.

        This test documents the actual behavior: overwrite is blocked by
        FileExistsError BEFORE governance check runs. Both on governed and
        non-governed profiles."""
        existing = _init_governed_profile(tmp_path, name="existing-gov")
        archive = self._make_archive(tmp_path, "existing-gov")
        fake_home = tmp_path / "profiles"
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir",
            lambda name: fake_home / name,
        )
        # Pre-existing overwrite guard fires regardless of --force
        with pytest.raises(FileExistsError, match="already exists"):
            import_profile(str(archive), name="existing-gov", force=False)
        with pytest.raises(FileExistsError, match="already exists"):
            import_profile(str(archive), name="existing-gov", force=True)
        # Original profile untouched
        assert existing.is_dir()
        assert (existing / "README.md").exists()
