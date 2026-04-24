#!/usr/bin/env python3
"""
Skills Sync -- Manifest-based seeding and updating of bundled skills.

Copies bundled skills from the repo's skills/ directory into ~/.hermes/skills/
and uses a manifest to track which skills have been synced and their origin hash.

Manifest format:
  - v2: each line is "skill_name:origin_hash" where origin_hash is the MD5
    of the bundled skill at the time it was last synced to the user dir.
  - v3: each line may append pipe-delimited flags after the hash as
    "skill_name:origin_hash|flag1,flag2" for bounded seeding metadata.
Old v1 manifests (plain names without hashes) are auto-migrated.

Update logic:
  - NEW skills (not in manifest): copied to user dir, origin hash recorded.
  - EXISTING skills (in manifest, present in user dir):
      * If user copy matches origin hash: user hasn't modified it → safe to
        update from bundled if bundled changed. New origin hash recorded.
      * If user copy differs from origin hash: user customized it → SKIP.
  - DELETED by user (in manifest, absent from user dir): respected, not re-added.
  - REMOVED from bundled (in manifest, gone from repo): cleaned from manifest.

The manifest lives at ~/.hermes/skills/.bundled_manifest.
"""

import hashlib
import logging
import os
import shutil
from pathlib import Path
from hermes_constants import get_hermes_home
from agent.skill_surface import runtime_local_skill_root
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION = "protected_custom_collision"


HERMES_HOME = get_hermes_home()
SKILLS_DIR = runtime_local_skill_root()  # Phase 3-B: resolver-backed
MANIFEST_FILE = SKILLS_DIR / ".bundled_manifest"


def _get_bundled_dir() -> Path:
    """Locate the bundled skills/ directory.

    Checks HERMES_BUNDLED_SKILLS env var first (set by Nix wrapper),
    then falls back to the relative path from this source file.
    """
    env_override = os.getenv("HERMES_BUNDLED_SKILLS")
    if env_override:
        return Path(env_override)
    return Path(__file__).parent.parent / "skills"


def _read_manifest() -> Dict[str, str]:
    """
    Read the manifest as a dict of {skill_name: encoded_origin_record}.

    Handles v1 (plain names), v2 (name:hash), and v3 (name:hash|flags)
    formats. v1 entries get an empty hash string which triggers migration on
    next sync.
    """
    if not MANIFEST_FILE.exists():
        return {}
    try:
        result = {}
        for line in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                # v2 format: name:hash
                name, _, hash_val = line.partition(":")
                result[name.strip()] = hash_val.strip()
            else:
                # v1 format: plain name — empty hash triggers migration
                result[line] = ""
        return result
    except (OSError, IOError):
        return {}


def _write_manifest(entries: Dict[str, str]):
    """Write the manifest file atomically in encoded manifest format.

    Uses a temp file + os.replace() to avoid corruption if the process
    crashes or is interrupted mid-write.
    """
    import tempfile

    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(f"{name}:{hash_val}" for name, hash_val in sorted(entries.items())) + "\n"

    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(MANIFEST_FILE.parent),
            prefix=".bundled_manifest_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, MANIFEST_FILE)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("Failed to write skills manifest %s: %s", MANIFEST_FILE, e, exc_info=True)


def _discover_bundled_skills(bundled_dir: Path) -> List[Tuple[str, Path]]:
    """
    Find all SKILL.md files in the bundled directory.
    Returns list of (skill_name, skill_directory_path) tuples.
    """
    skills = []
    if not bundled_dir.exists():
        return skills

    for skill_md in bundled_dir.rglob("SKILL.md"):
        path_str = str(skill_md)
        if "/.git/" in path_str or "/.github/" in path_str or "/.hub/" in path_str:
            continue
        skill_dir = skill_md.parent
        skill_name = skill_dir.name
        skills.append((skill_name, skill_dir))

    return skills


def _compute_relative_dest(skill_dir: Path, bundled_dir: Path) -> Path:
    """
    Compute the destination path in SKILLS_DIR preserving the category structure.
    e.g., bundled/skills/mlops/axolotl -> ~/.hermes/skills/mlops/axolotl
    """
    rel = skill_dir.relative_to(bundled_dir)
    return SKILLS_DIR / rel


def _dir_hash(directory: Path) -> str:
    """Compute a hash of all file contents in a directory for change detection."""
    hasher = hashlib.md5()
    try:
        for fpath in sorted(directory.rglob("*")):
            if fpath.is_file():
                rel = fpath.relative_to(directory)
                hasher.update(str(rel).encode("utf-8"))
                hasher.update(fpath.read_bytes())
    except (OSError, IOError):
        pass
    return hasher.hexdigest()


