"""Tests for tools/skills_sync.py — manifest-based skill seeding and updating."""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from agent.skill_utils import get_live_governed_skill_surface_context
from tools.skills_sync import (
    MANIFEST_FILE,
    MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION,
    SKILLS_DIR,
    _compute_relative_dest,
    _dir_hash,
    _discover_bundled_skills,
    _format_manifest_entry,
    _get_bundled_dir,
    _parse_manifest_entry,
    _read_manifest,
    _write_manifest,
    sync_skills,
)


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_live_profile_home(tmp_path: Path) -> Path:
    profile_home = tmp_path / ".hermes" / "profiles" / "demo"
    (profile_home / "skills").mkdir(parents=True, exist_ok=True)
    _run_git(profile_home, "init")
    _run_git(profile_home, "config", "user.email", "test@example.com")
    _run_git(profile_home, "config", "user.name", "Test User")
    (profile_home / "README.md").write_text("# demo profile\n", encoding="utf-8")
    _run_git(profile_home, "add", "README.md")
    _run_git(profile_home, "commit", "-m", "initial commit")
    return profile_home


def _commit_all(repo: Path, message: str) -> None:
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", message)


class TestReadWriteManifest:
    def test_read_missing_manifest(self, tmp_path):
        with patch(
            "tools.skills_sync.MANIFEST_FILE",
            tmp_path / "nonexistent",
        ):
            result = _read_manifest()
        assert result == {}

    def test_write_and_read_roundtrip_v2(self, tmp_path):
        manifest_file = tmp_path / ".bundled_manifest"
        entries = {"skill-a": "abc123", "skill-b": "def456", "skill-c": "789012"}

        with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
            _write_manifest(entries)
            result = _read_manifest()

        assert result == entries

    def test_write_and_read_roundtrip_with_flags(self, tmp_path):
        manifest_file = tmp_path / ".bundled_manifest"
        entries = {
            "skill-a": _format_manifest_entry(
                "abc123",
                flags={MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION},
            )
        }

        with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
            _write_manifest(entries)
            result = _read_manifest()

        assert result == entries
        origin_hash, flags = _parse_manifest_entry(result["skill-a"])
        assert origin_hash == "abc123"
        assert flags == {MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION}

    def test_write_manifest_sorted(self, tmp_path):
        manifest_file = tmp_path / ".bundled_manifest"
        entries = {"zebra": "hash1", "alpha": "hash2", "middle": "hash3"}

        with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
            _write_manifest(entries)

        lines = manifest_file.read_text().strip().splitlines()
        names = [line.split(":")[0] for line in lines]
        assert names == ["alpha", "middle", "zebra"]

    def test_read_v1_manifest_migration(self, tmp_path):
        """v1 format (plain names, no hashes) should be read with empty hashes."""
        manifest_file = tmp_path / ".bundled_manifest"
        manifest_file.write_text("skill-a\nskill-b\n")

        with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
            result = _read_manifest()

        assert result == {"skill-a": "", "skill-b": ""}

    def test_read_manifest_ignores_blank_lines(self, tmp_path):
        manifest_file = tmp_path / ".bundled_manifest"
        manifest_file.write_text("skill-a:hash1\n\n  \nskill-b:hash2\n")

        with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
            result = _read_manifest()

        assert result == {"skill-a": "hash1", "skill-b": "hash2"}

    def test_read_manifest_mixed_v1_v2(self, tmp_path):
        """Manifest with both v1 and v2 lines (shouldn't happen but handle gracefully)."""
        manifest_file = tmp_path / ".bundled_manifest"
        manifest_file.write_text("old-skill\nnew-skill:abc123\n")

        with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
            result = _read_manifest()

        assert result == {"old-skill": "", "new-skill": "abc123"}


