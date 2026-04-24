"""Tests for agent.skill_surface (Phase 3-A, 2026-04-24)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent import skill_surface


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


class TestRootResolution:
    def test_runtime_local_skill_root_tracks_hermes_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert skill_surface.runtime_local_skill_root() == tmp_path / "skills"

    def test_hub_root_is_child_of_runtime_local(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert skill_surface.hub_root() == tmp_path / "skills" / ".hub"

    def test_runtime_local_resolves_per_call_not_cached(self, tmp_path, monkeypatch):
        """Subprocess-respawn patterns (profiles.py::seed_profile_skills) rely
        on per-call HERMES_HOME resolution. A module-level cache would break
        that workaround."""
        home_a = tmp_path / "a"
        home_b = tmp_path / "b"
        home_a.mkdir()
        home_b.mkdir()

        monkeypatch.setenv("HERMES_HOME", str(home_a))
        assert skill_surface.runtime_local_skill_root() == home_a / "skills"

        monkeypatch.setenv("HERMES_HOME", str(home_b))
        assert skill_surface.runtime_local_skill_root() == home_b / "skills"

    def test_all_read_roots_puts_runtime_local_first(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        roots = skill_surface.all_read_roots()
        assert roots[0] == tmp_path / "skills", \
            f"runtime-local must be first; got {roots!r}"

    def test_external_skill_roots_is_list(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert isinstance(skill_surface.external_skill_roots(), list)


# ---------------------------------------------------------------------------
# Operator-intent write resolution
# ---------------------------------------------------------------------------


class TestResolveSkillWrite:
    def test_agent_create_lands_runtime_local(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        path = skill_surface.resolve_skill_write("mine/my-skill", "agent_create")
        assert path == tmp_path / "skills" / "mine" / "my-skill"

    def test_hub_install_lands_runtime_local(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        path = skill_surface.resolve_skill_write("cat/name", "hub_install")
        assert path == tmp_path / "skills" / "cat" / "name"

    def test_startup_sync_lands_runtime_local(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        path = skill_surface.resolve_skill_write("gbrain/signal-detector", "startup_sync")
        assert path == tmp_path / "skills" / "gbrain" / "signal-detector"

    def test_all_ordinary_ops_land_runtime_local(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        for op in ("self_learn_metadata", "agent_create", "hub_install",
                   "startup_sync", "profile_seed"):
            p = skill_surface.resolve_skill_write("foo", op)  # type: ignore[arg-type]
            assert p == tmp_path / "skills" / "foo", f"op={op} path={p}"

    def test_slug_with_leading_or_trailing_slash_stripped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert skill_surface.resolve_skill_write("/foo/", "agent_create") == \
            tmp_path / "skills" / "foo"

    def test_empty_slug_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with pytest.raises(ValueError, match="non-empty"):
            skill_surface.resolve_skill_write("", "agent_create")
        with pytest.raises(ValueError, match="non-empty"):
            skill_surface.resolve_skill_write("/", "agent_create")

    def test_unknown_op_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with pytest.raises(ValueError, match="unknown Op"):
            skill_surface.resolve_skill_write("foo", "bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Canonical-write gate
# ---------------------------------------------------------------------------


class TestIsCanonicalWriteAllowed:
    def test_promote_is_allowed(self):
        assert skill_surface.is_canonical_write_allowed("promote") is True

    def test_all_ordinary_ops_denied(self):
        for op in ("discover", "self_learn_metadata", "agent_create",
                   "hub_install", "startup_sync", "profile_seed"):
            assert skill_surface.is_canonical_write_allowed(op) is False, \
                f"op={op} unexpectedly allowed canonical write"


# ---------------------------------------------------------------------------
# Governed-target detection
# ---------------------------------------------------------------------------


class TestIsGovernedTarget:
    def test_non_profile_path_not_governed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert skill_surface.is_governed_target(tmp_path / "scratch" / "x.md") is False

    def test_hermes_home_not_git_repo_not_governed(self, tmp_path, monkeypatch):
        """HERMES_HOME that isn't a git repo returns False."""
        home = tmp_path / "profiles" / "test"
        home.mkdir(parents=True)
        (home / "skills").mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        # Not a git repo → not governed
        assert skill_surface.is_governed_target(home / "skills" / "x.md") is False

    def test_live_profile_base_on_protected_branch_is_governed(self, tmp_path, monkeypatch):
        """HERMES_HOME that IS a git repo on main → governed."""
        home = tmp_path / "profiles" / "test"
        home.mkdir(parents=True)
        skills = home / "skills"
        skills.mkdir()
        # Init a git repo at HERMES_HOME on main
        subprocess.run(["git", "init", "-q", "-b", "main", str(home)], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.name", "t"], check=True)
        (skills / "seed.md").write_text("seed")
        subprocess.run(["git", "-C", str(home), "add", "skills/seed.md"], check=True)
        subprocess.run(["git", "-C", str(home), "commit", "-q", "-m", "seed"], check=True)

        monkeypatch.setenv("HERMES_HOME", str(home))
        assert skill_surface.is_governed_target(skills / "seed.md") is True

    def test_live_profile_base_on_non_protected_branch_not_governed(self, tmp_path, monkeypatch):
        """HERMES_HOME git repo on a feature branch → applies but not live_profile_base."""
        home = tmp_path / "profiles" / "test"
        home.mkdir(parents=True)
        skills = home / "skills"
        skills.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "feature-x", str(home)], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.name", "t"], check=True)
        (skills / "seed.md").write_text("seed")
        subprocess.run(["git", "-C", str(home), "add", "skills/seed.md"], check=True)
        subprocess.run(["git", "-C", str(home), "commit", "-q", "-m", "seed"], check=True)

        monkeypatch.setenv("HERMES_HOME", str(home))
        assert skill_surface.is_governed_target(skills / "seed.md") is False


# ---------------------------------------------------------------------------
# Deprecation shim: old imports still work
# ---------------------------------------------------------------------------


class TestDeprecationShim:
    def test_skill_utils_still_exports_excluded_skill_dirs(self):
        from agent.skill_utils import EXCLUDED_SKILL_DIRS as legacy
        assert legacy == skill_surface.EXCLUDED_SKILL_DIRS

    def test_skill_utils_still_exports_get_all_skills_dirs(self):
        from agent.skill_utils import get_all_skills_dirs as legacy
        assert callable(legacy)

    def test_skill_utils_still_exports_live_governed_context(self):
        from agent.skill_utils import get_live_governed_skill_surface_context as legacy
        assert legacy is skill_surface.get_live_governed_skill_surface_context
