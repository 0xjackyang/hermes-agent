"""Phase 3-C.2.2 tests: governance gates at skills_hub install/uninstall.

Follows the per-function error contracts:
- install_from_quarantine raises ValueError (caller catches)
- uninstall_skill returns Tuple[bool, str]
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _init_governed_home(tmp_path: Path) -> Path:
    home = tmp_path / "profiles" / "test"
    home.mkdir(parents=True)
    (home / "skills").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(home)], check=True)
    subprocess.run(["git", "-C", str(home), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(home), "config", "user.name", "t"], check=True)
    return home


def _add_tracked_skill(home: Path, slug: str, body: str) -> Path:
    skill_dir = home / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(body)
    subprocess.run(
        ["git", "-C", str(home), "add", f"skills/{slug}/SKILL.md"], check=True
    )
    subprocess.run(
        ["git", "-C", str(home), "commit", "-q", "-m", f"add {slug}"], check=True
    )
    return skill_md


def _make_quarantine_bundle(quarantine_dir: Path, skill_name: str, body: str) -> Path:
    """Create a minimal quarantined skill bundle ready for install_from_quarantine."""
    bundle_path = quarantine_dir / skill_name
    bundle_path.mkdir(parents=True)
    (bundle_path / "SKILL.md").write_text(body)
    return bundle_path


NEW_BODY = """---
name: demo
description: bundled candidate
---
bundled body
"""

TRACKED_BODY = """---
name: demo
description: user's tracked canonical version
---
tracked body
"""


class TestHubInstallGovernance:
    """install_from_quarantine raises ValueError on governed tracked collision."""

    def test_install_rejects_tracked_canonical_collision(self, tmp_path, monkeypatch):
        home = _init_governed_home(tmp_path)
        tracked = _add_tracked_skill(home, "demo", TRACKED_BODY)
        monkeypatch.setenv("HERMES_HOME", str(home))

        # Reload to pick up new HERMES_HOME for module-level SKILLS_DIR/HUB_DIR
        import importlib
        import tools.skills_hub as mod
        importlib.reload(mod)

        # Create a valid quarantine bundle (must live under the reloaded HUB_DIR)
        mod.ensure_hub_dirs()
        q_bundle = _make_quarantine_bundle(mod.QUARANTINE_DIR, "demo", NEW_BODY)

        fake_bundle = MagicMock()
        fake_bundle.source = "test"
        fake_bundle.identifier = "demo@test"
        fake_bundle.trust_level = "untrusted"
        fake_bundle.files = {"SKILL.md": NEW_BODY}
        fake_bundle.metadata = {}
        fake_scan = MagicMock()
        fake_scan.verdict = "pass"

        with pytest.raises(ValueError, match="governed canonical skill surface"):
            mod.install_from_quarantine(
                q_bundle, "demo", "", fake_bundle, fake_scan
            )

        # Tracked file untouched
        assert tracked.read_text() == TRACKED_BODY

    def test_install_allows_untracked_new_skill_on_governed_profile(self, tmp_path, monkeypatch):
        """3-A.1 tracked-semantic regression guard. Install a skill whose name
        is NOT tracked on the governed profile — gate must not fire."""
        home = _init_governed_home(tmp_path)
        _add_tracked_skill(home, "seed", TRACKED_BODY)  # unrelated tracked skill
        monkeypatch.setenv("HERMES_HOME", str(home))

        import importlib
        import tools.skills_hub as mod
        importlib.reload(mod)

        mod.ensure_hub_dirs()
        q_bundle = _make_quarantine_bundle(mod.QUARANTINE_DIR, "brand-new", NEW_BODY)

        fake_bundle = MagicMock()
        fake_bundle.source = "test"
        fake_bundle.identifier = "brand-new@test"
        fake_bundle.trust_level = "untrusted"
        fake_bundle.files = {"SKILL.md": NEW_BODY}
        fake_bundle.metadata = {}
        fake_scan = MagicMock()
        fake_scan.verdict = "pass"

        # Must NOT raise
        result = mod.install_from_quarantine(
            q_bundle, "brand-new", "", fake_bundle, fake_scan
        )
        assert result.exists()
        assert (result / "SKILL.md").read_text() == NEW_BODY

    def test_install_allows_on_non_governed_profile(self, tmp_path, monkeypatch):
        home = tmp_path / "plain-home"
        home.mkdir()
        (home / "skills").mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        import importlib
        import tools.skills_hub as mod
        importlib.reload(mod)

        mod.ensure_hub_dirs()
        q_bundle = _make_quarantine_bundle(mod.QUARANTINE_DIR, "demo", NEW_BODY)

        fake_bundle = MagicMock()
        fake_bundle.source = "test"
        fake_bundle.identifier = "demo@test"
        fake_bundle.trust_level = "untrusted"
        fake_bundle.files = {"SKILL.md": NEW_BODY}
        fake_bundle.metadata = {}
        fake_scan = MagicMock()
        fake_scan.verdict = "pass"

        result = mod.install_from_quarantine(q_bundle, "demo", "", fake_bundle, fake_scan)
        assert result.exists()


class TestHubUninstallGovernance:
    """uninstall_skill returns (False, msg) on governed tracked target."""

    def test_uninstall_rejects_tracked_canonical_skill(self, tmp_path, monkeypatch):
        home = _init_governed_home(tmp_path)
        tracked = _add_tracked_skill(home, "demo", TRACKED_BODY)
        monkeypatch.setenv("HERMES_HOME", str(home))

        import importlib
        import tools.skills_hub as mod
        importlib.reload(mod)

        # Seed the hub lock so uninstall_skill passes the "not a hub-installed" check
        mod.ensure_hub_dirs()
        lock = mod.HubLockFile()
        lock.record_install(
            name="demo",
            source="test",
            identifier="demo@test",
            trust_level="untrusted",
            scan_verdict="pass",
            skill_hash="h",
            install_path="demo",
            files=["SKILL.md"],
            metadata={},
        )

        ok, msg = mod.uninstall_skill("demo")
        assert ok is False
        assert "governed canonical skill surface" in msg
        # Tracked file untouched
        assert tracked.read_text() == TRACKED_BODY

    def test_uninstall_not_a_hub_installed_still_wins(self, tmp_path, monkeypatch):
        """Regression: 'not a hub-installed skill' error must still take
        precedence over governance (governance gate runs AFTER lookup)."""
        home = _init_governed_home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))

        import importlib
        import tools.skills_hub as mod
        importlib.reload(mod)
        mod.ensure_hub_dirs()

        ok, msg = mod.uninstall_skill("does-not-exist")
        assert ok is False
        assert "not a hub-installed skill" in msg
        assert "governed" not in msg.lower()
