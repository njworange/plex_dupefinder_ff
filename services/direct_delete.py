from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

from .quarantine_delete import (
    DirectorySnapshot,
    FileSnapshot,
    QuarantinePlanError,
    SubtitleDecision,
    SUPPORTED_SUBTITLE_EXTENSIONS,
    UNSUPPORTED_SUBTITLE_EXTENSIONS,
    _canonical,
    _decision_snapshot,
    _directory_snapshot,
    _has_reparse_component,
    _sorted_decisions,
    _stat_file,
    _subtitle_directories,
    _tag_matches,
    _video_files,
    _within,
)


class DirectDeletePlanError(QuarantinePlanError):
    """A filesystem delete plan could not be proven safe."""


@dataclass(frozen=True)
class DirectDeletePlan:
    video: FileSnapshot
    survivors: Tuple[FileSnapshot, ...]
    eligible: Tuple[SubtitleDecision, ...]
    excluded: Tuple[SubtitleDecision, ...]
    protected: Tuple[SubtitleDecision, ...]
    allowed_roots: Tuple[str, ...]
    section_locations: Tuple[str, ...]
    watched_directories: Tuple[DirectorySnapshot, ...]
    scan_mode: str
    plan_digest: str

    @property
    def blocking(self) -> Tuple[SubtitleDecision, ...]:
        return tuple(
            item
            for item in self.excluded
            if str(item.reason).startswith("required_backup_unavailable:")
        )

    def as_api(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "backend": "direct",
            "status": "blocked" if self.blocking else "planned",
            "executable": not bool(self.blocking),
            "video": {"path": self.video.path, "size": self.video.size},
            "eligible": [item.as_dict(False) for item in self.eligible],
            "excluded": [item.as_dict(False) for item in self.excluded],
            "counts": {
                "eligible": len(self.eligible),
                "excluded": len(self.excluded),
                "protected": len(self.protected),
                "deleted": 0,
                "blocking": len(self.blocking),
            },
            "plan_digest": self.plan_digest,
        }

    def public_dict(self) -> Dict[str, Any]:
        aliases = {
            "shared_with_surviving_or_sibling_video": "ambiguous_owner",
            "symlink_or_reparse_not_safe": "symlink",
            "hardlink_not_safe": "hardlink",
            "subtitle_name_not_exclusive": "ambiguous_owner",
        }

        def value(item: SubtitleDecision, excluded: bool) -> Dict[str, Any]:
            reason = aliases.get(item.reason, item.reason)
            result = {
                "path": item.path,
                "source_path": item.path,
                "reason": reason,
            }
            if excluded:
                result["reason_code"] = reason
            return result

        payload = self.as_api()
        payload["included_subtitles"] = [value(item, False) for item in self.eligible]
        payload["excluded_subtitles"] = [value(item, True) for item in self.excluded]
        payload["protected"] = [value(item, True) for item in self.protected]
        payload["protected_subtitles"] = list(payload["protected"])
        payload["blocking"] = [value(item, True) for item in self.blocking]
        return payload

    def manifest_dict(self) -> Dict[str, Any]:
        return {
            "backend": "direct",
            "execution_strategy": "plex_media_delete_sidecar_v1",
            "scan_mode": self.scan_mode,
            "video": self.video.as_dict(),
            "survivors": [value.as_dict() for value in self.survivors],
            "eligible": [item.as_dict(True) for item in self.eligible],
            "excluded": [item.as_dict(True) for item in self.excluded],
            "protected": [item.as_dict(True) for item in self.protected],
            "allowed_roots": list(self.allowed_roots),
            "section_locations": list(self.section_locations),
            "watched_directories": [
                value.as_dict() for value in self.watched_directories
            ],
            "plan_digest": self.plan_digest,
        }


