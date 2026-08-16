from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .domain import MediaVersion, MetadataItem, SafetyResult


def normalize_remote_path(value: str) -> str:
    raw = (value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    # Preserve a Windows drive while normalizing separators and dot segments.
    drive_match = re.match(r"^([A-Za-z]):(/.*)?$", raw)
    if drive_match:
        suffix = posixpath.normpath(drive_match.group(2) or "/")
        return (drive_match.group(1).lower() + ":" + suffix).casefold()
    if raw.startswith("//"):
        return posixpath.normpath(raw).casefold()
    # Plex paths on Linux are case-sensitive; never broaden an allow-root by case folding.
    return posixpath.normpath(raw)


def is_absolute_remote_path(value: str) -> bool:
    raw = (value or "").strip().replace("\\", "/")
    if not raw:
        return False
    return raw.startswith("/") or bool(re.match(r"^[A-Za-z]:/", raw))


def path_within_root(path: str, root: str) -> bool:
    normalized_path = normalize_remote_path(path)
    normalized_root = normalize_remote_path(root).rstrip("/")
    if not normalized_path or not normalized_root:
        return False
    return normalized_path == normalized_root or normalized_path.startswith(normalized_root + "/")


@dataclass(frozen=True)
class SafetyPolicy:
    allowed_roots: Tuple[str, ...] = ()
    require_guid: bool = True
    block_multipart: bool = True
    require_allowed_roots: bool = True


def assess_group(item: MetadataItem, policy: SafetyPolicy) -> SafetyResult:
    flags: List[str] = []
    details: Dict[str, Any] = {}

    if item.media_type not in ("movie", "episode"):
        flags.append("unsupported_media_type")
    if len(item.media) < 2:
        flags.append("less_than_two_versions")
    if policy.require_guid and not item.guid:
        flags.append("missing_guid")
    if item.media_type == "episode" and (
        not item.grandparent_rating_key or item.parent_index is None or item.index is None
    ):
        flags.append("missing_episode_identity")

    media_ids = [version.media_id for version in item.media]
    if any(not media_id for media_id in media_ids):
        flags.append("missing_media_id")
    if len(media_ids) != len(set(media_ids)):
        flags.append("duplicate_media_id")

    invalid_roots = [root for root in policy.allowed_roots if not is_absolute_remote_path(root)]
    if invalid_roots:
        flags.append("invalid_allowed_root")
        details["invalid_allowed_roots"] = sorted(set(invalid_roots))

    path_owners: Dict[str, List[str]] = {}
    outside_roots: List[str] = []
    non_absolute_paths: List[str] = []
    for version in item.media:
        if not version.parts or any(not part.file for part in version.parts):
            flags.append("missing_file_path")
        if policy.block_multipart and len(version.parts) > 1:
            flags.append("multipart_version")
        for path in version.paths:
            normalized = normalize_remote_path(path)
            path_owners.setdefault(normalized, []).append(version.media_id)
            if not is_absolute_remote_path(path):
                non_absolute_paths.append(path)
            if policy.require_allowed_roots and (
                not policy.allowed_roots or not any(path_within_root(path, root) for root in policy.allowed_roots)
            ):
                outside_roots.append(path)

    shared = {path: owners for path, owners in path_owners.items() if path and len(set(owners)) > 1}
    if shared:
        flags.append("shared_file_path")
        details["shared_paths"] = sorted(shared.keys())
    if outside_roots:
        flags.append("path_outside_allowed_roots")
        details["outside_roots"] = sorted(set(outside_roots))
    if non_absolute_paths:
        flags.append("non_absolute_file_path")
        details["non_absolute_paths"] = sorted(set(non_absolute_paths))

    unique_flags = tuple(dict.fromkeys(flags))
    return SafetyResult(safe=not unique_flags, flags=unique_flags, details=details)


def snapshot_fingerprints(item: MetadataItem) -> Dict[str, str]:
    return {version.media_id: version.fingerprint() for version in item.media}


def validate_fresh_snapshot(
    current: MetadataItem,
    expected_identity_fingerprint: str,
    expected_media_fingerprints: Mapping[str, str],
) -> SafetyResult:
    flags: List[str] = []
    details: Dict[str, Any] = {}
    if current.identity_fingerprint() != expected_identity_fingerprint:
        flags.append("metadata_identity_changed")

    current_fingerprints = snapshot_fingerprints(current)
    if set(current_fingerprints) != set(expected_media_fingerprints):
        flags.append("media_set_changed")
        details["expected_media_ids"] = sorted(expected_media_fingerprints)
        details["current_media_ids"] = sorted(current_fingerprints)
    else:
        changed = sorted(
            media_id
            for media_id, fingerprint in expected_media_fingerprints.items()
            if current_fingerprints.get(media_id) != fingerprint
        )
        if changed:
            flags.append("media_snapshot_changed")
            details["changed_media_ids"] = changed

    unique_flags = tuple(dict.fromkeys(flags))
    return SafetyResult(safe=not unique_flags, flags=unique_flags, details=details)
