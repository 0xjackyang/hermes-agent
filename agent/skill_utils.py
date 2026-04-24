"""Lightweight skill metadata utilities shared by prompt_builder and skills_tool.

This module intentionally avoids importing the tool registry, CLI config, or any
heavy dependency chain.  It is safe to import at module level without triggering
tool registration or provider resolution.
"""

import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# ── Platform mapping ──────────────────────────────────────────────────────

PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}

EXCLUDED_SKILL_DIRS = frozenset((".git", ".github", ".hub"))

# ── Lazy YAML loader ─────────────────────────────────────────────────────

_yaml_load_fn = None
_yaml_dump_fn = None


def yaml_load(content: str):
    """Parse YAML with lazy import and CSafeLoader preference."""
    global _yaml_load_fn
    if _yaml_load_fn is None:
        import yaml

        loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader

        def _load(value: str):
            return yaml.load(value, Loader=loader)

        _yaml_load_fn = _load
    return _yaml_load_fn(content)


def yaml_dump(value: Dict[str, Any]) -> str:
    """Dump YAML with lazy import and stable key order."""
    global _yaml_dump_fn
    if _yaml_dump_fn is None:
        import yaml

        dumper = getattr(yaml, "CSafeDumper", None) or yaml.SafeDumper

        def _dump(payload: Dict[str, Any]) -> str:
            return yaml.dump(
                payload,
                Dumper=dumper,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )

        _yaml_dump_fn = _dump
    return _yaml_dump_fn(value)


# ── Frontmatter parsing ──────────────────────────────────────────────────


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string.

    Uses yaml with CSafeLoader for full YAML support (nested metadata, lists)
    with a fallback to simple key:value splitting for robustness.

    Returns:
        (frontmatter_dict, remaining_body)
    """
    frontmatter: Dict[str, Any] = {}
    body = content

    if not content.startswith("---"):
        return frontmatter, body

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body

    yaml_content = content[3 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]

    try:
        parsed = yaml_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        # Fallback: simple key:value parsing for malformed YAML
        for line in yaml_content.strip().split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

    return frontmatter, body


# ── Lifecycle metadata ────────────────────────────────────────────────────

SKILL_CREATED_AT_UNKNOWN = "unknown"
SKILL_LAST_USED_AT_NEVER = "never"
SKILL_STATUS_ACTIVE = "active"
SKILL_STATUS_STALE = "stale"
SKILL_STATUS_DEPRECATED = "deprecated"
SKILL_STATUS_ARCHIVED = "archived"
VALID_SKILL_STATUSES = frozenset(
    {
        SKILL_STATUS_ACTIVE,
        SKILL_STATUS_STALE,
        SKILL_STATUS_DEPRECATED,
        SKILL_STATUS_ARCHIVED,
    }
)


def skill_timestamp_now(now: Optional[datetime] = None) -> str:
    """Return an ISO-8601 UTC timestamp for skill metadata fields."""
    current = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    return current.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_skill_lifecycle_timestamp(value: Any) -> Optional[datetime]:
    """Parse a lifecycle timestamp, returning None for sentinel values."""
    if value in (None, "", SKILL_CREATED_AT_UNKNOWN, SKILL_LAST_USED_AT_NEVER):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _coerce_source_session_ids(value: Any) -> List[str]:
    """Normalize source_session_ids to a clean string list."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def normalize_skill_lifecycle_fields(
    frontmatter: Dict[str, Any],
    *,
    fallback_frontmatter: Optional[Dict[str, Any]] = None,
    created_at_default: str = SKILL_CREATED_AT_UNKNOWN,
    last_used_default: str = SKILL_LAST_USED_AT_NEVER,
    source_session_id: Optional[str] = None,
    mark_used: bool = False,
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], bool, List[str]]:
    """Ensure lifecycle metadata fields exist with stable defaults.

    Returns ``(updated_frontmatter, changed, missing_fields_before_fill)``.
    Missing values are seeded from ``fallback_frontmatter`` when available,
    otherwise from the provided defaults.
    """
    fallback_frontmatter = fallback_frontmatter or {}
    updated = dict(frontmatter)
    changed = False
    missing_fields: List[str] = []

    def _preferred_value(key: str):
        current = updated.get(key)
        if current not in (None, "", []):
            return current
        return fallback_frontmatter.get(key)

    created_at = _preferred_value("created_at")
    if created_at in (None, ""):
        created_at = created_at_default
        missing_fields.append("created_at")
    if updated.get("created_at") != created_at:
        updated["created_at"] = created_at
        changed = True

    if mark_used:
        last_used_at = skill_timestamp_now(now)
    else:
        last_used_at = _preferred_value("last_used_at")
        if last_used_at in (None, ""):
            last_used_at = last_used_default
            missing_fields.append("last_used_at")
    if updated.get("last_used_at") != last_used_at:
        updated["last_used_at"] = last_used_at
        changed = True

    session_ids = _coerce_source_session_ids(_preferred_value("source_session_ids"))
    if not session_ids:
        missing_fields.append("source_session_ids")
    if source_session_id and source_session_id not in session_ids:
        session_ids.append(source_session_id)
    if updated.get("source_session_ids") != session_ids:
        updated["source_session_ids"] = session_ids
        changed = True

    status = _preferred_value("status")
    normalized_status = str(status).strip().lower() if status not in (None, "") else ""
    if not normalized_status:
        normalized_status = SKILL_STATUS_ACTIVE
        missing_fields.append("status")
    if updated.get("status") != normalized_status:
        updated["status"] = normalized_status
        changed = True

    if "notability_score" not in updated and "notability_score" in fallback_frontmatter:
        updated["notability_score"] = fallback_frontmatter["notability_score"]
        changed = True

    return updated, changed, missing_fields


