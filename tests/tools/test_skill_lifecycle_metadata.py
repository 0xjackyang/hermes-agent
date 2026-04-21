import json
from pathlib import Path
from unittest.mock import patch

from tools.skill_manager_tool import _create_skill, _edit_skill
from tools.skills_tool import skill_view


SKILL_CONTENT = """---
name: test-skill
description: Test skill.
---

# Test Skill

Do the thing.
"""


def _write_skill(skills_dir: Path, name: str, frontmatter_extra: str = "", body: str = "Do the thing.") -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Description for {name}.\n{frontmatter_extra}---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_skill_view_backfills_lifecycle_metadata_and_updates_last_used(tmp_path):
    _write_skill(tmp_path, "metadata-free")

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        result = json.loads(skill_view("metadata-free"))

    assert result["success"] is True
    assert result["created_at"] == "unknown"
    assert result["status"] == "active"
    assert result["source_session_ids"] == []
    assert result["last_used_at"] not in (None, "", "never")

    stored = (tmp_path / "metadata-free" / "SKILL.md").read_text(encoding="utf-8")
    assert "created_at: unknown" in stored
    assert "status: active" in stored
    assert "last_used_at:" in stored


def test_skill_manage_create_and_edit_capture_source_session_ids(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    with patch("tools.skill_manager_tool.SKILLS_DIR", skills_dir), patch(
        "agent.skill_utils.get_all_skills_dirs", return_value=[skills_dir]
    ):
        created = _create_skill("test-skill", SKILL_CONTENT, session_id="session-create")
        assert created["success"] is True

        edited = _edit_skill(
            "test-skill",
            SKILL_CONTENT.replace("Do the thing.", "Do the edited thing."),
            session_id="session-edit",
        )
        assert edited["success"] is True

    stored = (skills_dir / "test-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "created_at:" in stored
    assert "last_used_at: never" in stored
    assert "session-create" in stored
    assert "session-edit" in stored