def _parse_manifest_entry(entry: str) -> tuple[str, set[str]]:
    """Decode a manifest entry into its origin hash and any bounded flags."""

    normalized = (entry or "").strip()
    if not normalized:
        return "", set()

    origin_hash, sep, flags_blob = normalized.partition("|")
    if not sep:
        return origin_hash.strip(), set()

    flags = {flag.strip() for flag in flags_blob.split(",") if flag.strip()}
    return origin_hash.strip(), flags


def _format_manifest_entry(origin_hash: str, *, flags: set[str] | None = None) -> str:
    """Encode an origin hash and optional bounded flags for manifest storage."""

    normalized_hash = (origin_hash or "").strip()
    if not normalized_hash:
        return ""

    normalized_flags = sorted(
        {flag.strip() for flag in (flags or set()) if flag and flag.strip()}
    )
    if not normalized_flags:
        return normalized_hash
    return f"{normalized_hash}|{','.join(normalized_flags)}"


def _get_live_governed_skill_surface_context(skill_dir: Path) -> dict:
    """Resolve governed-surface context for a bundled skill destination lazily."""
    try:
        from agent.skill_utils import get_live_governed_skill_surface_context

        return get_live_governed_skill_surface_context(skill_dir / "SKILL.md")
    except Exception:
        logger.debug("Could not resolve governed skill-surface context for %s", skill_dir, exc_info=True)
        return {
            "applies": False,
            "live_profile_base": False,
            "tracked": False,
            "reason": "context_lookup_failed",
        }


def _is_live_governed_tracked_skill_surface(skill_dir: Path) -> bool:
    context = _get_live_governed_skill_surface_context(skill_dir)
    return bool(context.get("live_profile_base") and context.get("tracked"))


def _preserve_tracked_collision_off_live_base(manifest_flags: set[str], context: dict) -> bool:
    """Keep protected tracked custom-skill collisions from later auto-overwrite."""

    return bool(
        MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION in manifest_flags
        and context.get("applies")
        and context.get("tracked")
    )