def _digest(value: Dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot_identity(value: FileSnapshot) -> Tuple[int, int]:
    return int(value.device), int(value.inode)


def build_direct_delete_plan(
    delete_paths: Sequence[str],
    survivor_paths: Sequence[str],
    allowed_roots: Sequence[str],
    section_locations: Sequence[str],
    scan_mode: str,
) -> DirectDeletePlan:
    """Build an immutable, exact-ownership plan without mutating media files."""

    target_paths = tuple(delete_paths)
    if len(target_paths) != 1:
        raise DirectDeletePlanError("직접 삭제는 단일 Part 영상만 지원합니다.")
    raw_video = str(target_paths[0] or "")
    if not raw_video or not os.path.isabs(raw_video):
        raise DirectDeletePlanError("삭제 대상 영상은 로컬 절대 경로여야 합니다.")
    mode = str(scan_mode or "").strip().lower()
    if mode not in ("binary", "web"):
        raise DirectDeletePlanError("직접 삭제는 Binary 또는 Web 부분 스캔이 필수입니다.")

    video = _stat_file(raw_video, False)
    roots = tuple(_canonical(path) for path in allowed_roots if str(path or "").strip())
    locations = tuple(
        _canonical(path) for path in section_locations if str(path or "").strip()
    )
    if not roots or not any(_within(video.path, root) for root in roots):
        raise DirectDeletePlanError("삭제 대상 영상이 허용 미디어 루트 밖입니다.")
    if not locations or not any(_within(video.path, root) for root in locations):
        raise DirectDeletePlanError("삭제 대상 영상이 Plex library Location 밖입니다.")

    survivor_snapshots = tuple(_stat_file(path, False) for path in survivor_paths)
    video_identity = _snapshot_identity(video)
    if any(
        survivor.path == video.path or _snapshot_identity(survivor) == video_identity
        for survivor in survivor_snapshots
    ):
        raise DirectDeletePlanError(
            "삭제 Media와 유지 Media가 같은 영상 파일을 공유하므로 직접 삭제할 수 없습니다."
        )
    if any(
        not any(_within(survivor.path, root) for root in roots)
        or not any(_within(survivor.path, root) for root in locations)
        for survivor in survivor_snapshots
    ):
        raise DirectDeletePlanError("유지 영상 경로를 허용 루트와 Plex Location에서 확인할 수 없습니다.")

    explicit = [video.path] + [item.path for item in survivor_snapshots]
    survivor_set = {item.path for item in survivor_snapshots}
    delete_directory = _canonical(os.path.dirname(video.path))
    directories: Set[str] = set(_subtitle_directories(delete_directory))
    for survivor_path in survivor_set:
        directories.update(
            _subtitle_directories(_canonical(os.path.dirname(survivor_path)))
        )
    inventory_before = tuple(
        _directory_snapshot(directory) for directory in sorted(directories)
    )

    eligible: List[SubtitleDecision] = []
    excluded: List[SubtitleDecision] = []
    protected: List[SubtitleDecision] = []
    seen: Set[str] = set()
    for directory in sorted(directories):
        if _has_reparse_component(directory):
            if _within(directory, delete_directory):
                excluded.append(
                    SubtitleDecision(directory, "subtitle_directory_reparse_point")
                )
            continue
        owner_directory = directory
        if os.path.basename(directory).lower() in ("subs", "subtitles"):
            owner_directory = os.path.dirname(directory)
        owners = _video_files(owner_directory, explicit)
        owner_stems = [
            (path, os.path.splitext(os.path.basename(path))[0]) for path in owners
        ]
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            path = os.path.normpath(os.path.abspath(os.path.join(directory, name)))
            if path in seen:
                continue
            extension = os.path.splitext(name)[1].lower()
            if extension not in (
                SUPPORTED_SUBTITLE_EXTENSIONS | UNSUPPORTED_SUBTITLE_EXTENSIONS
            ):
                continue
            subtitle_stem = os.path.splitext(name)[0]
            matched = {
                owner_path
                for owner_path, owner_stem in owner_stems
                if _tag_matches(subtitle_stem, owner_stem)
            }
            delete_matches = video.path in matched
            survivor_matches = bool(matched & survivor_set)
            video_stem = os.path.splitext(os.path.basename(video.path))[0]
            candidate_prefix = os.path.normcase(subtitle_stem).startswith(
                os.path.normcase(video_stem + ".")
            ) or os.path.normcase(subtitle_stem) == os.path.normcase(video_stem)
            if not delete_matches and not survivor_matches and not candidate_prefix:
                continue
            seen.add(path)
            snapshot, unsafe_reason = _decision_snapshot(path)
            # Every related sidecar needs a rollback copy before PMS DELETE.
            # This includes target-exclusive files: Plex can remove a subtitle
            # while leaving its Media version present after a failed/unknown
            # request, in which case we must be able to restore it.
            requires_backup = bool(
                delete_matches or survivor_matches or candidate_prefix
            )
            if requires_backup and snapshot is None:
                excluded.append(
                    SubtitleDecision(
                        path,
                        "required_backup_unavailable:%s"
                        % (unsafe_reason or "file_state_unverifiable"),
                    )
                )
                continue
            if extension in UNSUPPORTED_SUBTITLE_EXTENSIONS and candidate_prefix:
                decision = SubtitleDecision(
                    path, "unsupported_or_paired_subtitle_format", snapshot
                )
                excluded.append(decision)
                if snapshot is not None and survivor_matches:
                    protected.append(decision)
                continue
            if delete_matches:
                if unsafe_reason:
                    excluded.append(SubtitleDecision(path, unsafe_reason, snapshot))
                elif snapshot is not None and snapshot.device != video.device:
                    excluded.append(
                        SubtitleDecision(path, "different_filesystem", snapshot)
                    )
                elif matched != {video.path}:
                    decision = SubtitleDecision(
                        path, "shared_with_surviving_or_sibling_video", snapshot
                    )
                    excluded.append(decision)
                    protected.append(decision)
                else:
                    eligible.append(
                        SubtitleDecision(path, "exclusive_to_deleted_video", snapshot)
                    )
            elif candidate_prefix:
                decision = SubtitleDecision(
                    path,
                    "survivor_owned"
                    if survivor_matches
                    else "subtitle_name_not_exclusive",
                    snapshot,
                )
                excluded.append(decision)
                if snapshot is not None:
                    protected.append(decision)
            if survivor_matches and snapshot is not None:
                protected.append(
                    SubtitleDecision(path, "protected_for_surviving_video", snapshot)
                )

    eligible_values = _sorted_decisions(eligible)
    excluded_values = _sorted_decisions(excluded)
    protected_by_path: Dict[str, SubtitleDecision] = {}
    # An exclusion means the plugin must preserve that related sidecar even if
    # PMS removes it as collateral.  Only entries without a safe full snapshot
    # remain blocking and therefore cannot reach execution.
    for value in excluded_values:
        if value.snapshot is not None:
            protected_by_path[value.path] = value
    for value in protected:
        protected_by_path[value.path] = value
    protected_values = _sorted_decisions(protected_by_path.values())

    # No unlink target may alias another target or any protected/surviving file.
    target_values = [video] + [
        item.snapshot for item in eligible_values if item.snapshot is not None
    ]
    target_paths_seen: Set[str] = set()
    target_identities: Set[Tuple[int, int]] = set()
    protected_values_all = list(survivor_snapshots) + [
        item.snapshot for item in protected_values if item.snapshot is not None
    ]
    protected_paths = {item.path for item in protected_values_all}
    protected_identities = {_snapshot_identity(item) for item in protected_values_all}
    for value in target_values:
        key = os.path.normcase(value.path)
        identity = _snapshot_identity(value)
        if (
            key in target_paths_seen
            or identity in target_identities
            or value.path in protected_paths
            or identity in protected_identities
        ):
            raise DirectDeletePlanError("삭제 대상과 유지 파일의 identity가 겹칩니다.")
        target_paths_seen.add(key)
        target_identities.add(identity)

    inventory_after = tuple(
        _directory_snapshot(directory) for directory in sorted(directories)
    )
    if inventory_before != inventory_after:
        raise DirectDeletePlanError(
            "자막 소유권 검사 중 폴더 내용이 변경되었습니다. 다시 사전확인하세요."
        )
    manifest = {
        "backend": "direct",
        "execution_strategy": "plex_media_delete_sidecar_v1",
        "scan_mode": mode,
        "video": video.as_dict(),
        "survivors": [value.as_dict() for value in survivor_snapshots],
        "eligible": [item.as_dict(True) for item in eligible_values],
        "excluded": [item.as_dict(True) for item in excluded_values],
        "protected": [item.as_dict(True) for item in protected_values],
        "allowed_roots": list(roots),
        "section_locations": list(locations),
        "watched_directories": [value.as_dict() for value in inventory_after],
    }
    digest = _digest(manifest)
    return DirectDeletePlan(
        video=video,
        survivors=survivor_snapshots,
        eligible=eligible_values,
        excluded=excluded_values,
        protected=protected_values,
        allowed_roots=roots,
        section_locations=locations,
        watched_directories=inventory_after,
        scan_mode=mode,
        plan_digest=digest,
    )


class DirectDeletePlanner:
    def plan(
        self,
        item: Any,
        delete_media_id: str,
        allowed_roots: Sequence[str],
        section_locations: Sequence[str],
        scan_mode: str,
    ) -> DirectDeletePlan:
        target = None
        survivors: List[str] = []
        for version in tuple(getattr(item, "media", ()) or ()):
            paths = list(getattr(version, "paths", ()) or ())
            if str(getattr(version, "media_id", "")) == str(delete_media_id):
                target = paths
            else:
                survivors.extend(paths)
        if target is None:
            raise DirectDeletePlanError(
                "삭제 대상 Media ID를 현재 Plex 항목에서 찾을 수 없습니다."
            )
        return build_direct_delete_plan(
            target,
            survivors,
            allowed_roots,
            section_locations,
            scan_mode,
        )
