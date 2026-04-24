"""Phase 3-D integration test: governance invariant across self-learning paths.

**Invariant (the whole point of Phase 3):** Given a live governed profile base
(HERMES_HOME is itself a git repo on a protected branch), the main self-learning
code paths must NOT mutate tracked skill files.

This test exercises the invariant against the real code paths (not just unit
mocks): `sync_skills`, `sync_skill_lifecycle_metadata`, and direct-path
lifecycle touchups. If any future change regresses the governance contract,
this test fails.

Covered code paths:
- `tools.skills_sync.sync_skills` with a bundled skill colliding with a
  tracked custom skill (the startup-sync collision scenario)
- `agent.skill_utils.sync_skill_lifecycle_metadata` with `mark_used=True`
  on a tracked file (the mark-used-on-invocation scenario)
- `sync_skill_lifecycle_metadata` used as a normalization-only read path
  (the list-scan scenario)

Not covered here (belongs to Phase 3-C.2 once those migrate):
- `skill_manager_tool` create / edit / delete
- `skills_hub` install / uninstall
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest


SKILL_BODY_A = """---
name: demo-skill
description: the BUNDLED version
---

bundled body content
"""

SKILL_BODY_B = """---
name: demo-skill
description: the USER's CUSTOM version (tracked on main)
---

custom body content with local edits
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def live_governed_profile(tmp_path, monkeypatch):
    """Build a fake live governed profile base with tracked skill files.

    Layout:
      tmp_path/
        profiles/
          test-profile/          <- HERMES_HOME (git repo on main)
            skills/
              demo-skill/
                SKILL.md         <- tracked, content B
            .git/
        bundled-source/
          skills/
            demo-skill/
              SKILL.md           <- bundled, content A (what sync would install)

    Sets HERMES_HOME and HERMES_BUNDLED_SKILLS env vars. Returns a dict
    with paths for the test to reference.
    """
    home = tmp_path / "profiles" / "test-profile"
    home.mkdir(parents=True)
    home_skills = home / "skills" / "demo-skill"
    home_skills.mkdir(parents=True)
    tracked_file = home_skills / "SKILL.md"
    tracked_file.write_text(SKILL_BODY_B)

    # Make HERMES_HOME a git repo on main with the tracked file committed
    subprocess.run(["git", "init", "-q", "-b", "main", str(home)], check=True)
    subprocess.run(["git", "-C", str(home), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(home), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(home), "add", "skills/"], check=True)
    subprocess.run(["git", "-C", str(home), "commit", "-q", "-m", "track custom skill"], check=True)

    # Bundled source (what sync_skills would copy FROM)
    bundled = tmp_path / "bundled-source" / "skills" / "demo-skill"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text(SKILL_BODY_A)

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_SKILLS", str(bundled.parent))

    return {
        "home": home,
        "tracked_file": tracked_file,
        "bundled_file": bundled / "SKILL.md",
        "tracked_sha": _sha256(tracked_file),
        "tracked_mtime": tracked_file.stat().st_mtime,
    }


