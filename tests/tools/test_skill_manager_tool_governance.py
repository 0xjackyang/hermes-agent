"""Phase 3-C.2.1 tests: governance gates at skill_manager_tool write sites."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.skill_manager_tool import (
    _create_skill,
    _edit_skill,
    _patch_skill,
    _delete_skill,
    _write_file,
    _remove_file,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _init_governed_home(tmp_path: Path) -> Path:
    """HERMES_HOME that is itself a git repo on main (live profile base)."""
    home = tmp_path / "profiles" / "test"
    home.mkdir(parents=True)
    (home / "skills").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(home)], check=True)
    subprocess.run(["git", "-C", str(home), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(home), "config", "user.name", "t"], check=True)
    return home


def _add_tracked_skill(home: Path, name: str, body: str) -> Path:
    """Write + commit a skill so is_governed_target sees it as tracked."""
    skill_dir = home / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(body)
    subprocess.run(
        ["git", "-C", str(home), "add", f"skills/{name}/SKILL.md"], check=True
    )
    subprocess.run(["git", "-C", str(home), "commit", "-q", "-m", f"add {name}"], check=True)
    return skill_file


SKILL_BODY = """---
name: demo-skill
description: tracked canonical skill
---

body
"""

VALID_NEW_SKILL = """---
name: demo-skill
description: new attempt
---

