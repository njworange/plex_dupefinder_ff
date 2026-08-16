from __future__ import annotations

import posixpath
from typing import Any, Iterable, List, Sequence

from .safety import (
    is_absolute_remote_path,
    normalize_remote_path,
    path_within_root,
)


def _locations(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for value in values or ():
        normalized = normalize_remote_path(str(value or ""))
        if normalized != "/" and not (
            len(normalized) == 3 and normalized[1:] == ":/"
        ):
            normalized = normalized.rstrip("/")
        if not normalized or not is_absolute_remote_path(normalized):
            continue
        if normalized not in result:
            result.append(normalized)
    return sorted(result, key=len, reverse=True)


def _candidate_paths(candidate: Any, current_item: Any) -> List[str]:
    media_id = str(getattr(candidate, "media_id", "") or "")
    for version in getattr(current_item, "media", ()) or ():
        if str(getattr(version, "media_id", "") or "") != media_id:
            continue
        return [str(getattr(part, "file", "") or "") for part in version.parts]
    return []


def build_scan_targets(
    group: Any,
    candidate: Any,
    current_item: Any,
    section_locations: Sequence[str],
) -> List[str]:
    """Return exact partial-scan directories for a confirmed delete.

    The calculation intentionally happens from the live pre-DELETE Metadata
    snapshot.  A movie scans its containing movie folder.  An episode is
    promoted to the show folder (the first component below the selected Plex
    section Location), matching Plex's TV partial-scan behavior.  Any missing,
    relative, section-root, or out-of-section path rejects the whole plan; a
    broad or partial fallback is less safe than leaving the delete blocked.
    """

    locations = _locations(section_locations)
    paths = _candidate_paths(candidate, current_item)
    media_type = str(getattr(group, "media_type", "") or "")
    if media_type not in ("movie", "episode") or not locations or not paths:
        return []

    targets: List[str] = []
    for raw_path in paths:
        if not raw_path or not is_absolute_remote_path(raw_path):
            return []
        path = normalize_remote_path(raw_path)
        roots = [root for root in locations if path_within_root(path, root)]
        if not roots:
            return []
        root = roots[0]
        parent = posixpath.dirname(path)
        if not parent or parent == root:
            # A path directly under a section root would turn a supposedly
            # targeted refresh into a whole-library scan.
            return []

        if media_type == "movie":
            target = parent
        else:
            relative = path[len(root) :].lstrip("/")
            components = [part for part in relative.split("/") if part]
            if len(components) < 2:
                return []
            target = posixpath.join(root, components[0])

        if target == root or not path_within_root(target, root):
            return []
        if target not in targets:
            targets.append(target)
    return targets


def validate_scan_target(
    path: str,
    section_locations: Sequence[str],
    allowed_roots: Sequence[str],
) -> bool:
    normalized = normalize_remote_path(path)
    locations = _locations(section_locations)
    roots = _locations(allowed_roots)
    if not normalized or not is_absolute_remote_path(normalized):
        return False
    if normalized in locations or not any(
        path_within_root(normalized, location) for location in locations
    ):
        return False
    if not roots or not any(path_within_root(normalized, root) for root in roots):
        return False
    return True