def serialize_skill_content(frontmatter: Dict[str, Any], body: str) -> str:
    """Render a SKILL.md document from parsed frontmatter + markdown body."""
    yaml_content = yaml_dump(frontmatter).strip()
    stripped_body = body.lstrip("\n")
    if stripped_body:
        return f"---\n{yaml_content}\n---\n\n{stripped_body}"
    return f"---\n{yaml_content}\n---\n"


def apply_skill_lifecycle_to_content(
    content: str,
    *,
    fallback_frontmatter: Optional[Dict[str, Any]] = None,
    created_at_default: str = SKILL_CREATED_AT_UNKNOWN,
    last_used_default: str = SKILL_LAST_USED_AT_NEVER,
    source_session_id: Optional[str] = None,
    mark_used: bool = False,
    now: Optional[datetime] = None,
) -> Tuple[str, Dict[str, Any], bool, List[str]]:
    """Apply lifecycle metadata defaults to a raw SKILL.md document."""
    if not content.startswith("---"):
        return content, {}, False, []

    frontmatter, body = parse_frontmatter(content)
    updated_frontmatter, changed, missing_fields = normalize_skill_lifecycle_fields(
        frontmatter,
        fallback_frontmatter=fallback_frontmatter,
        created_at_default=created_at_default,
        last_used_default=last_used_default,
        source_session_id=source_session_id,
        mark_used=mark_used,
        now=now,
    )
    if not changed:
        return content, updated_frontmatter, False, missing_fields
    return (
        serialize_skill_content(updated_frontmatter, body),
        updated_frontmatter,
        True,
        missing_fields,
    )


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically rewrite a text file in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.tmp.",
        suffix="",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            logger.debug("Failed to clean up temporary skill metadata file %s", temp_path, exc_info=True)
        raise


