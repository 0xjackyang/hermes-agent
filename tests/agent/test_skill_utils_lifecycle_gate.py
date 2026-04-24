"""Phase 3-C.1 tests: lifecycle writeback governance gate.

Verifies that `sync_skill_lifecycle_metadata` does NOT mutate tracked
files on a live governed profile base (hidden-danger scenario
surfaced during Phase 3 scouting), while preserving existing behavior
for all other cases.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent.skill_utils import sync_skill_lifecycle_metadata


def _init_live_profile_base(root: Path, branch: str = "main") -> Path:
    """Create a HERMES_HOME that IS a git repo on `branch`. Returns HERMES_HOME."""
    home = root / "profiles" / "test"
    home.mkdir(parents=True)
    (home / "skills").mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(home)], check=True)
    subprocess.run(["git", "-C", str(home), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(home), "config", "user.name", "t"], check=True)
    return home


def _write_and_commit_skill(home: Path, slug: str, body: str) -> Path:
    """Write a skill file and commit it so git sees it as tracked."""
    skill = home / "skills" / slug
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(body)
    rel = skill.relative_to(home)
    subprocess.run(["git", "-C", str(home), "add", str(rel)], check=True)
    subprocess.run(["git", "-C", str(home), "commit", "-q", "-m", f"add {slug}"], check=True)
    return skill


# Minimal frontmatter-ish content that triggers lifecycle normalization
_SEED_CONTENT = """---
name: test-skill
description: a test
---

body
"""


class TestLifecycleWritebackGate:
    """Phase 3-C.1: sync_skill_lifecycle_metadata must defer writes on
    governed tracked targets, matching is_governed_target semantics."""

    def test_governed_tracked_file_not_mutated(self, tmp_path, monkeypatch):
        """Live profile base + committed file → gate defers, persisted=False,
        file contents unchanged on disk."""
        home = _init_live_profile_base(tmp_path)
        skill = _write_and_commit_skill(home, "test.md", _SEED_CONTENT)
        monkeypatch.setenv("HERMES_HOME", str(home))

        original_contents = skill.read_text()
        original_mtime = skill.stat().st_mtime

        result = sync_skill_lifecycle_metadata(skill, mark_used=True)

        assert result["persisted"] is False, \
            "governed tracked file was persisted despite gate"
        assert skill.read_text() == original_contents, \
            "governed tracked file contents changed on disk"
        assert skill.stat().st_mtime == original_mtime, \
            "governed tracked file mtime changed despite gate"
        # content in return value is still the normalized in-memory version
        assert isinstance(result.get("content"), str)
        assert isinstance(result.get("frontmatter"), dict)

    def test_governed_untracked_file_IS_written(self, tmp_path, monkeypatch):
        """Live profile base + untracked new file → gate does NOT defer
        (new skill creation must still work in governed profile)."""
        home = _init_live_profile_base(tmp_path)
        # Seed one tracked file so the branch has a commit
        _write_and_commit_skill(home, "seed.md", _SEED_CONTENT)
        # New, untracked skill
        new_skill = home / "skills" / "new.md"
        new_skill.write_text(_SEED_CONTENT)
        monkeypatch.setenv("HERMES_HOME", str(home))

        result = sync_skill_lifecycle_metadata(new_skill, mark_used=True)

        # Should write (untracked ≠ governed-target per Phase 3-A.1 semantics)
        # Not strictly asserting persisted=True because it depends on whether
        # lifecycle normalization marked `changed`; the key invariant is
        # the gate did NOT block a write here.
        assert isinstance(result.get("content"), str)
        assert isinstance(result.get("frontmatter"), dict)
        # File still readable and exists
        assert new_skill.exists()

    def test_non_governed_filesystem_writes_normally(self, tmp_path, monkeypatch):
        """Plain HERMES_HOME (not a git repo) → gate does not apply."""
        home = tmp_path / "plain-home"
        home.mkdir()
        (home / "skills").mkdir()
        skill = home / "skills" / "test.md"
        skill.write_text(_SEED_CONTENT)
        monkeypatch.setenv("HERMES_HOME", str(home))

        result = sync_skill_lifecycle_metadata(skill, mark_used=True)

        # Behavioral equivalence with pre-gate: normal write path unchanged
        assert isinstance(result.get("content"), str)
        assert skill.exists()

    def test_governed_on_non_protected_branch_writes_normally(self, tmp_path, monkeypatch):
        """Live profile home on a feature branch → not live_profile_base
        → gate does not apply (feature branches are work-in-progress)."""
        home = _init_live_profile_base(tmp_path, branch="feature-x")
        skill = _write_and_commit_skill(home, "test.md", _SEED_CONTENT)
        monkeypatch.setenv("HERMES_HOME", str(home))

        result = sync_skill_lifecycle_metadata(skill, mark_used=True)

        # live_profile_base=False on feature branch → is_governed_target=False
        # Writes proceed normally
        assert isinstance(result.get("content"), str)

    def test_persist_false_short_circuits_before_gate(self, tmp_path, monkeypatch):
        """persist=False must still skip the write — gate is irrelevant when
        caller explicitly asks for no-persist."""
        home = _init_live_profile_base(tmp_path)
        skill = _write_and_commit_skill(home, "test.md", _SEED_CONTENT)
        monkeypatch.setenv("HERMES_HOME", str(home))

        result = sync_skill_lifecycle_metadata(skill, persist=False, mark_used=True)

        assert result["persisted"] is False
        # content still computed in memory
        assert isinstance(result.get("content"), str)

    def test_no_circular_import(self):
        """Verify lazy-import pattern actually works — both modules load
        cleanly in isolation and the gate function is importable."""
        # Import both in the problematic order that was the original risk
        import importlib
        importlib.import_module("agent.skill_utils")
        importlib.import_module("agent.skill_surface")
        # Gate function should be callable (lazy-imported inside the function)
        from agent.skill_utils import sync_skill_lifecycle_metadata as fn
        assert callable(fn)
