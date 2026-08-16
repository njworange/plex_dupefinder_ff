from __future__ import annotations

import json
from typing import Dict, List, Set

from .models import ModelDuplicateGroup, ModelMediaCandidate
from .services.safety import normalize_remote_path


def candidate_paths(candidate: ModelMediaCandidate) -> List[str]:
    try:
        parts = json.loads(candidate.parts_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(parts, list):
        return []
    return [
        str(part["file"])
        for part in parts
        if isinstance(part, dict) and part.get("file")
    ]


def cross_group_path_conflicts(run_id: int) -> Set[int]:
    """Find groups whose active Part path is owned by another metadata group."""
    path_owners: Dict[str, Set[int]] = {}
    for group in ModelDuplicateGroup.all_by_run(run_id):
        for candidate in ModelMediaCandidate.by_group(group.id, include_deleted=False):
            for path in candidate_paths(candidate):
                normalized = normalize_remote_path(path)
                if normalized:
                    path_owners.setdefault(normalized, set()).add(group.id)
    conflicts: Set[int] = set()
    for owners in path_owners.values():
        if len(owners) > 1:
            conflicts.update(owners)
    return conflicts


def group_has_cross_path_conflict(run_id: int, group_id: int) -> bool:
    return int(group_id) in cross_group_path_conflicts(int(run_id))