new body content goes here
"""


# ---------------------------------------------------------------------------
# Tests: governance gate rejects write on governed tracked targets
# ---------------------------------------------------------------------------


class TestGovernanceRejectsWriteOnTrackedTarget:
    def test_create_collides_with_tracked_skill_returns_already_exists(self, tmp_path, monkeypatch):
        """Create on a name that already has a tracked skill hits the
        _find_skill existing check FIRST (line 374-381), before the
        governance gate at line 397. Test asserts the expected
        already-exists path. The governance gate at _create_skill is
        belt-and-suspenders — unreachable through the public API so long
        as _find_skill runs first, but defensive for future refactors.
        See tests below for the directly-reachable governance rejections
        on edit/delete/write_file/remove_file."""
        home = _init_governed_home(tmp_path)
        _add_tracked_skill(home, "demo-skill", SKILL_BODY)
        monkeypatch.setenv("HERMES_HOME", str(home))
        import importlib
        import tools.skill_manager_tool as mod
        importlib.reload(mod)

        result = mod._create_skill(name="demo-skill", content=VALID_NEW_SKILL)

        assert result["success"] is False
        assert "already exists" in result["error"], (
            f"expected existing-skill error first; got {result!r}"
        )

    def test_edit_tracked_skill_is_rejected_as_governed(self, tmp_path, monkeypatch):
        """Editing a tracked canonical skill must return a governance error."""
        home = _init_governed_home(tmp_path)
        tracked_file = _add_tracked_skill(home, "demo-skill", SKILL_BODY)
        monkeypatch.setenv("HERMES_HOME", str(home))
        import importlib
        import tools.skill_manager_tool as mod
        importlib.reload(mod)

        result = mod._edit_skill(name="demo-skill", content=VALID_NEW_SKILL)

        assert result["success"] is False
        assert "governed" in result["error"].lower()
        # File unchanged on disk
        assert tracked_file.read_text() == SKILL_BODY

    def test_delete_tracked_skill_is_rejected_as_governed(self, tmp_path, monkeypatch):
        home = _init_governed_home(tmp_path)
        tracked_file = _add_tracked_skill(home, "demo-skill", SKILL_BODY)
        monkeypatch.setenv("HERMES_HOME", str(home))
        import importlib
        import tools.skill_manager_tool as mod
        importlib.reload(mod)

        result = mod._delete_skill(name="demo-skill")

        assert result["success"] is False
        assert "governed" in result["error"].lower()
        # Skill dir + file still present
        assert tracked_file.exists()

    def test_write_file_on_tracked_supporting_file_rejected(self, tmp_path, monkeypatch):
        home = _init_governed_home(tmp_path)
        _add_tracked_skill(home, "demo-skill", SKILL_BODY)
        # Also track a supporting file
        support = home / "skills" / "demo-skill" / "references" / "note.md"
        support.parent.mkdir(parents=True)
        support.write_text("tracked note")
        subprocess.run(
            ["git", "-C", str(home), "add", "skills/demo-skill/references/note.md"], check=True
        )
        subprocess.run(["git", "-C", str(home), "commit", "-q", "-m", "add note"], check=True)
        original = support.read_text()
        monkeypatch.setenv("HERMES_HOME", str(home))
        import importlib
        import tools.skill_manager_tool as mod
        importlib.reload(mod)

        result = mod._write_file(
            name="demo-skill", file_path="references/note.md", file_content="new content"
        )

        assert result["success"] is False
        assert "governed" in result["error"].lower()
        assert support.read_text() == original

    def test_remove_file_on_tracked_supporting_file_rejected(self, tmp_path, monkeypatch):
        home = _init_governed_home(tmp_path)
        _add_tracked_skill(home, "demo-skill", SKILL_BODY)
        support = home / "skills" / "demo-skill" / "references" / "note.md"
        support.parent.mkdir(parents=True)
        support.write_text("tracked note")
        subprocess.run(
            ["git", "-C", str(home), "add", "skills/demo-skill/references/note.md"], check=True
        )
        subprocess.run(["git", "-C", str(home), "commit", "-q", "-m", "add note"], check=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        import importlib
        import tools.skill_manager_tool as mod
        importlib.reload(mod)

        result = mod._remove_file(name="demo-skill", file_path="references/note.md")

        assert result["success"] is False
        assert "governed" in result["error"].lower()
        assert support.exists()


# ---------------------------------------------------------------------------
# Regression tests: non-governed + governed-untracked still work
# ---------------------------------------------------------------------------


class TestGovernanceAllowsWriteOnNonGovernedOrUntracked:
    def test_create_on_plain_home_succeeds(self, tmp_path, monkeypatch):
        home = tmp_path / "plain-home"
        home.mkdir()
        (home / "skills").mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        import importlib
        import tools.skill_manager_tool as mod
        importlib.reload(mod)

        result = mod._create_skill(name="fresh-skill", content=VALID_NEW_SKILL)

        assert result["success"] is True, f"create on plain home failed: {result!r}"
        assert (home / "skills" / "fresh-skill" / "SKILL.md").exists()

    def test_create_on_governed_but_untracked_name_succeeds(self, tmp_path, monkeypatch):
        """Governed FS, but new skill name is not tracked → gate doesn't fire
        (per 3-A.1 tracked semantic). New skill creation on a governed profile
        is legitimate."""
        home = _init_governed_home(tmp_path)
        # Seed one tracked skill so branch has a commit
        _add_tracked_skill(home, "seed", SKILL_BODY)
        monkeypatch.setenv("HERMES_HOME", str(home))
        import importlib
        import tools.skill_manager_tool as mod
        importlib.reload(mod)

        result = mod._create_skill(name="new-skill", content=VALID_NEW_SKILL)

        assert result["success"] is True, (
            f"create on governed-untracked failed: {result!r} "
            f"(3-A.1 regression: tracked semantic would wrongly reject new creation)"
        )
        assert (home / "skills" / "new-skill" / "SKILL.md").exists()

    def test_edit_on_governed_but_untracked_succeeds(self, tmp_path, monkeypatch):
        """Write + don't commit → untracked file on governed FS → edit allowed."""
        home = _init_governed_home(tmp_path)
        _add_tracked_skill(home, "seed", SKILL_BODY)
        # Write untracked skill (no commit)
        untracked_dir = home / "skills" / "untracked-skill"
        untracked_dir.mkdir(parents=True)
        (untracked_dir / "SKILL.md").write_text(VALID_NEW_SKILL)
        monkeypatch.setenv("HERMES_HOME", str(home))
        import importlib
        import tools.skill_manager_tool as mod
        importlib.reload(mod)

        revised = VALID_NEW_SKILL.replace("new body content", "edited content")
        result = mod._edit_skill(name="untracked-skill", content=revised)

        assert result["success"] is True
