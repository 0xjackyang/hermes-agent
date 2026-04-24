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

    def test_trailing_slash_stripped(self, tmp_path, monkeypatch):
        """Trailing slash is safe to normalize — it means "this dir"."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert skill_surface.resolve_skill_write("foo/", "agent_create") == \
            tmp_path / "skills" / "foo"

    def test_leading_slash_rejected_as_absolute(self, tmp_path, monkeypatch):
        """Leading slash is ambiguous with absolute paths (/etc/passwd);
        reject rather than silently normalize to avoid the traversal class."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with pytest.raises(ValueError, match="absolute"):
            skill_surface.resolve_skill_write("/foo/", "agent_create")

    def test_empty_slug_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with pytest.raises(ValueError, match="non-empty"):
            skill_surface.resolve_skill_write("", "agent_create")
        # "/" is now caught by the absolute-path check before emptiness
        with pytest.raises(ValueError, match="absolute|non-empty"):
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


class TestPhase3A1HotfixPathTraversal:
    """Phase 3-A.1 hotfix: slug validation rejects traversal/absolute/backslash."""

    def test_parent_traversal_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with pytest.raises(ValueError, match="parent-traversal|escapes skill root"):
            skill_surface.resolve_skill_write("../escape", "agent_create")

    def test_embedded_parent_traversal_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with pytest.raises(ValueError, match="parent-traversal|escapes skill root"):
            skill_surface.resolve_skill_write("a/../escape", "agent_create")

    def test_absolute_unix_path_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with pytest.raises(ValueError, match="absolute|escapes"):
            skill_surface.resolve_skill_write("/etc/passwd", "agent_create")

    def test_backslash_rejected(self, tmp_path, monkeypatch):
        """Backslashes can re-root on Windows (C:\\..., UNC \\\\server\\...).
        Reject at validation rather than rely on Path join semantics."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with pytest.raises(ValueError, match="backslash|null byte"):
            skill_surface.resolve_skill_write("cat\\name", "agent_create")

    def test_null_byte_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with pytest.raises(ValueError, match="null byte|backslash"):
            skill_surface.resolve_skill_write("cat\x00name", "agent_create")

    def test_resolved_path_inside_root(self, tmp_path, monkeypatch):
        """Defense-in-depth: resolve to canonical path + verify still under root."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        out = skill_surface.resolve_skill_write("cat/name", "agent_create")
        root = tmp_path / "skills"
        assert out.resolve().is_relative_to(root.resolve()), \
            f"resolved path {out.resolve()} escaped root {root.resolve()}"


class TestPhase3A1HotfixOpTaxonomy:
    """Phase 3-A.1 hotfix: delete/uninstall tokens added."""

    def test_agent_delete_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert skill_surface.resolve_skill_write("cat/name", "agent_delete") == \
            tmp_path / "skills" / "cat" / "name"

    def test_hub_uninstall_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert skill_surface.resolve_skill_write("cat/name", "hub_uninstall") == \
            tmp_path / "skills" / "cat" / "name"

    def test_agent_delete_not_canonical_write(self):
        assert skill_surface.is_canonical_write_allowed("agent_delete") is False

    def test_hub_uninstall_not_canonical_write(self):
        assert skill_surface.is_canonical_write_allowed("hub_uninstall") is False


class TestPhase3A1HotfixGovernedSemantics:
    """Phase 3-A.1 hotfix: is_governed_target requires tracked; is_governed_filesystem split out."""

    def test_untracked_on_live_profile_base_is_not_governed_target(self, tmp_path, monkeypatch):
        """NEW path under live governed profile base → not tracked → NOT governed target.
        Codex finding: without this, Phase 3-C callers would wrongly reject new skill creation."""
        import subprocess
        home = tmp_path / "profiles" / "test"
        home.mkdir(parents=True)
        skills = home / "skills"
        skills.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(home)], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.name", "t"], check=True)
        # Seed a tracked file so branch is committed
        (skills / "seed.md").write_text("seed")
        subprocess.run(["git", "-C", str(home), "add", "skills/seed.md"], check=True)
        subprocess.run(["git", "-C", str(home), "commit", "-q", "-m", "seed"], check=True)

        monkeypatch.setenv("HERMES_HOME", str(home))
        # NEW untracked file — must NOT be flagged as governed-target
        new_path = skills / "new-skill.md"
        assert skill_surface.is_governed_target(new_path) is False, \
            "untracked new file on governed profile must not be rejected by is_governed_target"

    def test_untracked_on_live_profile_base_IS_governed_filesystem(self, tmp_path, monkeypatch):
        """Same scenario: is_governed_filesystem returns True (the FS is governed),
        is_governed_target returns False (the specific path is not tracked)."""
        import subprocess
        home = tmp_path / "profiles" / "test"
        home.mkdir(parents=True)
        skills = home / "skills"
        skills.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(home)], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.name", "t"], check=True)
        (skills / "seed.md").write_text("seed")
        subprocess.run(["git", "-C", str(home), "add", "skills/seed.md"], check=True)
        subprocess.run(["git", "-C", str(home), "commit", "-q", "-m", "seed"], check=True)

        monkeypatch.setenv("HERMES_HOME", str(home))
        new_path = skills / "new-skill.md"
        assert skill_surface.is_governed_filesystem(new_path) is True
        assert skill_surface.is_governed_target(new_path) is False

    def test_tracked_on_live_profile_base_IS_governed_target(self, tmp_path, monkeypatch):
        """Original test still holds: tracked file on main → governed_target True."""
        import subprocess
        home = tmp_path / "profiles" / "test"
        home.mkdir(parents=True)
        skills = home / "skills"
        skills.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(home)], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.name", "t"], check=True)
        (skills / "seed.md").write_text("seed")
        subprocess.run(["git", "-C", str(home), "add", "skills/seed.md"], check=True)
        subprocess.run(["git", "-C", str(home), "commit", "-q", "-m", "seed"], check=True)

        monkeypatch.setenv("HERMES_HOME", str(home))
        assert skill_surface.is_governed_target(skills / "seed.md") is True
        assert skill_surface.is_governed_filesystem(skills / "seed.md") is True


class TestPhase3C23ProfileOpTokens:
    """Phase 3-C.2.3 closes the Op taxonomy with profile_delete and
    profile_import. Behavior-design (governance gate pattern for operator-
    invoked commands) deferred to Phase 3-C.2.4."""

    def test_profile_delete_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        path = skill_surface.resolve_skill_write("any-slug", "profile_delete")
        assert path == tmp_path / "skills" / "any-slug"

    def test_profile_import_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        path = skill_surface.resolve_skill_write("any-slug", "profile_import")
        assert path == tmp_path / "skills" / "any-slug"

    def test_profile_delete_not_canonical_write(self):
        """Consistent with every other ordinary op: not "promote"."""
        assert skill_surface.is_canonical_write_allowed("profile_delete") is False

    def test_profile_import_not_canonical_write(self):
        assert skill_surface.is_canonical_write_allowed("profile_import") is False

    def test_full_op_taxonomy_has_nine_ordinary_ops(self):
        """Document the full set so future extensions are visible to reviewers."""
        expected = {
            "self_learn_metadata", "agent_create", "agent_delete",
            "hub_install", "hub_uninstall", "startup_sync",
            "profile_seed", "profile_delete", "profile_import",
        }
        actual = skill_surface._ORDINARY_OPS
        assert actual == expected, (
            f"ORDINARY_OPS drift. Expected {expected!r}, got {actual!r}. "
            f"If adding a new op, update both this test and the runtime-ops "
            f"doctrine entry (decisions/... and runtime/runtime-ops.md)."
        )