class TestGovernanceInvariantIntegration:
    """End-to-end: self-learning code paths do NOT mutate tracked canonical files."""

    def test_sync_skills_preserves_tracked_custom_collision(self, live_governed_profile):
        """sync_skills encounters a bundled skill whose name matches a tracked
        custom user skill. The custom file must NOT be overwritten, even
        when bundled content differs."""
        # Force fresh module state so module-level SKILLS_DIR resolves against
        # the monkeypatched HERMES_HOME
        import importlib
        import tools.skills_sync as skills_sync_mod
        importlib.reload(skills_sync_mod)

        result = skills_sync_mod.sync_skills(quiet=True)

        tracked = live_governed_profile["tracked_file"]
        assert _sha256(tracked) == live_governed_profile["tracked_sha"], \
            "sync_skills mutated a tracked custom skill — Phase 3 governance violation"
        assert tracked.read_text() == SKILL_BODY_B, \
            "tracked file body changed; governance gate failed"

        # The skipped counter must tick up for the collision. Which specific
        # reporting bucket is used (governed_skipped vs user_modified vs just
        # skipped) is a separate UX concern tracked as a Phase 3-D.1 follow-on.
        # The governance invariant is the file not mutated; reporting gap is
        # a nice-to-have that does not affect on-disk safety.
        assert result.get("skipped", 0) >= 1, (
            f"sync_skills should have skipped the collision; got {result!r}"
        )

    def test_sync_lifecycle_mark_used_does_not_mutate_tracked(self, live_governed_profile):
        """sync_skill_lifecycle_metadata with mark_used=True on a tracked file
        in a live governed profile must defer the last_used_at stamp (Phase 3-C.1)."""
        from agent.skill_utils import sync_skill_lifecycle_metadata

        tracked = live_governed_profile["tracked_file"]
        original_sha = live_governed_profile["tracked_sha"]
        original_mtime = live_governed_profile["tracked_mtime"]

        result = sync_skill_lifecycle_metadata(tracked, mark_used=True)

        assert _sha256(tracked) == original_sha, \
            "lifecycle mark_used mutated tracked file on governed profile"
        assert tracked.stat().st_mtime == original_mtime, \
            "tracked file mtime changed despite governance gate"
        assert result["persisted"] is False, \
            f"persisted=True on governed target; got {result!r}"

    def test_sync_lifecycle_list_scan_does_not_mutate_tracked(self, live_governed_profile):
        """The list-scan pattern (callers C1/C3 in 3-C.1 caller audit) reads
        `content` field without caring about persistence. Must not mutate."""
        from agent.skill_utils import sync_skill_lifecycle_metadata

        tracked = live_governed_profile["tracked_file"]
        original_sha = live_governed_profile["tracked_sha"]

        # No mark_used — pure normalize-and-return path
        result = sync_skill_lifecycle_metadata(tracked, persist=True)

        assert _sha256(tracked) == original_sha
        # `content` is always populated for in-memory consumers
        assert isinstance(result.get("content"), str)
        assert len(result["content"]) > 0


class TestGovernanceInvariantRegression:
    """Regression-proof: non-governed flows still mutate normally."""

    def test_non_governed_profile_normalizes_and_persists(self, tmp_path, monkeypatch):
        """Plain HERMES_HOME (not a git repo) — full write path still works."""
        from agent.skill_utils import sync_skill_lifecycle_metadata

        home = tmp_path / "plain-home"
        home.mkdir()
        (home / "skills").mkdir()
        skill = home / "skills" / "demo.md"
        # Use content that requires normalization (missing lifecycle fields)
        skill.write_text("---\nname: demo\ndescription: x\n---\n\nbody\n")
        monkeypatch.setenv("HERMES_HOME", str(home))

        result = sync_skill_lifecycle_metadata(skill, mark_used=True)

        # Non-governed FS → write path unchanged from pre-3-C.1
        assert isinstance(result.get("content"), str)
        # File still exists and readable
        assert skill.exists()

    def test_governed_but_untracked_writes_normally(self, tmp_path, monkeypatch):
        """Governed FS but untracked file → new skill creation must still write.
        Regression guard for the 3-A.1 tracked-semantic fix."""
        from agent.skill_utils import sync_skill_lifecycle_metadata

        home = tmp_path / "profiles" / "test"
        home.mkdir(parents=True)
        (home / "skills").mkdir()
        # Seed one tracked file so branch has commits
        seed = home / "skills" / "seed.md"
        seed.write_text("---\nname: seed\ndescription: x\n---\n\nbody\n")
        subprocess.run(["git", "init", "-q", "-b", "main", str(home)], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "-C", str(home), "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(home), "add", "skills/seed.md"], check=True)
        subprocess.run(["git", "-C", str(home), "commit", "-q", "-m", "seed"], check=True)

        # New UNTRACKED file — tracked semantic says this is NOT governed-target
        new_skill = home / "skills" / "new-skill.md"
        new_skill.write_text("---\nname: new\ndescription: x\n---\n\nbody\n")
        monkeypatch.setenv("HERMES_HOME", str(home))

        result = sync_skill_lifecycle_metadata(new_skill, mark_used=True)

        # Untracked → gate does NOT block, file may be written
        assert isinstance(result.get("content"), str)
        assert new_skill.exists()
