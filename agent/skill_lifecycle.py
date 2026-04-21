"""Skill lifecycle inventory, audit, and health helpers.

This module is intentionally tool-agnostic so CLI commands and future pruning
phases can reuse the same inventory + heuristic layer.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.skill_utils import (
    SKILL_LAST_USED_AT_NEVER,
    SKILL_STATUS_ARCHIVED,
    SKILL_STATUS_DEPRECATED,
    get_category_from_skill_path,
    iter_all_skill_index_files,
    normalize_skill_lifecycle_fields,
    parse_frontmatter,
    parse_skill_lifecycle_timestamp,
)

DEFAULT_STALE_DAYS = 90
DEFAULT_BLOAT_LINES = 220
DEFAULT_BLOAT_TOKENS = 3500
DEFAULT_BLOAT_PITFALLS = 10
DEFAULT_DUPLICATE_RATIO = 0.86

_TOKEN_RE = re.compile(r"[a-z0-9]{4,}")
_PITFALL_RE = re.compile(r"(?im)^(?:#+\s*)?pitfall(?:\b|:)")
_DATED_PITFALL_RE = re.compile(r"(?im)^.*(?:pitfall).*\b20\d{2}-\d{2}-\d{2}\b.*$")
_HEADING_RE = re.compile(r"(?m)^#+\s+")
_STOPWORDS = {
    "about",
    "after",
    "again",
    "agent",
    "against",
    "allow",
    "allows",
    "also",
    "always",
    "before",
    "being",
    "build",
    "check",
    "create",
    "default",
    "during",
    "ensure",
    "from",
    "have",
    "into",
    "just",
    "line",
    "must",
    "needed",
    "note",
    "only",
    "path",
    "phase",
    "return",
    "should",
    "skills",
    "skill",
    "that",
    "their",
    "them",
    "then",
    "there",
    "these",
    "this",
    "through",
    "tool",
    "tools",
    "update",
    "when",
    "with",
    "without",
    "your",
}


def _first_non_header_line(body: str) -> str:
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def _normalize_similarity_text(name: str, description: str, body: str) -> str:
    excerpt = body[:4000]
    text = " ".join(part for part in (name, description, excerpt) if part)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[^a-z0-9\s]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _similarity_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(text.lower()):
        if token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= 40:
            break
    return tokens


def _normalized_body_hash(body: str) -> str:
    normalized = re.sub(r"\s+", " ", body).strip().lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _coerce_notability_score(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_skill_record(skill_md: Path) -> Dict[str, Any]:
    raw_content = skill_md.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(raw_content)
    normalized_frontmatter, _, missing_fields = normalize_skill_lifecycle_fields(
        frontmatter,
    )

    name = str(normalized_frontmatter.get("name") or skill_md.parent.name)
    description = str(
        normalized_frontmatter.get("description") or _first_non_header_line(body)
    ).strip()
    similarity_text = _normalize_similarity_text(name, description, body)
    last_used_at = str(normalized_frontmatter.get("last_used_at") or SKILL_LAST_USED_AT_NEVER)

    return {
        "name": name,
        "path": str(skill_md),
        "category": get_category_from_skill_path(skill_md),
        "description": description,
        "created_at": str(normalized_frontmatter.get("created_at") or ""),
        "last_used_at": last_used_at,
        "source_session_ids": list(normalized_frontmatter.get("source_session_ids") or []),
        "status": str(normalized_frontmatter.get("status") or "active"),
        "notability_score": _coerce_notability_score(
            normalized_frontmatter.get("notability_score")
        ),
        "metadata_missing_fields": missing_fields,
        "line_count": len(raw_content.splitlines()),
        "char_count": len(raw_content),
        "estimated_tokens": len(raw_content) // 4,
        "heading_count": len(_HEADING_RE.findall(body)),
        "pitfall_count": len(_PITFALL_RE.findall(body)),
        "dated_pitfall_count": len(_DATED_PITFALL_RE.findall(body)),
        "content_hash": _normalized_body_hash(body),
        "similarity_text": similarity_text,
        "similarity_tokens": _similarity_tokens(similarity_text),
        "parse_error": None,
    }


def collect_skill_inventory() -> List[Dict[str, Any]]:
    """Scan installed skill files and return normalized lifecycle records."""
    records: List[Dict[str, Any]] = []
    for skill_md in iter_all_skill_index_files("SKILL.md"):
        try:
            records.append(_build_skill_record(skill_md))
        except Exception as exc:  # pragma: no cover - defensive guard
            records.append(
                {
                    "name": skill_md.parent.name,
                    "path": str(skill_md),
                    "category": get_category_from_skill_path(skill_md),
                    "description": "",
                    "created_at": "",
                    "last_used_at": "",
                    "source_session_ids": [],
                    "status": "active",
                    "notability_score": None,
                    "metadata_missing_fields": [],
                    "line_count": 0,
                    "char_count": 0,
                    "estimated_tokens": 0,
                    "heading_count": 0,
                    "pitfall_count": 0,
                    "dated_pitfall_count": 0,
                    "content_hash": "",
                    "similarity_text": "",
                    "similarity_tokens": [],
                    "parse_error": str(exc),
                }
            )
    records.sort(key=lambda item: ((item.get("category") or ""), item["name"], item["path"]))
    return records


def _token_overlap(left: Dict[str, Any], right: Dict[str, Any]) -> float:
    left_tokens = set(left.get("similarity_tokens") or [])
    right_tokens = set(right.get("similarity_tokens") or [])
    if not left_tokens or not right_tokens:
        return 0.0
    union = left_tokens | right_tokens
    if not union:
        return 0.0
    return len(left_tokens & right_tokens) / len(union)


def find_duplicate_skills(
    records: List[Dict[str, Any]],
    *,
    duplicate_ratio: float = DEFAULT_DUPLICATE_RATIO,
) -> Dict[str, Any]:
    """Find exact and near-duplicate skill candidates."""
    exact_groups: List[Dict[str, Any]] = []
    by_hash: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        if record.get("parse_error") or not record.get("content_hash"):
            continue
        by_hash.setdefault(record["content_hash"], []).append(record)

    for content_hash, items in by_hash.items():
        if len(items) < 2:
            continue
        exact_groups.append(
            {
                "content_hash": content_hash,
                "count": len(items),
                "skills": [
                    {"name": item["name"], "path": item["path"], "category": item.get("category")}
                    for item in items
                ],
            }
        )

    near_pairs: List[Dict[str, Any]] = []
    for index, left in enumerate(records):
        if left.get("parse_error"):
            continue
        for right in records[index + 1 :]:
            if right.get("parse_error"):
                continue
            if left.get("content_hash") and left.get("content_hash") == right.get("content_hash"):
                continue

            name_ratio = SequenceMatcher(
                None,
                left["name"].lower(),
                right["name"].lower(),
            ).ratio()
            token_overlap = _token_overlap(left, right)
            text_ratio = 0.0
            if name_ratio >= 0.55 or token_overlap >= 0.25:
                text_ratio = SequenceMatcher(
                    None,
                    left.get("similarity_text", ""),
                    right.get("similarity_text", ""),
                ).ratio()

            if not (
                text_ratio >= duplicate_ratio
                or token_overlap >= 0.70
                or (name_ratio >= 0.82 and token_overlap >= 0.25)
            ):
                continue

            score = max(text_ratio, token_overlap, name_ratio)
            near_pairs.append(
                {
                    "left": {"name": left["name"], "path": left["path"]},
                    "right": {"name": right["name"], "path": right["path"]},
                    "score": round(score, 3),
                    "name_similarity": round(name_ratio, 3),
                    "token_overlap": round(token_overlap, 3),
                    "text_similarity": round(text_ratio, 3),
                }
            )

    near_pairs.sort(key=lambda item: item["score"], reverse=True)
    return {"exact_groups": exact_groups, "near_pairs": near_pairs}


def build_skill_audit(
    records: List[Dict[str, Any]],
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    bloat_lines: int = DEFAULT_BLOAT_LINES,
    bloat_tokens: int = DEFAULT_BLOAT_TOKENS,
    bloat_pitfalls: int = DEFAULT_BLOAT_PITFALLS,
    duplicate_ratio: float = DEFAULT_DUPLICATE_RATIO,
) -> Dict[str, Any]:
    """Build a read-only lifecycle audit from skill inventory records."""
    stale: List[Dict[str, Any]] = []
    dead: List[Dict[str, Any]] = []
    unobserved: List[Dict[str, Any]] = []
    metadata_gaps: List[Dict[str, Any]] = []
    bloated: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []

    for record in records:
        if record.get("parse_error"):
            parse_errors.append(
                {
                    "name": record["name"],
                    "path": record["path"],
                    "error": record["parse_error"],
                }
            )
            continue

        if record.get("metadata_missing_fields"):
            metadata_gaps.append(
                {
                    "name": record["name"],
                    "path": record["path"],
                    "missing_fields": list(record["metadata_missing_fields"]),
                }
            )

        status = record.get("status") or "active"
        if status in {SKILL_STATUS_DEPRECATED, SKILL_STATUS_ARCHIVED}:
            dead.append(
                {
                    "name": record["name"],
                    "path": record["path"],
                    "status": status,
                }
            )

        last_used_at = record.get("last_used_at") or ""
        if last_used_at == SKILL_LAST_USED_AT_NEVER:
            unobserved.append(
                {
                    "name": record["name"],
                    "path": record["path"],
                    "status": status,
                }
            )
        else:
            last_used_dt = parse_skill_lifecycle_timestamp(last_used_at)
            if last_used_dt is not None:
                age_days = (
                    datetime.now(last_used_dt.tzinfo or timezone.utc) - last_used_dt
                )
                if age_days.days > stale_days and status not in {
                    SKILL_STATUS_DEPRECATED,
                    SKILL_STATUS_ARCHIVED,
                }:
                    stale.append(
                        {
                            "name": record["name"],
                            "path": record["path"],
                            "last_used_at": last_used_at,
                            "age_days": age_days.days,
                            "status": status,
                        }
                    )

        reasons: List[str] = []
        if record["line_count"] > bloat_lines:
            reasons.append(f"{record['line_count']} lines > {bloat_lines}")
        if record["estimated_tokens"] > bloat_tokens:
            reasons.append(f"~{record['estimated_tokens']} tokens > {bloat_tokens}")
        if record["pitfall_count"] >= bloat_pitfalls:
            reasons.append(f"{record['pitfall_count']} pitfall headings")
        if record["dated_pitfall_count"] >= max(3, bloat_pitfalls // 2):
            reasons.append(f"{record['dated_pitfall_count']} dated pitfall entries")
        if reasons:
            bloated.append(
                {
                    "name": record["name"],
                    "path": record["path"],
                    "line_count": record["line_count"],
                    "estimated_tokens": record["estimated_tokens"],
                    "pitfall_count": record["pitfall_count"],
                    "reasons": reasons,
                }
            )

    duplicates = find_duplicate_skills(records, duplicate_ratio=duplicate_ratio)
    summary = {
        "total_skills": len(records),
        "dead": len(dead),
        "stale": len(stale),
        "unobserved": len(unobserved),
        "metadata_gaps": len(metadata_gaps),
        "bloated": len(bloated),
        "parse_errors": len(parse_errors),
        "exact_duplicate_groups": len(duplicates["exact_groups"]),
        "near_duplicate_pairs": len(duplicates["near_pairs"]),
        "metadata_coverage_pct": round(
            100.0 * (len(records) - len(metadata_gaps)) / len(records),
            1,
        )
        if records
        else 100.0,
        "stale_days_threshold": stale_days,
    }

    return {
        "summary": summary,
        "dead": dead,
        "stale": stale,
        "unobserved": unobserved,
        "metadata_gaps": metadata_gaps,
        "bloated": bloated,
        "duplicates": duplicates,
        "parse_errors": parse_errors,
        "records": records,
    }


def build_skill_health_report(
    records: List[Dict[str, Any]],
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    bloat_lines: int = DEFAULT_BLOAT_LINES,
    bloat_tokens: int = DEFAULT_BLOAT_TOKENS,
    bloat_pitfalls: int = DEFAULT_BLOAT_PITFALLS,
    duplicate_ratio: float = DEFAULT_DUPLICATE_RATIO,
) -> Dict[str, Any]:
    """Summarize overall skill health from the shared audit model."""
    audit = build_skill_audit(
        records,
        stale_days=stale_days,
        bloat_lines=bloat_lines,
        bloat_tokens=bloat_tokens,
        bloat_pitfalls=bloat_pitfalls,
        duplicate_ratio=duplicate_ratio,
    )
    summary = audit["summary"]

    if (
        summary["parse_errors"]
        or summary["dead"]
        or summary["exact_duplicate_groups"]
        or summary["near_duplicate_pairs"]
        or summary["bloated"]
    ):
        overall_status = "needs-attention"
    elif summary["stale"] or summary["metadata_gaps"] or summary["unobserved"]:
        overall_status = "watch"
    else:
        overall_status = "healthy"

    top_issues: List[str] = []
    if summary["dead"]:
        top_issues.append(f"{summary['dead']} deprecated/archived skill(s)")
    if summary["exact_duplicate_groups"] or summary["near_duplicate_pairs"]:
        dup_count = summary["exact_duplicate_groups"] + summary["near_duplicate_pairs"]
        top_issues.append(f"{dup_count} duplicate candidate group(s)")
    if summary["bloated"]:
        top_issues.append(f"{summary['bloated']} bloated skill(s)")
    if summary["stale"]:
        top_issues.append(f"{summary['stale']} stale skill(s) over {stale_days}d")
    if summary["metadata_gaps"]:
        top_issues.append(f"{summary['metadata_gaps']} skill(s) still missing lifecycle metadata")
    if summary["unobserved"]:
        top_issues.append(f"{summary['unobserved']} skill(s) have no observed usage yet")
    if not top_issues:
        top_issues.append("No audit findings above the configured thresholds")

    return {
        "overall_status": overall_status,
        "summary": summary,
        "top_issues": top_issues,
        "audit": audit,
    }