PROFILE_PROTECTED_BRANCHES = frozenset({"main", "master"})


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _git_repo_root(path_text: str) -> str:
    if not path_text:
        return ""
    proc = subprocess.run(
        ["git", "-C", path_text, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _git_current_branch(repo_root_text: str) -> str:
    if not repo_root_text:
        return ""
    proc = subprocess.run(
        ["git", "-C", repo_root_text, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _git_is_tracked(repo_root_text: str, relative_path_text: str) -> bool:
    if not repo_root_text or not relative_path_text:
        return False
    proc = subprocess.run(
        ["git", "-C", repo_root_text, "ls-files", "--error-unmatch", "--", relative_path_text],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def get_live_governed_skill_surface_context(target_path: Path) -> Dict[str, Any]:
    """Describe whether ``target_path`` lands on a live governed profile skill surface.

    The live governed surface is the current default/named profile checkout rooted at
    ``HERMES_HOME`` when it is itself a git repo on a protected base branch.  In that
    state, tracked skill paths are treated as governed canonical surfaces for bounded
    startup sync / seeding decisions.
    """

    hermes_home = get_hermes_home().expanduser().resolve()
    default_home = (Path.home() / ".hermes").expanduser().resolve()
    skills_root = (hermes_home / "skills").resolve()
    resolved_target = target_path.expanduser().resolve()

    context: Dict[str, Any] = {
        "applies": False,
        "live_profile_base": False,
        "tracked": False,
        "hermes_home": str(hermes_home),
        "skills_root": str(skills_root),
        "repo_root": "",
        "branch": "",
        "relative_path": "",
        "reason": "outside_profile_skills",
    }

    if not _path_is_relative_to(resolved_target, skills_root):
        return context

    context["relative_path"] = str(resolved_target.relative_to(hermes_home))

    if hermes_home.parent.name != "profiles" and hermes_home != default_home:
        context["reason"] = "hermes_home_not_live_profile_home"
        return context

    repo_root_text = _git_repo_root(str(hermes_home))
    if not repo_root_text:
        context["reason"] = "hermes_home_not_git_repo"
        return context

    repo_root = Path(repo_root_text)
    context["repo_root"] = repo_root_text
    if repo_root != hermes_home:
        context["reason"] = "hermes_home_not_repo_root"
        return context

    branch = _git_current_branch(repo_root_text)
    context["branch"] = branch
    context["applies"] = True
    context["tracked"] = _git_is_tracked(repo_root_text, context["relative_path"])

    if branch not in PROFILE_PROTECTED_BRANCHES:
        context["reason"] = "profile_repo_not_on_live_base_branch"
        return context

    context["live_profile_base"] = True
    context["reason"] = "live_profile_base"
    return context


def sync_skill_lifecycle_metadata(
    skill_file: Path,
    *,
    fallback_frontmatter: Optional[Dict[str, Any]] = None,
    created_at_default: str = SKILL_CREATED_AT_UNKNOWN,
    last_used_default: str = SKILL_LAST_USED_AT_NEVER,
    source_session_id: Optional[str] = None,
    mark_used: bool = False,
    persist: bool = True,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Normalize lifecycle metadata for a skill file and optionally persist it."""
    raw_content = skill_file.read_text(encoding="utf-8")
    new_content, frontmatter, changed, missing_fields = apply_skill_lifecycle_to_content(
        raw_content,
        fallback_frontmatter=fallback_frontmatter,
        created_at_default=created_at_default,
        last_used_default=last_used_default,
        source_session_id=source_session_id,
        mark_used=mark_used,
        now=now,
    )
    persisted = False
    if changed and persist:
        # Phase 3-C.1 governance gate: skip writeback on governed tracked files.
        # Lazy-import to break circular dep (skill_surface already imports us).
        from agent.skill_surface import is_governed_target as _is_governed_target
        if _is_governed_target(skill_file):
            logger.debug(
                "skill lifecycle writeback deferred: governed target %s "
                "(Phase 3-C.1 gate; see agent.skill_surface.is_governed_target)",
                skill_file,
            )
        else:
            try:
                _atomic_write_text(skill_file, new_content)
                persisted = True
            except Exception:
                logger.debug("Could not persist lifecycle metadata for %s", skill_file, exc_info=True)
    return {
        "content": new_content if changed else raw_content,
        "frontmatter": frontmatter,
        "changed": changed,
        "persisted": persisted,
        "metadata_missing_fields": missing_fields,
    }


# ── Platform matching ─────────────────────────────────────────────────────


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """Return True when the skill is compatible with the current OS.

    Skills declare platform requirements via a top-level ``platforms`` list
    in their YAML frontmatter::

        platforms: [macos]          # macOS only
        platforms: [macos, linux]   # macOS and Linux

    If the field is absent or empty the skill is compatible with **all**
    platforms (backward-compatible default).
    """
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = sys.platform
    for platform in platforms:
        normalized = str(platform).lower().strip()
        mapped = PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
    return False


# ── Disabled skills ───────────────────────────────────────────────────────


def get_disabled_skill_names(platform: str | None = None) -> Set[str]:
    """Read disabled skill names from config.yaml.

    Args:
        platform: Explicit platform name (e.g. ``"telegram"``).  When
            *None*, resolves from ``HERMES_PLATFORM`` or
            ``HERMES_SESSION_PLATFORM`` env vars.  Falls back to the
            global disabled list when no platform is determined.

    Reads the config file directly (no CLI config imports) to stay
    lightweight.
    """
    config_path = get_hermes_home() / "config.yaml"
    if not config_path.exists():
        return set()
    try:
        parsed = yaml_load(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Could not read skill config %s: %s", config_path, e)
        return set()
    if not isinstance(parsed, dict):
        return set()

    skills_cfg = parsed.get("skills")
    if not isinstance(skills_cfg, dict):
        return set()

    resolved_platform = (
        platform
        or os.getenv("HERMES_PLATFORM")
        or os.getenv("HERMES_SESSION_PLATFORM")
    )
    if resolved_platform:
        platform_disabled = (skills_cfg.get("platform_disabled") or {}).get(
            resolved_platform
        )
        if platform_disabled is not None:
            return _normalize_string_set(platform_disabled)
    return _normalize_string_set(skills_cfg.get("disabled"))


def _normalize_string_set(values) -> Set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    return {str(v).strip() for v in values if str(v).strip()}


# ── External skills directories ──────────────────────────────────────────


def get_external_skills_dirs() -> List[Path]:
    """Read ``skills.external_dirs`` from config.yaml and return validated paths.

    Each entry is expanded (``~`` and ``${VAR}``) and resolved to an absolute
    path.  Only directories that actually exist are returned.  Duplicates and
    paths that resolve to the local ``~/.hermes/skills/`` are silently skipped.
    """
    config_path = get_hermes_home() / "config.yaml"
    if not config_path.exists():
        return []
    try:
        parsed = yaml_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []

    skills_cfg = parsed.get("skills")
    if not isinstance(skills_cfg, dict):
        return []

    raw_dirs = skills_cfg.get("external_dirs")
    if not raw_dirs:
        return []
    if isinstance(raw_dirs, str):
        raw_dirs = [raw_dirs]
    if not isinstance(raw_dirs, list):
        return []

    local_skills = (get_hermes_home() / "skills").resolve()
    seen: Set[Path] = set()
    result: List[Path] = []

    for entry in raw_dirs:
        entry = str(entry).strip()
        if not entry:
            continue
        # Expand ~ and environment variables
        expanded = os.path.expanduser(os.path.expandvars(entry))
        p = Path(expanded).resolve()
        if p == local_skills:
            continue
        if p in seen:
            continue
        if p.is_dir():
            seen.add(p)
            result.append(p)
        else:
            logger.debug("External skills dir does not exist, skipping: %s", p)

    return result


def get_all_skills_dirs() -> List[Path]:
    """Return all skill directories: local ``~/.hermes/skills/`` first, then external.

    The local dir is always first (and always included even if it doesn't exist
    yet — callers handle that).  External dirs follow in config order.
    """
    dirs = [get_hermes_home() / "skills"]
    dirs.extend(get_external_skills_dirs())
    return dirs


# ── Condition extraction ──────────────────────────────────────────────────


def extract_skill_conditions(frontmatter: Dict[str, Any]) -> Dict[str, List]:
    """Extract conditional activation fields from parsed frontmatter."""
    metadata = frontmatter.get("metadata")
    # Handle cases where metadata is not a dict (e.g., a string from malformed YAML)
    if not isinstance(metadata, dict):
        metadata = {}
    hermes = metadata.get("hermes") or {}
    if not isinstance(hermes, dict):
        hermes = {}
    return {
        "fallback_for_toolsets": hermes.get("fallback_for_toolsets", []),
        "requires_toolsets": hermes.get("requires_toolsets", []),
        "fallback_for_tools": hermes.get("fallback_for_tools", []),
        "requires_tools": hermes.get("requires_tools", []),
    }


# ── Skill config extraction ───────────────────────────────────────────────


def extract_skill_config_vars(frontmatter: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract config variable declarations from parsed frontmatter.

    Skills declare config.yaml settings they need via::

        metadata:
          hermes:
            config:
              - key: wiki.path
                description: Path to the LLM Wiki knowledge base directory
                default: "~/wiki"
                prompt: Wiki directory path

    Returns a list of dicts with keys: ``key``, ``description``, ``default``,
    ``prompt``.  Invalid or incomplete entries are silently skipped.
    """
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return []
    hermes = metadata.get("hermes")
    if not isinstance(hermes, dict):
        return []
    raw = hermes.get("config")
    if not raw:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    result: List[Dict[str, Any]] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key or key in seen:
            continue
        # Must have at least key and description
        desc = str(item.get("description", "")).strip()
        if not desc:
            continue
        entry: Dict[str, Any] = {
            "key": key,
            "description": desc,
        }
        default = item.get("default")
        if default is not None:
            entry["default"] = default
        prompt_text = item.get("prompt")
        if isinstance(prompt_text, str) and prompt_text.strip():
            entry["prompt"] = prompt_text.strip()
        else:
            entry["prompt"] = desc
        seen.add(key)
        result.append(entry)
    return result


def discover_all_skill_config_vars() -> List[Dict[str, Any]]:
    """Scan all enabled skills and collect their config variable declarations.

    Walks every skills directory, parses each SKILL.md frontmatter, and returns
    a deduplicated list of config var dicts.  Each dict also includes a
    ``skill`` key with the skill name for attribution.

    Disabled and platform-incompatible skills are excluded.
    """
    all_vars: List[Dict[str, Any]] = []
    seen_keys: set = set()

    disabled = get_disabled_skill_names()
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.is_dir():
            continue
        for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
            try:
                raw = skill_file.read_text(encoding="utf-8")
                frontmatter, _ = parse_frontmatter(raw)
            except Exception:
                continue

            skill_name = frontmatter.get("name") or skill_file.parent.name
            if str(skill_name) in disabled:
                continue
            if not skill_matches_platform(frontmatter):
                continue

            config_vars = extract_skill_config_vars(frontmatter)
            for var in config_vars:
                if var["key"] not in seen_keys:
                    var["skill"] = str(skill_name)
                    all_vars.append(var)
                    seen_keys.add(var["key"])

    return all_vars


# Storage prefix: all skill config vars are stored under skills.config.*
# in config.yaml.  Skill authors declare logical keys (e.g. "wiki.path");
# the system adds this prefix for storage and strips it for display.
SKILL_CONFIG_PREFIX = "skills.config"


def _resolve_dotpath(config: Dict[str, Any], dotted_key: str):
    """Walk a nested dict following a dotted key.  Returns None if any part is missing."""
    parts = dotted_key.split(".")
    current = config
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def resolve_skill_config_values(
    config_vars: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve current values for skill config vars from config.yaml.

    Skill config is stored under ``skills.config.<key>`` in config.yaml.
    Returns a dict mapping **logical** keys (as declared by skills) to their
    current values (or the declared default if the key isn't set).
    Path values are expanded via ``os.path.expanduser``.
    """
    config_path = get_hermes_home() / "config.yaml"
    config: Dict[str, Any] = {}
    if config_path.exists():
        try:
            parsed = yaml_load(config_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                config = parsed
        except Exception:
            pass

    resolved: Dict[str, Any] = {}
    for var in config_vars:
        logical_key = var["key"]
        storage_key = f"{SKILL_CONFIG_PREFIX}.{logical_key}"
        value = _resolve_dotpath(config, storage_key)

        if value is None or (isinstance(value, str) and not value.strip()):
            value = var.get("default", "")

        # Expand ~ in path-like values
        if isinstance(value, str) and ("~" in value or "${" in value):
            value = os.path.expanduser(os.path.expandvars(value))

        resolved[logical_key] = value

    return resolved


# ── Description extraction ────────────────────────────────────────────────


def extract_skill_description(frontmatter: Dict[str, Any]) -> str:
    """Extract a truncated description from parsed frontmatter."""
    raw_desc = frontmatter.get("description", "")
    if not raw_desc:
        return ""
    desc = str(raw_desc).strip().strip("'\"")
    if len(desc) > 60:
        return desc[:57] + "..."
    return desc


# ── File iteration ────────────────────────────────────────────────────────


def iter_skill_index_files(skills_dir: Path, filename: str):
    """Walk skills_dir yielding sorted paths matching *filename*.

    Excludes ``.git``, ``.github``, ``.hub`` directories.
    """
    matches = []
    for root, dirs, files in os.walk(skills_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS]
        if filename in files:
            matches.append(Path(root) / filename)
    for path in sorted(matches, key=lambda p: str(p.relative_to(skills_dir))):
        yield path


def iter_all_skill_index_files(filename: str = "SKILL.md"):
    """Yield skill index files from the local skills dir and configured externals."""
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.is_dir():
            continue
        yield from iter_skill_index_files(skills_dir, filename)


# ── Category helpers ───────────────────────────────────────────────────────


def get_category_from_skill_path(
    skill_path: Path,
    *,
    skills_dirs: Optional[List[Path]] = None,
) -> Optional[str]:
    """Extract the category segment from a skill path when present."""
    dirs_to_check = skills_dirs or get_all_skills_dirs()
    for skills_dir in dirs_to_check:
        try:
            rel_path = skill_path.relative_to(skills_dir)
            parts = rel_path.parts
            if len(parts) >= 3:
                return parts[0]
        except ValueError:
            continue
    return None
