from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


SUPPORTED_SUBTITLE_EXTENSIONS = frozenset((".srt", ".smi", ".ssa", ".ass", ".vtt"))
UNSUPPORTED_SUBTITLE_EXTENSIONS = frozenset((".idx", ".sub", ".sup"))
VIDEO_EXTENSIONS = frozenset(
    (
        ".3g2",
        ".3gp",
        ".asf",
        ".avi",
        ".divx",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".ogm",
        ".ogv",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
    )
)
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2})?$")
_FLAGS = frozenset(("forced", "sdh", "cc"))
_MAX_SUBTITLE_BYTES = 64 * 1024 * 1024
_WINDOWS_REPARSE_POINT = 0x0400


class QuarantinePlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    links: int
    sha256: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "device": self.device,
            "inode": self.inode,
            "links": self.links,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class DirectorySnapshot:
    path: str
    device: int
    inode: int
    entries: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "device": self.device,
            "inode": self.inode,
            "entries": list(self.entries),
        }


@dataclass(frozen=True)
class SubtitleDecision:
    path: str
    reason: str
    snapshot: Optional[FileSnapshot] = None

    def as_dict(self, include_snapshot: bool = False) -> Dict[str, Any]:
        value: Dict[str, Any] = {"path": self.path, "reason": self.reason}
        if include_snapshot and self.snapshot is not None:
            value["snapshot"] = self.snapshot.as_dict()
        return value


@dataclass(frozen=True)
class QuarantinePlan:
    video: FileSnapshot
    eligible: Tuple[SubtitleDecision, ...]
    excluded: Tuple[SubtitleDecision, ...]
    protected: Tuple[SubtitleDecision, ...]
    allowed_roots: Tuple[str, ...]
    section_locations: Tuple[str, ...]
    quarantine_root: str
    quarantine_device: int
    quarantine_inode: int
    watched_directories: Tuple[DirectorySnapshot, ...]
    plan_digest: str

    def as_api(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "backend": "quarantine",
            "status": "planned",
            "video": {"path": self.video.path, "size": self.video.size},
            "eligible": [item.as_dict(False) for item in self.eligible],
            "excluded": [item.as_dict(False) for item in self.excluded],
            "counts": {
                "eligible": len(self.eligible),
                "excluded": len(self.excluded),
                "protected": len(self.protected),
                "quarantined": 0,
            },
            "plan_digest": self.plan_digest,
        }

    def public_dict(self) -> Dict[str, Any]:
        """Backward-compatible UI shape without filesystem identity fields."""

        reason_aliases = {
            "shared_with_surviving_or_sibling_video": "ambiguous_owner",
            "symlink_or_reparse_not_safe": "symlink",
            "hardlink_not_safe": "hardlink",
            "subtitle_name_not_exclusive": "ambiguous_owner",
        }

        def value(item: SubtitleDecision, included: bool) -> Dict[str, Any]:
            reason = reason_aliases.get(item.reason, item.reason)
            result = {
                "path": item.path,
                "source_path": item.path,
                "reason": reason,
            }
            if not included:
                result["reason_code"] = reason
            return result

        payload = self.as_api()
        payload["included_subtitles"] = [value(item, True) for item in self.eligible]
        payload["excluded_subtitles"] = [value(item, False) for item in self.excluded]
        return payload

    def manifest_dict(self) -> Dict[str, Any]:
        return {
            "video": self.video.as_dict(),
            "eligible": [item.as_dict(True) for item in self.eligible],
            "excluded": [item.as_dict(True) for item in self.excluded],
            "protected": [item.as_dict(True) for item in self.protected],
            "allowed_roots": list(self.allowed_roots),
            "section_locations": list(self.section_locations),
            "quarantine_root": self.quarantine_root,
            "quarantine_device": self.quarantine_device,
            "quarantine_inode": self.quarantine_inode,
            "watched_directories": [
                value.as_dict() for value in self.watched_directories
            ],
            "plan_digest": self.plan_digest,
        }