class TestDirHash:
    def test_same_content_same_hash(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        for d in (dir_a, dir_b):
            d.mkdir()
            (d / "SKILL.md").write_text("# Test")
            (d / "main.py").write_text("print(1)")
        assert _dir_hash(dir_a) == _dir_hash(dir_b)

    def test_different_content_different_hash(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "SKILL.md").write_text("# Version 1")
        (dir_b / "SKILL.md").write_text("# Version 2")
        assert _dir_hash(dir_a) != _dir_hash(dir_b)

    def test_empty_dir(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        h = _dir_hash(d)
        assert isinstance(h, str) and len(h) == 32

    def test_nonexistent_dir(self, tmp_path):
        h = _dir_hash(tmp_path / "nope")
        assert isinstance(h, str)  # returns hash of empty content


class TestDiscoverBundledSkills:
    def test_finds_skills_with_skill_md(self, tmp_path):
        (tmp_path / "category" / "skill-a").mkdir(parents=True)
        (tmp_path / "category" / "skill-a" / "SKILL.md").write_text("# Skill A")
        (tmp_path / "skill-b").mkdir()
        (tmp_path / "skill-b" / "SKILL.md").write_text("# Skill B")
        (tmp_path / "not-a-skill").mkdir()
        (tmp_path / "not-a-skill" / "README.md").write_text("Not a skill")

        skills = _discover_bundled_skills(tmp_path)
        skill_names = {name for name, _ in skills}
        assert "skill-a" in skill_names
        assert "skill-b" in skill_names
        assert "not-a-skill" not in skill_names

    def test_ignores_git_directories(self, tmp_path):
        (tmp_path / ".git" / "hooks").mkdir(parents=True)
        (tmp_path / ".git" / "hooks" / "SKILL.md").write_text("# Fake")
        skills = _discover_bundled_skills(tmp_path)
        assert len(skills) == 0

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        skills = _discover_bundled_skills(tmp_path / "nonexistent")
        assert skills == []


class TestComputeRelativeDest:
    def test_preserves_category_structure(self):
        bundled = Path("/repo/skills")
        skill_dir = Path("/repo/skills/mlops/axolotl")
        dest = _compute_relative_dest(skill_dir, bundled)
        assert str(dest).endswith("mlops/axolotl")

    def test_flat_skill(self):
        bundled = Path("/repo/skills")
        skill_dir = Path("/repo/skills/simple")
        dest = _compute_relative_dest(skill_dir, bundled)
        assert dest.name == "simple"


class TestSyncSkills:
    def _setup_bundled(self, tmp_path):
        """Create a fake bundled skills directory."""
        bundled = tmp_path / "bundled_skills"
        (bundled / "category" / "new-skill").mkdir(parents=True)
        (bundled / "category" / "new-skill" / "SKILL.md").write_text("# New")
        (bundled / "category" / "new-skill" / "main.py").write_text("print(1)")
        (bundled / "category" / "DESCRIPTION.md").write_text("Category desc")
        (bundled / "old-skill").mkdir()
        (bundled / "old-skill" / "SKILL.md").write_text("# Old")
        return bundled

    def _patches(self, bundled, skills_dir, manifest_file):
        """Return context manager stack for patching sync globals."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("tools.skills_sync._get_bundled_dir", return_value=bundled))
        stack.enter_context(patch("tools.skills_sync.SKILLS_DIR", skills_dir))
        stack.enter_context(patch("tools.skills_sync.MANIFEST_FILE", manifest_file))
        return stack

    def test_fresh_install_copies_all(self, tmp_path):
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)

        assert len(result["copied"]) == 2
        assert result["total_bundled"] == 2
        assert result["updated"] == []
        assert result["user_modified"] == []
        assert result["cleaned"] == []
        assert (skills_dir / "category" / "new-skill" / "SKILL.md").exists()
        assert (skills_dir / "old-skill" / "SKILL.md").exists()
        assert (skills_dir / "category" / "DESCRIPTION.md").exists()

    def test_fresh_install_records_origin_hashes(self, tmp_path):
        """After fresh install, manifest should have v2 format with hashes."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        with self._patches(bundled, skills_dir, manifest_file):
            sync_skills(quiet=True)
            manifest = _read_manifest()

        assert "new-skill" in manifest
        assert "old-skill" in manifest
        # Hashes should be non-empty MD5 strings
        assert len(manifest["new-skill"]) == 32
        assert len(manifest["old-skill"]) == 32

    def test_user_deleted_skill_not_re_added(self, tmp_path):
        """Skill in manifest but not on disk = user deleted it. Don't re-add."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        skills_dir.mkdir(parents=True)
        # old-skill is in manifest (v2 format) but NOT on disk
        old_hash = _dir_hash(bundled / "old-skill")
        manifest_file.write_text(f"old-skill:{old_hash}\n")

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)

        assert "new-skill" in result["copied"]
        assert "old-skill" not in result["copied"]
        assert "old-skill" not in result.get("updated", [])
        assert not (skills_dir / "old-skill").exists()

    def test_unmodified_skill_gets_updated(self, tmp_path):
        """Skill in manifest + on disk + user hasn't modified = update from bundled."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        # Simulate: user has old version that was synced from an older bundled
        user_skill = skills_dir / "old-skill"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("# Old v1")
        old_origin_hash = _dir_hash(user_skill)

        # Record origin hash = hash of what was synced (the old version)
        manifest_file.write_text(f"old-skill:{old_origin_hash}\n")

        # Now bundled has a newer version ("# Old" != "# Old v1")
        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)

        # Should be updated because user copy matches origin (unmodified)
        assert "old-skill" in result["updated"]
        assert (user_skill / "SKILL.md").read_text() == "# Old"

    def test_user_modified_skill_not_overwritten(self, tmp_path):
        """Skill modified by user should NOT be overwritten even if bundled changed."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        # Simulate: user had the old version synced, then modified it
        user_skill = skills_dir / "old-skill"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("# Old v1")
        old_origin_hash = _dir_hash(user_skill)

        # Record origin hash from what was originally synced
        manifest_file.write_text(f"old-skill:{old_origin_hash}\n")

        # User modifies their copy
        (user_skill / "SKILL.md").write_text("# My custom version")

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)

        # Should NOT update — user modified it
        assert "old-skill" in result["user_modified"]
        assert "old-skill" not in result.get("updated", [])
        assert (user_skill / "SKILL.md").read_text() == "# My custom version"

    def test_unchanged_skill_not_updated(self, tmp_path):
        """Skill in sync (user == bundled == origin) = no action needed."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        # Copy bundled to user dir (simulating perfect sync state)
        user_skill = skills_dir / "old-skill"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("# Old")
        origin_hash = _dir_hash(user_skill)
        manifest_file.write_text(f"old-skill:{origin_hash}\n")

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)

        assert "old-skill" not in result.get("updated", [])
        assert "old-skill" not in result.get("user_modified", [])
        assert result["skipped"] >= 1

    def test_v1_manifest_migration_sets_baseline(self, tmp_path):
        """v1 manifest entries (no hash) should set baseline from user's current copy."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        # Pre-create skill on disk
        user_skill = skills_dir / "old-skill"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("# Old modified by user")

        # v1 manifest (no hashes)
        manifest_file.write_text("old-skill\n")

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)
            # Should skip (migration baseline set), NOT update
            assert "old-skill" not in result.get("updated", [])
            assert "old-skill" not in result.get("user_modified", [])

            # Now check manifest was upgraded to v2 with user's hash as baseline
            manifest = _read_manifest()
            assert len(manifest["old-skill"]) == 32  # MD5 hash

    def test_v1_migration_then_bundled_update_detected(self, tmp_path):
        """After v1 migration, a subsequent sync should detect bundled updates."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        # User has the SAME content as bundled (in sync)
        user_skill = skills_dir / "old-skill"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("# Old")

        # v1 manifest
        manifest_file.write_text("old-skill\n")

        with self._patches(bundled, skills_dir, manifest_file):
            # First sync: migration — sets baseline
            sync_skills(quiet=True)

            # Now change bundled content
            (bundled / "old-skill" / "SKILL.md").write_text("# Old v2 — improved")

            # Second sync: should detect bundled changed + user unmodified → update
            result = sync_skills(quiet=True)

        assert "old-skill" in result["updated"]
        assert (user_skill / "SKILL.md").read_text() == "# Old v2 — improved"

    def test_stale_manifest_entries_cleaned(self, tmp_path):
        """Skills in manifest that no longer exist in bundled dir get cleaned."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        skills_dir.mkdir(parents=True)
        manifest_file.write_text("old-skill:abc123\nremoved-skill:def456\n")

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)

        assert "removed-skill" in result["cleaned"]
        with patch("tools.skills_sync.MANIFEST_FILE", manifest_file):
            manifest = _read_manifest()
        assert "removed-skill" not in manifest

    def test_does_not_overwrite_existing_unmanifested_skill(self, tmp_path):
        """New skill whose name collides with user-created skill = skipped."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        user_skill = skills_dir / "category" / "new-skill"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("# User modified")

        with self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)

        assert (user_skill / "SKILL.md").read_text() == "# User modified"

    def test_nonexistent_bundled_dir(self, tmp_path):
        with patch("tools.skills_sync._get_bundled_dir", return_value=tmp_path / "nope"):
            result = sync_skills(quiet=True)
        assert result == {
            "copied": [], "updated": [], "governed_skipped": [], "skipped": 0,
            "user_modified": [], "cleaned": [], "total_bundled": 0,
        }

    def test_failed_copy_does_not_poison_manifest(self, tmp_path):
        """If copytree fails, the skill must NOT be added to the manifest.

        Otherwise the next sync treats it as 'user deleted' and never retries.
        """
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        with self._patches(bundled, skills_dir, manifest_file):
            # Patch copytree to fail for new-skill
            original_copytree = __import__("shutil").copytree

            def failing_copytree(src, dst, *a, **kw):
                if "new-skill" in str(src):
                    raise OSError("Simulated disk full")
                return original_copytree(src, dst, *a, **kw)

            with patch("shutil.copytree", side_effect=failing_copytree):
                result = sync_skills(quiet=True)

            # new-skill should NOT be in copied (it failed)
            assert "new-skill" not in result["copied"]

            # Critical: new-skill must NOT be in the manifest
            manifest = _read_manifest()
            assert "new-skill" not in manifest, (
                "Failed copy was recorded in manifest — next sync will "
                "treat it as 'user deleted' and never retry"
            )

            # Now run sync again (copytree works this time) — it should retry
            result2 = sync_skills(quiet=True)
            assert "new-skill" in result2["copied"]
            assert (skills_dir / "category" / "new-skill" / "SKILL.md").exists()

    def test_failed_update_does_not_destroy_user_copy(self, tmp_path):
        """If copytree fails during update, the user's existing copy must survive."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        # Start with old synced version
        user_skill = skills_dir / "old-skill"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("# Old v1")
        old_hash = _dir_hash(user_skill)
        manifest_file.write_text(f"old-skill:{old_hash}\n")

        with self._patches(bundled, skills_dir, manifest_file):
            # Patch copytree to fail (rmtree succeeds, copytree fails)
            original_copytree = __import__("shutil").copytree

            def failing_copytree(src, dst, *a, **kw):
                if "old-skill" in str(src):
                    raise OSError("Simulated write failure")
                return original_copytree(src, dst, *a, **kw)

            with patch("shutil.copytree", side_effect=failing_copytree):
                result = sync_skills(quiet=True)

            # old-skill should NOT be in updated (it failed)
            assert "old-skill" not in result.get("updated", [])

            # The skill directory should still exist (rmtree destroyed it
            # but copytree failed to replace it — this is data loss)
            assert user_skill.exists(), (
                "Update failure destroyed user's skill copy without replacing it"
            )

    def test_update_records_new_origin_hash(self, tmp_path):
        """After updating a skill, the manifest should record the new bundled hash."""
        bundled = self._setup_bundled(tmp_path)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"

        # Start with old synced version
        user_skill = skills_dir / "old-skill"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("# Old v1")
        old_hash = _dir_hash(user_skill)
        manifest_file.write_text(f"old-skill:{old_hash}\n")

        with self._patches(bundled, skills_dir, manifest_file):
            sync_skills(quiet=True)  # updates to "# Old"
            manifest = _read_manifest()

        # New origin hash should match the bundled version
        new_bundled_hash = _dir_hash(bundled / "old-skill")
        assert manifest["old-skill"] == new_bundled_hash
        assert manifest["old-skill"] != old_hash

    def test_sync_skills_skips_updates_on_live_governed_tracked_profile_surface(self, tmp_path):
        bundled = self._setup_bundled(tmp_path)
        profile_home = _init_live_profile_home(tmp_path)
        skills_dir = profile_home / "skills"
        manifest_file = skills_dir / ".bundled_manifest"

        tracked_skill = skills_dir / "old-skill"
        tracked_skill.mkdir(parents=True)
        (tracked_skill / "SKILL.md").write_text("# Old v1", encoding="utf-8")
        _commit_all(profile_home, "add tracked bundled skill")
        old_hash = _dir_hash(tracked_skill)
        manifest_file.write_text(f"old-skill:{old_hash}\n", encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}), self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)
            manifest = _read_manifest()

        assert result["updated"] == []
        assert result["governed_skipped"] == ["old-skill"]
        assert (tracked_skill / "SKILL.md").read_text(encoding="utf-8") == "# Old v1"
        assert manifest["old-skill"] == old_hash

    def test_first_collision_protected_manifest_baseline_stays_preserved_off_live_base(self, tmp_path):
        bundled = self._setup_bundled(tmp_path)
        profile_home = _init_live_profile_home(tmp_path)
        skills_dir = profile_home / "skills"
        manifest_file = skills_dir / ".bundled_manifest"

        tracked_skill = skills_dir / "old-skill"
        tracked_skill.mkdir(parents=True)
        (tracked_skill / "SKILL.md").write_text("# My tracked custom skill", encoding="utf-8")
        _commit_all(profile_home, "add tracked custom skill")

        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}), self._patches(bundled, skills_dir, manifest_file):
            first_result = sync_skills(quiet=True)
            manifest_after_first_sync = _read_manifest()
            first_origin_hash, first_flags = _parse_manifest_entry(manifest_after_first_sync["old-skill"])
            _run_git(profile_home, "checkout", "-b", "packet/worktree")
            second_result = sync_skills(quiet=True)
            manifest_after_second_sync = _read_manifest()
            second_origin_hash, second_flags = _parse_manifest_entry(manifest_after_second_sync["old-skill"])

        # Phase 3-D.1: first-run collision on live base now surfaces the
        # governance skip in governed_skipped (was silently just ticking the
        # skipped counter). Second sync runs after `checkout -b packet/worktree`
        # so it's off live-base — the preserve-off-live-base path does not
        # append to governed_skipped (unchanged, correct).
        assert first_result["updated"] == []
        assert first_result["governed_skipped"] == ["old-skill"]
        assert first_origin_hash == _dir_hash(tracked_skill)
        assert first_flags == {MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION}
        assert second_result["updated"] == []
        assert second_result["governed_skipped"] == []
        assert second_origin_hash == first_origin_hash
        assert second_flags == {MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION}
        assert (tracked_skill / "SKILL.md").read_text(encoding="utf-8") == "# My tracked custom skill"

    def test_repeated_live_base_collision_rerun_keeps_flag_and_blocks_later_off_live_base_overwrite(self, tmp_path):
        bundled = self._setup_bundled(tmp_path)
        profile_home = _init_live_profile_home(tmp_path)
        skills_dir = profile_home / "skills"
        manifest_file = skills_dir / ".bundled_manifest"

        tracked_skill = skills_dir / "old-skill"
        tracked_skill.mkdir(parents=True)
        (tracked_skill / "SKILL.md").write_text("# Old", encoding="utf-8")
        _commit_all(profile_home, "add tracked custom skill matching bundled contents")

        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}), self._patches(bundled, skills_dir, manifest_file):
            first_result = sync_skills(quiet=True)
            manifest_after_first_sync = _read_manifest()
            first_origin_hash, first_flags = _parse_manifest_entry(manifest_after_first_sync["old-skill"])

            second_result = sync_skills(quiet=True)
            manifest_after_second_sync = _read_manifest()
            second_origin_hash, second_flags = _parse_manifest_entry(manifest_after_second_sync["old-skill"])

            _run_git(profile_home, "checkout", "-b", "packet/worktree")
            (bundled / "old-skill" / "SKILL.md").write_text("# Old bundled v2", encoding="utf-8")

            third_result = sync_skills(quiet=True)
            manifest_after_third_sync = _read_manifest()
            third_origin_hash, third_flags = _parse_manifest_entry(manifest_after_third_sync["old-skill"])

        # Phase 3-D.1: first-run collision on live base surfaces governance skip.
        # Second sync is still on live base but bundled_hash == origin_hash
        # (no rewrite needed) so the in-sync branch does not touch governed_skipped.
        assert first_result["updated"] == []
        assert first_result["governed_skipped"] == ["old-skill"]
        assert first_origin_hash == _dir_hash(tracked_skill)
        assert first_flags == {MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION}

        assert second_result["updated"] == []
        assert second_result["governed_skipped"] == []
        assert second_origin_hash == first_origin_hash
        assert second_flags == {MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION}

        assert third_result["updated"] == []
        assert third_result["governed_skipped"] == []
        assert third_origin_hash == first_origin_hash
        assert third_flags == {MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION}
        assert (tracked_skill / "SKILL.md").read_text(encoding="utf-8") == "# Old"

    def test_sync_skills_updates_tracked_skill_off_live_base_branch(self, tmp_path):
        bundled = self._setup_bundled(tmp_path)
        profile_home = _init_live_profile_home(tmp_path)
        skills_dir = profile_home / "skills"
        manifest_file = skills_dir / ".bundled_manifest"

        tracked_skill = skills_dir / "old-skill"
        tracked_skill.mkdir(parents=True)
        (tracked_skill / "SKILL.md").write_text("# Old v1", encoding="utf-8")
        _commit_all(profile_home, "add tracked bundled skill")
        _run_git(profile_home, "checkout", "-b", "packet/worktree")
        old_hash = _dir_hash(tracked_skill)
        manifest_file.write_text(f"old-skill:{old_hash}\n", encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}), self._patches(bundled, skills_dir, manifest_file):
            result = sync_skills(quiet=True)

        assert result["updated"] == ["old-skill"]
        assert result["governed_skipped"] == []
        assert (tracked_skill / "SKILL.md").read_text(encoding="utf-8") == "# Old"

    def test_live_governed_skill_surface_context_tracks_branch_and_git_status(self, tmp_path):
        profile_home = _init_live_profile_home(tmp_path)
        skill_dir = profile_home / "skills" / "tracked-skill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("# tracked\n", encoding="utf-8")
        _commit_all(profile_home, "add tracked skill")

        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}):
            initial = get_live_governed_skill_surface_context(skill_md)
            _run_git(profile_home, "checkout", "-b", "packet/worktree")
            transitioned = get_live_governed_skill_surface_context(skill_md)

        assert initial["applies"] is True
        assert initial["live_profile_base"] is True
        assert initial["tracked"] is True
        assert initial["reason"] == "live_profile_base"
        assert transitioned["applies"] is True
        assert transitioned["live_profile_base"] is False
        assert transitioned["tracked"] is True
        assert transitioned["reason"] == "profile_repo_not_on_live_base_branch"


class TestGetBundledDir:
    def test_env_var_override(self, tmp_path, monkeypatch):
        """HERMES_BUNDLED_SKILLS env var overrides the default path resolution."""
        custom_dir = tmp_path / "custom_skills"
        custom_dir.mkdir()
        monkeypatch.setenv("HERMES_BUNDLED_SKILLS", str(custom_dir))
        assert _get_bundled_dir() == custom_dir

    def test_default_without_env_var(self, monkeypatch):
        """Without the env var, falls back to relative path from __file__."""
        monkeypatch.delenv("HERMES_BUNDLED_SKILLS", raising=False)
        result = _get_bundled_dir()
        assert result.name == "skills"

    def test_env_var_empty_string_ignored(self, monkeypatch):
        """Empty HERMES_BUNDLED_SKILLS should fall back to default."""
        monkeypatch.setenv("HERMES_BUNDLED_SKILLS", "")
        result = _get_bundled_dir()
        assert result.name == "skills"
