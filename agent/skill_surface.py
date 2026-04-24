"""Centralized skill-surface resolver (Phase 3-A, 2026-04-24).

Canonical source of truth for "where does this skill read/write go?" across
the Hermes codebase. Replaces piecemeal `SKILLS_DIR = HERMES_HOME / "skills"`
duplication in ``tools/skills_sync.py``, ``tools/skill_manager_tool.py``,
``tools/skills_hub.py``, ``tools/skills_tool.py`` by promoting the existing
``agent/skill_utils.py`` helpers into a dedicated module with an explicit
operator-intent API.

Precedence (Phase 3 design target):

* **Read**: runtime-local masks canonical. Callers walk ``all_read_roots()``
  first-hit-wins. The runtime-local dir is always yielded first.
* **Write**: ordinary operations land under ``runtime_local_skill_root()``.
  Governed canonical surfaces (live profile base on a protected branch)
  are discovery/promote targets, NOT ambient write targets. Callers gate
  writes on ``is_governed_target(path)``; if True and ``op != "promote"``
  the write must be deferred or rejected per Phase 3 governance.

This module is intentionally additive in Phase 3-A. Consumer migration
lands in Phase 3-B (reads) and 3-C (writes). See closure-plan sub-packet
split recorded in runtime-ops.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from hermes_constants import get_hermes_home

# Re-export existing helpers so consumers have one import surface once
# Phase 3-B/3-C migration completes. These stay in skill_utils as well
# for backwards compatibility (deprecation shim).
from agent.skill_utils import (
    EXCLUDED_SKILL_DIRS,
    PROFILE_PROTECTED_BRANCHES,
    get_all_skills_dirs as _get_all_skills_dirs,
    get_external_skills_dirs as _get_external_skills_dirs,
    get_live_governed_skill_surface_context,
)

__all__ = [
    "Op",
    "EXCLUDED_SKILL_DIRS",
    "PROFILE_PROTECTED_BRANCHES",
    "runtime_local_skill_root",
    "hub_root",
    "optional_skills_root",
    "external_skill_roots",
    "all_read_roots",
    "resolve_skill_write",
    "is_governed_target",
    "is_canonical_write_allowed",
    "get_live_governed_skill_surface_context",
]


# Operator-intent taxonomy. Every skill write call-site must declare its
# intent so the resolver can apply governance. Phase 3-C migrates existing
# write sites onto these tokens.
Op = Literal[
    "discover",              # read-only scan; never writes (included for symmetry)
    "self_learn_metadata",   # lifecycle frontmatter writeback (skill_utils.py:427 today)
    "agent_create",          # agent-authored skill create/edit via skill_manager_tool
    "hub_install",           # skills_hub install from tap/registry
    "startup_sync",          # skills_sync.py bundled → runtime-local copy on boot
    "profile_seed",          # profile create/setup skill seeding
    "promote",               # explicit runtime-local → canonical promotion
]

_ORDINARY_OPS: frozenset[Op] = frozenset(
    {"self_learn_metadata", "agent_create", "hub_install",
     "startup_sync", "profile_seed"}
)


def runtime_local_skill_root() -> Path:
    """Return the per-profile writable skill root (``HERMES_HOME/"skills"``).

    This is the single place ordinary self-learning / install / seed writes
    should land. Resolves HERMES_HOME per-call (no module-level caching) so
    subprocess-respawn patterns (see ``hermes_cli/profiles.py::seed_profile_skills``)
    see a fresh value.
    """
    return get_hermes_home() / "skills"


def hub_root() -> Path:
    """Return the skills-hub subspace (``<runtime-local>/.hub``)."""
    return runtime_local_skill_root() / ".hub"


def optional_skills_root() -> Path | None:
    """Return the repo-bundled optional-skills root if discoverable.

    Returns None if ``hermes_constants.get_optional_skills_dir`` is not
    available in this install layout. Read-only surface.
    """
    try:
        from hermes_constants import get_optional_skills_dir  # type: ignore[attr-defined]
    except Exception:
        return None
    try:
        result = get_optional_skills_dir()
    except Exception:
        return None
    return Path(result) if result else None


def external_skill_roots() -> list[Path]:
    """User-configured external skill directories (read-only)."""
    return _get_external_skills_dirs()


def all_read_roots() -> list[Path]:
    """All skill read roots in precedence order (runtime-local first)."""
    return _get_all_skills_dirs()


def is_governed_target(path: Path) -> bool:
    """True when ``path`` lands on a live governed profile skill surface.

    A "governed" surface is a HERMES_HOME that is itself a git repo whose
    repo-root == HERMES_HOME and current branch is in
    ``PROFILE_PROTECTED_BRANCHES``. In that state, tracked skill files
    under ``HERMES_HOME/skills`` are canonical (discovery/promote only,
    not ordinary-write targets).

    Wraps ``get_live_governed_skill_surface_context`` for a boolean answer.
    Use the context dict directly when you need richer diagnostics
    (branch, repo_root, reason).
    """
    ctx = get_live_governed_skill_surface_context(path)
    return bool(ctx.get("live_profile_base"))


def is_canonical_write_allowed(op: Op) -> bool:
    """True only for explicit ``"promote"`` operations."""
    return op == "promote"


def resolve_skill_write(slug: str, op: Op) -> Path:
    """Return the intended write path for ``slug`` under operator intent ``op``.

    Ordinary ops always resolve under ``runtime_local_skill_root()``.
    ``slug`` may contain forward-slashes for category/name layouts used
    by skills_hub (e.g. ``"gateway/bounded-interruption"``).

    The resolver does NOT actively reject governed writes here — it
    returns the intended target so callers can gate on
    ``is_governed_target(path)`` before writing. Phase 3-C migrates
    consumers to use this explicitly; actively-raising semantics can be
    added once all call-sites declare their ``Op``.
    """
    slug = slug.strip().strip("/")
    if not slug:
        raise ValueError("slug must be non-empty after stripping slashes")
    if op not in _ORDINARY_OPS and op != "promote" and op != "discover":
        raise ValueError(f"unknown Op: {op!r}")
    return runtime_local_skill_root() / slug