def sync_skills(quiet: bool = False) -> dict:
    """
    Sync bundled skills into ~/.hermes/skills/ using the manifest.

    Returns:
        dict with keys: copied (list), updated (list), governed_skipped (list),
                        skipped (int), user_modified (list), cleaned (list),
                        total_bundled (int)
    """
    bundled_dir = _get_bundled_dir()
    if not bundled_dir.exists():
        return {
            "copied": [], "updated": [], "governed_skipped": [], "skipped": 0,
            "user_modified": [], "cleaned": [], "total_bundled": 0,
        }

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest()
    bundled_skills = _discover_bundled_skills(bundled_dir)
    bundled_names = {name for name, _ in bundled_skills}

    copied = []
    updated = []
    governed_skipped = []
    user_modified = []
    skipped = 0

    for skill_name, skill_src in bundled_skills:
        dest = _compute_relative_dest(skill_src, bundled_dir)
        bundled_hash = _dir_hash(skill_src)

        if skill_name not in manifest:
            # ── New skill — never offered before ──
            try:
                if dest.exists():
                    # User already has a skill with the same name — don't overwrite.
                    # On a live governed tracked surface, record the current on-disk
                    # hash as a protected collision baseline so future branch-local
                    # syncs do not later overwrite the tracked custom skill.
                    skipped += 1
                    if _is_live_governed_tracked_skill_surface(dest):
                        manifest[skill_name] = _format_manifest_entry(
                            _dir_hash(dest),
                            flags={MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION},
                        )
                    else:
                        manifest[skill_name] = _format_manifest_entry(bundled_hash)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(skill_src, dest)
                    copied.append(skill_name)
                    manifest[skill_name] = _format_manifest_entry(bundled_hash)
                    if not quiet:
                        print(f"  + {skill_name}")
            except (OSError, IOError) as e:
                if not quiet:
                    print(f"  ! Failed to copy {skill_name}: {e}")
                # Do NOT add to manifest — next sync should retry

        elif dest.exists():
            # ── Existing skill — in manifest AND on disk ──
            origin_hash, manifest_flags = _parse_manifest_entry(manifest.get(skill_name, ""))
            user_hash = _dir_hash(dest)
            skill_surface_context = None

            if not origin_hash:
                # v1 migration: no origin hash recorded. Set baseline from
                # user's current copy so future syncs can detect modifications.
                manifest[skill_name] = _format_manifest_entry(user_hash)
                if user_hash == bundled_hash:
                    skipped += 1  # already in sync
                else:
                    # Can't tell if user modified or bundled changed — be safe
                    skipped += 1
                continue

            if user_hash != origin_hash:
                # User modified this skill — don't overwrite their changes
                user_modified.append(skill_name)
                if not quiet:
                    print(f"  ~ {skill_name} (user-modified, skipping)")
                continue

            if MANIFEST_FLAG_PROTECTED_CUSTOM_COLLISION in manifest_flags:
                skill_surface_context = _get_live_governed_skill_surface_context(dest)
                if bundled_hash == origin_hash:
                    manifest[skill_name] = _format_manifest_entry(
                        bundled_hash,
                        flags=manifest_flags,
                    )
                elif _preserve_tracked_collision_off_live_base(
                    manifest_flags,
                    skill_surface_context,
                ):
                    skipped += 1
                    if skill_surface_context.get("live_profile_base"):
                        governed_skipped.append(skill_name)
                        if not quiet:
                            print(f"  = {skill_name} (live governed surface, keeping tracked custom skill)")
                    elif not quiet:
                        print(f"  = {skill_name} (tracked custom collision baseline, keeping)")
                    continue

            # User copy matches origin — check if bundled has a newer version
            if bundled_hash != origin_hash:
                if skill_surface_context is None:
                    skill_surface_context = _get_live_governed_skill_surface_context(dest)
                if skill_surface_context.get("live_profile_base") and skill_surface_context.get("tracked"):
                    governed_skipped.append(skill_name)
                    skipped += 1
                    if not quiet:
                        print(f"  = {skill_name} (live governed surface, skipping update)")
                    continue
                try:
                    # Move old copy to a backup so we can restore on failure
                    backup = dest.with_suffix(".bak")
                    shutil.move(str(dest), str(backup))
                    try:
                        shutil.copytree(skill_src, dest)
                        manifest[skill_name] = _format_manifest_entry(bundled_hash)
                        updated.append(skill_name)
                        if not quiet:
                            print(f"  ↑ {skill_name} (updated)")
                        # Remove backup after successful copy
                        shutil.rmtree(backup, ignore_errors=True)
                    except (OSError, IOError):
                        # Restore from backup
                        if backup.exists() and not dest.exists():
                            shutil.move(str(backup), str(dest))
                        raise
                except (OSError, IOError) as e:
                    if not quiet:
                        print(f"  ! Failed to update {skill_name}: {e}")
            else:
                skipped += 1  # bundled unchanged, user unchanged

        else:
            # ── In manifest but not on disk — user deleted it ──
            skipped += 1

    # Clean stale manifest entries (skills removed from bundled dir)
    cleaned = sorted(set(manifest.keys()) - bundled_names)
    for name in cleaned:
        del manifest[name]

    # Also copy DESCRIPTION.md files for categories (if not already present)
    for desc_md in bundled_dir.rglob("DESCRIPTION.md"):
        rel = desc_md.relative_to(bundled_dir)
        dest_desc = SKILLS_DIR / rel
        if not dest_desc.exists():
            try:
                dest_desc.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(desc_md, dest_desc)
            except (OSError, IOError) as e:
                logger.debug("Could not copy %s: %s", desc_md, e)

    _write_manifest(manifest)

    return {
        "copied": copied,
        "updated": updated,
        "governed_skipped": governed_skipped,
        "skipped": skipped,
        "user_modified": user_modified,
        "cleaned": cleaned,
        "total_bundled": len(bundled_skills),
    }


if __name__ == "__main__":
    print("Syncing bundled skills into ~/.hermes/skills/ ...")
    result = sync_skills(quiet=False)
    parts = [
        f"{len(result['copied'])} new",
        f"{len(result['updated'])} updated",
        f"{result['skipped']} unchanged",
    ]
    if result["user_modified"]:
        parts.append(f"{len(result['user_modified'])} user-modified (kept)")
    if result["governed_skipped"]:
        parts.append(f"{len(result['governed_skipped'])} governed tracked updates skipped")
    if result["cleaned"]:
        parts.append(f"{len(result['cleaned'])} cleaned from manifest")
    print(f"\nDone: {', '.join(parts)}. {result['total_bundled']} total bundled.")
