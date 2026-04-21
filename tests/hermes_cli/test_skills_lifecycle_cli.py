from io import StringIO

from rich.console import Console

from hermes_cli.skills_hub import do_audit, do_health


def _write_skill(skills_dir, name, frontmatter_extra="", body="Do the thing."):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Description for {name}.\n{frontmatter_extra}---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


def _capture(fn, *args, **kwargs) -> str:
    sink = StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None)
    fn(*args, console=console, **kwargs)
    return sink.getvalue()


def test_do_audit_and_health_surface_stale_duplicate_and_bloat(tmp_path, monkeypatch):
    stale_meta = (
        "created_at: 2025-01-01T00:00:00Z\n"
        "last_used_at: 2025-01-02T00:00:00Z\n"
        "source_session_ids: [seed-session]\n"
        "status: active\n"
    )
    fresh_meta = (
        "created_at: 2026-01-01T00:00:00Z\n"
        "last_used_at: 2026-04-20T00:00:00Z\n"
        "source_session_ids: [seed-session]\n"
        "status: active\n"
    )
    bloated_body = "\n".join([f"## Pitfall: 2026-04-21 case {idx}" for idx in range(12)])

    _write_skill(tmp_path, "stale-skill", frontmatter_extra=stale_meta)
    _write_skill(tmp_path, "duplicate-one", frontmatter_extra=fresh_meta, body="Same body for duplicate detection.")
    _write_skill(tmp_path, "duplicate-two", frontmatter_extra=fresh_meta, body="Same body for duplicate detection.")
    _write_skill(tmp_path, "bloated-skill", frontmatter_extra=fresh_meta, body=bloated_body)

    monkeypatch.setattr("agent.skill_utils.get_all_skills_dirs", lambda: [tmp_path])

    audit_output = _capture(do_audit, stale_days=30)
    assert "Skill Lifecycle Audit" in audit_output
    assert "Stale skills" in audit_output
    assert "Bloated skills" in audit_output
    assert "Exact duplicate groups" in audit_output
    assert "duplicate-one" in audit_output
    assert "bloated-skill" in audit_output

    health_output = _capture(do_health, stale_days=30)
    assert "Skill Health" in health_output
    assert "needs-attention" in health_output
    assert "duplicate candidate" in health_output or "duplicate" in health_output