def _canonical(path: str) -> str:
    # Keep the filesystem spelling for audit/UI.  Comparisons use normcase
    # separately so Windows/UNC remains case-insensitive without lowercasing
    # the user-visible path.
    return os.path.realpath(os.path.abspath(str(path)))


def _absolute(path: str) -> str:
    return os.path.normpath(os.path.abspath(str(path)))


def _path_key(path: str) -> str:
    return os.path.normcase(_canonical(path))


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except (OSError, ValueError):
        return False


def _is_reparse(snapshot: os.stat_result) -> bool:
    return bool(getattr(snapshot, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT)


def _has_reparse_component(path: str) -> bool:
    current = os.path.abspath(path)
    while True:
        try:
            value = os.lstat(current)
        except OSError:
            return True
        if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _stat_file(path: str, content_hash: bool) -> FileSnapshot:
    lexical = _absolute(path)
    try:
        before = os.lstat(lexical)
    except OSError as exc:
        raise QuarantinePlanError("파일 상태를 확인할 수 없습니다: %s" % lexical) from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or _is_reparse(before)
        or _has_reparse_component(os.path.dirname(lexical))
    ):
        raise QuarantinePlanError("심볼릭 링크 또는 reparse 경로는 처리할 수 없습니다: %s" % lexical)
    if not stat.S_ISREG(before.st_mode):
        raise QuarantinePlanError("일반 파일만 처리할 수 있습니다: %s" % lexical)
    if int(getattr(before, "st_nlink", 1) or 1) != 1:
        raise QuarantinePlanError("hard link 파일은 처리할 수 없습니다: %s" % lexical)
    if content_hash and int(before.st_size) > _MAX_SUBTITLE_BYTES:
        raise QuarantinePlanError("자막 파일 크기가 안전 한도를 초과했습니다: %s" % lexical)
    canonical = _canonical(lexical)

    digest = ""
    if content_hash:
        hasher = hashlib.sha256()
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lexical, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                int(opened.st_dev) != int(before.st_dev)
                or int(opened.st_ino) != int(before.st_ino)
                or int(opened.st_size) != int(before.st_size)
                or int(getattr(opened, "st_mtime_ns", int(opened.st_mtime * 1e9)))
                != int(getattr(before, "st_mtime_ns", int(before.st_mtime * 1e9)))
            ):
                raise QuarantinePlanError("자막 파일이 검사 중 변경되었습니다: %s" % lexical)
            while True:
                chunk = os.read(descriptor, 128 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
            after = os.fstat(descriptor)
            if (
                int(after.st_size) != int(opened.st_size)
                or int(getattr(after, "st_mtime_ns", int(after.st_mtime * 1e9)))
                != int(getattr(opened, "st_mtime_ns", int(opened.st_mtime * 1e9)))
            ):
                raise QuarantinePlanError("자막 파일이 검사 중 변경되었습니다: %s" % lexical)
            digest = hasher.hexdigest()
        finally:
            os.close(descriptor)

    return FileSnapshot(
        path=canonical,
        size=int(before.st_size),
        mtime_ns=int(getattr(before, "st_mtime_ns", int(before.st_mtime * 1e9))),
        device=int(before.st_dev),
        inode=int(before.st_ino),
        links=int(getattr(before, "st_nlink", 1) or 1),
        sha256=digest,
    )


def snapshot_matches(snapshot: FileSnapshot, verify_hash: bool = False) -> bool:
    try:
        current = _stat_file(snapshot.path, verify_hash or bool(snapshot.sha256))
    except QuarantinePlanError:
        return False
    return (
        current.size == snapshot.size
        and current.mtime_ns == snapshot.mtime_ns
        and current.device == snapshot.device
        and current.inode == snapshot.inode
        and current.links == snapshot.links
        and (not snapshot.sha256 or current.sha256 == snapshot.sha256)
    )


def capture_file_snapshot(path: str, content_hash: bool = False) -> FileSnapshot:
    """Capture a no-follow identity snapshot for recovery journaling."""

    return _stat_file(path, content_hash)


def _tag_matches(subtitle_stem: str, video_stem: str) -> bool:
    if os.path.normcase(subtitle_stem) == os.path.normcase(video_stem):
        return True
    prefix = video_stem + "."
    if not os.path.normcase(subtitle_stem).startswith(os.path.normcase(prefix)):
        return False
    suffix = subtitle_stem[len(prefix) :]
    parts = suffix.split(".") if suffix else []
    if len(parts) == 1:
        return bool(_LANGUAGE.match(parts[0])) or parts[0].lower() in _FLAGS
    if len(parts) == 2:
        return bool(_LANGUAGE.match(parts[0])) and parts[1].lower() in _FLAGS
    return False


def _video_files(directory: str, explicit_paths: Iterable[str]) -> List[str]:
    values: Set[str] = set()
    for path in explicit_paths:
        if _canonical(os.path.dirname(path)) == _canonical(directory):
            values.add(_canonical(path))
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(directory, name)
        if os.path.splitext(name)[1].lower() not in VIDEO_EXTENSIONS:
            continue
        try:
            value = os.lstat(path)
        except OSError:
            raise QuarantinePlanError(
                "같은 폴더의 영상 파일 상태를 확인할 수 없습니다: %s" % path
            ) from None
        if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
            raise QuarantinePlanError(
                "같은 폴더에 영상 symlink/reparse 항목이 있어 자막 소유권을 확정할 수 없습니다."
            )
        if not stat.S_ISREG(value.st_mode):
            raise QuarantinePlanError(
                "같은 폴더에 일반 파일이 아닌 영상 항목이 있습니다."
            )
        values.add(_canonical(path))
    return sorted(values)


def _subtitle_directories(video_directory: str) -> List[str]:
    values = [video_directory]
    for name in ("Subs", "Subtitles"):
        path = os.path.join(video_directory, name)
        if os.path.isdir(path):
            values.append(path)
    return values


def _decision_snapshot(path: str) -> Tuple[Optional[FileSnapshot], Optional[str]]:
    try:
        return _stat_file(path, True), None
    except QuarantinePlanError as exc:
        text = str(exc)
        if "hard link" in text:
            return None, "hardlink_not_safe"
        if "심볼릭" in text or "reparse" in text:
            return None, "symlink_or_reparse_not_safe"
        if "크기가" in text:
            return None, "subtitle_too_large"
        if "일반 파일" in text:
            return None, "not_regular_file"
        return None, "file_state_unverifiable"


def _directory_snapshot(path: str) -> DirectorySnapshot:
    lexical = _absolute(path)
    if _has_reparse_component(lexical):
        raise QuarantinePlanError("자막 소유권 검사 폴더가 안전하지 않습니다: %s" % lexical)
    try:
        value = os.lstat(lexical)
        names = tuple(sorted(os.listdir(lexical)))
    except OSError as exc:
        raise QuarantinePlanError("자막 소유권 검사 폴더를 읽을 수 없습니다: %s" % lexical) from exc
    if (
        not stat.S_ISDIR(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or _is_reparse(value)
    ):
        raise QuarantinePlanError("자막 소유권 검사 경로가 폴더가 아닙니다: %s" % lexical)
    return DirectorySnapshot(
        path=_canonical(lexical),
        device=int(value.st_dev),
        inode=int(value.st_ino),
        entries=names,
    )


def directory_snapshot_matches(
    snapshot: DirectorySnapshot, removed_paths: Sequence[str] = ()
) -> bool:
    try:
        current = _directory_snapshot(snapshot.path)
    except QuarantinePlanError:
        return False
    removed = {
        os.path.basename(path)
        for path in removed_paths
        if os.path.normcase(_canonical(os.path.dirname(path)))
        == os.path.normcase(snapshot.path)
    }
    expected = tuple(name for name in snapshot.entries if name not in removed)
    return (
        current.device == snapshot.device
        and current.inode == snapshot.inode
        and current.entries == expected
    )


def _sorted_decisions(values: Iterable[SubtitleDecision]) -> Tuple[SubtitleDecision, ...]:
    return tuple(sorted(values, key=lambda item: (os.path.normcase(item.path), item.reason)))


def _digest_payload(value: Dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_quarantine_plan(
    delete_paths: Sequence[str],
    survivor_paths: Sequence[str],
    allowed_roots: Sequence[str],
    section_locations: Sequence[str],
    quarantine_root: str,
) -> QuarantinePlan:
    if len(tuple(delete_paths)) != 1:
        raise QuarantinePlanError("안전 격리는 단일 Part 영상만 지원합니다.")
    raw_video = str(tuple(delete_paths)[0] or "")
    if not raw_video or not os.path.isabs(raw_video):
        raise QuarantinePlanError("삭제 대상 영상은 로컬 절대 경로여야 합니다.")
    video = _stat_file(raw_video, False)

    roots = tuple(_canonical(path) for path in allowed_roots if str(path or "").strip())
    if not roots or not any(_within(video.path, root) for root in roots):
        raise QuarantinePlanError("삭제 대상 영상이 허용 미디어 루트 밖입니다.")

    if not quarantine_root or not os.path.isabs(quarantine_root):
        raise QuarantinePlanError("격리 루트는 로컬 절대 경로여야 합니다.")
    quarantine_lexical = _absolute(quarantine_root)
    if (
        not os.path.isdir(quarantine_lexical)
        or _has_reparse_component(quarantine_lexical)
    ):
        raise QuarantinePlanError("격리 루트가 없거나 안전한 일반 디렉터리가 아닙니다.")
    quarantine = _canonical(quarantine_lexical)
    locations = tuple(
        _canonical(path) for path in section_locations if str(path or "").strip()
    )
    if any(_within(quarantine, root) or _within(root, quarantine) for root in locations):
        raise QuarantinePlanError("격리 루트는 Plex library Location 밖이어야 합니다.")
    if any(_within(quarantine, root) or _within(root, quarantine) for root in roots):
        raise QuarantinePlanError("격리 루트는 삭제 허용 미디어 루트 밖이어야 합니다.")
    quarantine_stat = os.stat(quarantine)
    if int(quarantine_stat.st_dev) != video.device:
        raise QuarantinePlanError("영상과 격리 루트가 같은 파일시스템이 아닙니다.")

    survivor_snapshots = [_stat_file(path, False) for path in survivor_paths]
    if any(
        survivor.path == video.path
        or (
            survivor.device == video.device
            and survivor.inode == video.inode
        )
        for survivor in survivor_snapshots
    ):
        raise QuarantinePlanError(
            "삭제 Media와 유지 Media가 같은 영상 파일을 공유하므로 격리할 수 없습니다."
        )
    explicit = [video.path] + [item.path for item in survivor_snapshots]
    survivor_set = set(item.path for item in survivor_snapshots)
    delete_directory = _canonical(os.path.dirname(video.path))
    directories: Set[str] = set(_subtitle_directories(delete_directory))
    for survivor in survivor_set:
        directories.update(_subtitle_directories(_canonical(os.path.dirname(survivor))))
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
        owner_stems = [(path, os.path.splitext(os.path.basename(path))[0]) for path in owners]
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            # Keep the lexical directory entry until lstat has rejected a
            # symlink/reparse point; realpath here would erase that evidence.
            path = _absolute(os.path.join(directory, name))
            if path in seen:
                continue
            extension = os.path.splitext(name)[1].lower()
            if extension not in SUPPORTED_SUBTITLE_EXTENSIONS | UNSUPPORTED_SUBTITLE_EXTENSIONS:
                continue
            subtitle_stem = os.path.splitext(name)[0]
            matched = {
                owner_path
                for owner_path, owner_stem in owner_stems
                if _tag_matches(subtitle_stem, owner_stem)
            }
            delete_matches = video.path in matched
            survivor_matches = bool(matched & survivor_set)
            candidate_prefix = os.path.normcase(subtitle_stem).startswith(
                os.path.normcase(os.path.splitext(os.path.basename(video.path))[0] + ".")
            ) or os.path.normcase(subtitle_stem) == os.path.normcase(
                os.path.splitext(os.path.basename(video.path))[0]
            )
            if not delete_matches and not survivor_matches and not candidate_prefix:
                continue
            seen.add(path)
            snapshot, unsafe_reason = _decision_snapshot(path)
            if extension in UNSUPPORTED_SUBTITLE_EXTENSIONS and candidate_prefix:
                excluded.append(
                    SubtitleDecision(path, "unsupported_or_paired_subtitle_format", snapshot)
                )
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
                excluded.append(
                    SubtitleDecision(
                        path,
                        "survivor_owned" if survivor_matches else "subtitle_name_not_exclusive",
                        snapshot,
                    )
                )
            if survivor_matches and snapshot is not None:
                protected.append(
                    SubtitleDecision(path, "protected_for_surviving_video", snapshot)
                )

    eligible_values = _sorted_decisions(eligible)
    excluded_values = _sorted_decisions(excluded)
    protected_by_path: Dict[str, SubtitleDecision] = {}
    for item in protected:
        protected_by_path[item.path] = item
    protected_values = _sorted_decisions(protected_by_path.values())
    inventory_after = tuple(
        _directory_snapshot(directory) for directory in sorted(directories)
    )
    if inventory_before != inventory_after:
        raise QuarantinePlanError(
            "자막 소유권 검사 중 폴더 내용이 변경되었습니다. 다시 사전확인하세요."
        )
    manifest = {
        "video": video.as_dict(),
        "eligible": [item.as_dict(True) for item in eligible_values],
        "excluded": [item.as_dict(True) for item in excluded_values],
        "protected": [item.as_dict(True) for item in protected_values],
        "allowed_roots": list(roots),
        "section_locations": list(locations),
        "quarantine_root": quarantine,
        "quarantine_device": int(quarantine_stat.st_dev),
        "quarantine_inode": int(quarantine_stat.st_ino),
        "watched_directories": [value.as_dict() for value in inventory_after],
    }
    digest = _digest_payload(manifest)
    return QuarantinePlan(
        video=video,
        eligible=eligible_values,
        excluded=excluded_values,
        protected=protected_values,
        allowed_roots=roots,
        section_locations=locations,
        quarantine_root=quarantine,
        quarantine_device=int(quarantine_stat.st_dev),
        quarantine_inode=int(quarantine_stat.st_ino),
        watched_directories=inventory_after,
        plan_digest=digest,
    )


class QuarantinePlanner:
    """Adapter that plans from the immutable Plex metadata snapshot."""

    def plan(
        self,
        item: Any,
        delete_media_id: str,
        allowed_roots: Sequence[str],
        quarantine_root: str,
        section_locations: Sequence[str] = (),
    ) -> QuarantinePlan:
        target = None
        survivors: List[str] = []
        for version in tuple(getattr(item, "media", ()) or ()):
            paths = list(getattr(version, "paths", ()) or ())
            if str(getattr(version, "media_id", "")) == str(delete_media_id):
                target = paths
            else:
                survivors.extend(paths)
        if target is None:
            raise QuarantinePlanError("삭제 대상 Media ID를 현재 Plex 항목에서 찾을 수 없습니다.")
        return build_quarantine_plan(
            target,
            survivors,
            allowed_roots,
            section_locations,
            quarantine_root,
        )
