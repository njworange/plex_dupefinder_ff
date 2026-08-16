from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from framework import F

from .models import (
    ModelActionLog,
    ModelDirectDeleteJournal,
    ModelDuplicateGroup,
    ModelPostDeleteScanJob,
)
from .services.direct_delete import (
    DirectDeletePlan,
    DirectDeletePlanError,
    DirectDeletePlanner,
)
from .services.quarantine_delete import (
    DirectorySnapshot,
    FileSnapshot,
    QuarantinePlanError,
    _directory_snapshot,
    capture_file_snapshot,
    snapshot_matches,
)
from .setup import P


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    value
    for value in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOSYS", None),
    )
    if value is not None
)
_FULL_CONTENT_PROOF_BYTES = 64 * 1024 * 1024
_CONTENT_PROOF_BLOCK_BYTES = 256 * 1024
_CONTENT_PROOF_BLOCKS = 32


def _fsync_directory(path: str) -> bool:
    """Durably flush a directory where the filesystem supports it.

    A number of FUSE filesystems, including some mergerfs configurations,
    reject directory fsync with an explicit "unsupported" errno.  That is not
    evidence that the rename/unlink failed, so it is recorded as a warning and
    execution continues.  All other errors remain fatal.
    """

    if os.name == "nt":
        return True
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
        return True
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            raise
        P.logger.warning(
            "Direct delete directory fsync unsupported: errno=%s", exc.errno
        )
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _same_identity(expected: FileSnapshot, current: FileSnapshot) -> bool:
    return (
        expected.size == current.size
        and expected.mtime_ns == current.mtime_ns
        and expected.device == current.device
        and expected.inode == current.inode
        and expected.links == current.links
        and (not expected.sha256 or expected.sha256 == current.sha256)
    )


def _mtime_ns(value: os.stat_result) -> int:
    return int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1e9)))


def _safe_regular_stat(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and not bool(getattr(value, "st_file_attributes", 0) & 0x0400)
    )


def _stat_matches_snapshot(
    expected: FileSnapshot, current: os.stat_result, require_identity: bool
) -> bool:
    if not _safe_regular_stat(current):
        return False
    if (
        int(current.st_size) != expected.size
        or _mtime_ns(current) != expected.mtime_ns
        or int(getattr(current, "st_nlink", 1) or 1) != expected.links
    ):
        return False
    return not require_identity or (
        int(current.st_dev) == expected.device
        and int(current.st_ino) == expected.inode
    )


def _open_regular_nofollow(path: str) -> Tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not _safe_regular_stat(opened):
            raise DirectDeletePlanError("직접 삭제 대상이 안전한 일반 파일이 아닙니다.")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _pread_exact(descriptor: int, length: int, offset: int) -> bytes:
    chunks: List[bytes] = []
    remaining = max(0, int(length))
    position = max(0, int(offset))
    while remaining:
        if hasattr(os, "pread"):
            chunk = os.pread(descriptor, remaining, position)
        else:  # pragma: no cover - POSIX builds used by FlaskFarm expose pread.
            original = os.lseek(descriptor, 0, os.SEEK_CUR)
            try:
                os.lseek(descriptor, position, os.SEEK_SET)
                chunk = os.read(descriptor, remaining)
            finally:
                os.lseek(descriptor, original, os.SEEK_SET)
        if not chunk:
            break
        chunks.append(chunk)
        position += len(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _content_proof_offsets(expected: FileSnapshot) -> Tuple[int, ...]:
    size = expected.size
    if size <= _FULL_CONTENT_PROOF_BYTES:
        return (0,)
    maximum = max(0, size - _CONTENT_PROOF_BLOCK_BYTES)
    offsets = {0, maximum}
    seed = (
        "%s:%s:%s:%s" % (
            expected.size,
            expected.mtime_ns,
            expected.device,
            expected.inode,
        )
    ).encode("ascii")
    counter = 0
    while len(offsets) < min(_CONTENT_PROOF_BLOCKS, maximum + 1):
        digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        offsets.add(int.from_bytes(digest[:8], "big") % (maximum + 1))
        counter += 1
    return tuple(sorted(offsets))


def _descriptor_content_proof(
    descriptor: int, expected: FileSnapshot, full_content: bool
) -> str:
    hasher = hashlib.sha256()
    if full_content or expected.size <= _FULL_CONTENT_PROOF_BYTES:
        offset = 0
        while offset < expected.size:
            length = min(1024 * 1024, expected.size - offset)
            value = _pread_exact(descriptor, length, offset)
            if len(value) != length:
                raise DirectDeletePlanError("직접 삭제 파일의 전체 해시를 읽을 수 없습니다.")
            hasher.update(value)
            offset += length
        return hasher.hexdigest()

    hasher.update(str(expected.size).encode("ascii"))
    for offset in _content_proof_offsets(expected):
        length = min(_CONTENT_PROOF_BLOCK_BYTES, expected.size - offset)
        value = _pread_exact(descriptor, length, offset)
        if len(value) != length:
            raise DirectDeletePlanError("직접 삭제 파일의 내용 증명을 읽을 수 없습니다.")
        hasher.update(offset.to_bytes(8, "big"))
        hasher.update(length.to_bytes(8, "big"))
        hasher.update(value)
    return hasher.hexdigest()


def _descriptor_owns_path(
    descriptor: int, path: str, expected: FileSnapshot
) -> bool:
    opened = os.fstat(descriptor)
    current = os.lstat(path)
    return (
        _stat_matches_snapshot(expected, opened, require_identity=False)
        and _stat_matches_snapshot(expected, current, require_identity=False)
        and int(opened.st_dev) == int(current.st_dev)
        and int(opened.st_ino) == int(current.st_ino)
    )


def _prove_posix_handoff(
    source_descriptor: int,
    tombstone: str,
    expected: FileSnapshot,
    full_content: bool,
    keep_target_open: bool = False,
) -> Optional[int]:
    source_after = os.fstat(source_descriptor)
    if not _stat_matches_snapshot(expected, source_after, require_identity=False):
        raise DirectDeletePlanError("열어 둔 삭제 원본의 상태가 변경되었습니다.")
    target_descriptor, target_opened = _open_regular_nofollow(tombstone)
    try:
        if not _stat_matches_snapshot(expected, target_opened, require_identity=False):
            raise DirectDeletePlanError("직접 삭제 handoff 파일 상태가 일치하지 않습니다.")
        if not _descriptor_owns_path(target_descriptor, tombstone, expected):
            raise DirectDeletePlanError("직접 삭제 handoff 경로 identity가 변경되었습니다.")
        source_proof = _descriptor_content_proof(
            source_descriptor, expected, full_content
        )
        target_proof = _descriptor_content_proof(
            target_descriptor, expected, full_content
        )
        if not secrets.compare_digest(source_proof, target_proof):
            raise DirectDeletePlanError("직접 삭제 handoff 파일 내용이 원본과 다릅니다.")
        if expected.sha256 and not secrets.compare_digest(
            expected.sha256, target_proof
        ):
            raise DirectDeletePlanError("직접 삭제 자막의 전체 해시가 변경되었습니다.")
        if not _descriptor_owns_path(target_descriptor, tombstone, expected):
            raise DirectDeletePlanError("직접 삭제 handoff 경로가 검사 중 변경되었습니다.")
        if keep_target_open:
            kept = target_descriptor
            target_descriptor = -1
            return kept
        return None
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)


def _operation_parent(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.dirname(path))))


def _verify_owner_directories(
    plan: DirectDeletePlan,
    removed_paths: Sequence[str] = (),
    operation_paths: Sequence[str] = (),
) -> None:
    removed_by_parent: Dict[str, set] = {}
    for path in removed_paths:
        removed_by_parent.setdefault(_operation_parent(path), set()).add(
            os.path.basename(path)
        )
    added_by_parent: Dict[str, set] = {}
    for path in operation_paths:
        added_by_parent.setdefault(_operation_parent(path), set()).add(
            os.path.basename(path)
        )
    for approved in plan.watched_directories:
        try:
            current = _directory_snapshot(approved.path)
        except QuarantinePlanError as exc:
            raise DirectDeletePlanError(str(exc)) from exc
        key = os.path.normcase(approved.path)
        removed = removed_by_parent.get(key, set())
        added = added_by_parent.get(key, set())
        expected_entries = tuple(
            sorted((set(approved.entries) - removed) | added)
        )
        if (
            current.device != approved.device
            or current.inode != approved.inode
            or current.entries != expected_entries
            or (
                not removed
                and not added
                and (
                    current.mtime_ns != approved.mtime_ns
                    or current.ctime_ns != approved.ctime_ns
                )
            )
        ):
            raise DirectDeletePlanError(
                "영상·자막 폴더 내용이 사전확인 이후 변경되었습니다. 수동 확인이 필요합니다."
            )


def _verify_operation_paths(values: Sequence[Dict[str, Any]]) -> None:
    for raw in values:
        if raw.get("state") != "created":
            continue
        path = os.path.normpath(os.path.abspath(str(raw.get("path") or "")))
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise DirectDeletePlanError("직접 삭제 작업 폴더가 사라졌습니다.") from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or bool(getattr(current, "st_file_attributes", 0) & 0x0400)
            or int(current.st_dev) != int(raw.get("device", -1))
            or int(current.st_ino) != int(raw.get("inode", -1))
            or os.path.normcase(os.path.realpath(path)) != os.path.normcase(path)
        ):
            raise DirectDeletePlanError("직접 삭제 작업 폴더 identity가 변경되었습니다.")


def _snapshot_from_dict(raw: Dict[str, Any]) -> FileSnapshot:
    try:
        return FileSnapshot(
            path=str(raw["path"]),
            size=int(raw["size"]),
            mtime_ns=int(raw["mtime_ns"]),
            device=int(raw["device"]),
            inode=int(raw["inode"]),
            links=int(raw["links"]),
            sha256=str(raw.get("sha256") or ""),
        )
    except (KeyError, TypeError, ValueError):
        raise DirectDeletePlanError("직접 삭제 파일 snapshot 기록이 올바르지 않습니다.") from None


class DirectDeleteManager:
    def __init__(self) -> None:
        self.planner = DirectDeletePlanner()

    @staticmethod
    def enabled() -> bool:
        return (
            str(P.ModelSetting.get("setting_delete_backend") or "plex")
            .strip()
            .lower()
            == "direct"
        )

    @staticmethod
    def _scan_mode() -> str:
        return (
            str(P.ModelSetting.get("setting_post_delete_scan_mode") or "none")
            .strip()
            .lower()
        )

    def preview(
        self,
        item: Any,
        delete_media_id: str,
        allowed_roots: Sequence[str],
        section_locations: Sequence[str],
    ) -> DirectDeletePlan:
        if not self.enabled():
            raise DirectDeletePlanError("직접 파일 삭제 방식이 활성화되지 않았습니다.")
        mode = self._scan_mode()
        if mode not in ("binary", "web"):
            raise DirectDeletePlanError("직접 삭제는 Binary 또는 Web 부분 스캔이 필수입니다.")
        return self.planner.plan(
            item,
            str(delete_media_id),
            tuple(allowed_roots),
            tuple(section_locations),
            mode,
        )

    @staticmethod
    def _commit(journal: ModelDirectDeleteJournal) -> None:
        journal.updated_at = datetime.now()
        F.db.session.commit()

    @staticmethod
    def _verify_protected(plan: DirectDeletePlan) -> None:
        for value in plan.survivors:
            if not snapshot_matches(value, verify_hash=False):
                raise DirectDeletePlanError("유지 영상이 사전확인 이후 변경되었습니다.")
        for decision in plan.protected:
            if decision.snapshot is None or not snapshot_matches(
                decision.snapshot, verify_hash=True
            ):
                raise DirectDeletePlanError("유지본 자막이 사전확인 이후 변경되었습니다.")

    def execute(
        self,
        plan: DirectDeletePlan,
        expected_digest: str,
        run: Any,
        group: Any,
        candidate: Any,
        keep: Any,
        action_log: ModelActionLog,
        batch_run_id: Optional[int] = None,
        heartbeat: Optional[Any] = None,
    ) -> ModelDirectDeleteJournal:
        def beat() -> None:
            if callable(heartbeat):
                heartbeat()

        beat()
        if not expected_digest or not secrets.compare_digest(
            str(expected_digest), str(plan.plan_digest)
        ):
            raise DirectDeletePlanError(
                "직접 삭제 계획이 사전확인 이후 변경되었습니다. 다시 사전확인하세요."
            )
        if not snapshot_matches(plan.video, verify_hash=False):
            raise DirectDeletePlanError("삭제 대상 영상이 사전확인 이후 변경되었습니다.")
        for decision in plan.eligible:
            if decision.snapshot is None or not snapshot_matches(
                decision.snapshot, verify_hash=True
            ):
                raise DirectDeletePlanError("삭제 대상 자막이 사전확인 이후 변경되었습니다.")
        self._verify_protected(plan)
        _verify_owner_directories(plan)

        operation_key = secrets.token_hex(24)
        source_items: List[Tuple[FileSnapshot, str]] = [(plan.video, "video")]
        source_items.extend(
            (decision.snapshot, "subtitle")
            for decision in plan.eligible
            if decision.snapshot is not None
        )
        # New handoffs are same-parent sibling files.  Creating a child
        # operation directory first can select a different physical branch on
        # mergerfs and turn an otherwise-local rename into EXDEV.
        operation_paths: List[Dict[str, Any]] = []
        operations: List[Dict[str, Any]] = []
        for index, (snapshot, kind) in enumerate(source_items):
            operations.append(
                {
                    "source_path": snapshot.path,
                    "tombstone_path": os.path.join(
                        os.path.dirname(snapshot.path),
                        ".pdff-direct-%s-%03d.tombstone" % (operation_key, index),
                    ),
                    "kind": kind,
                    "state": "pending",
                    "handoff_strategy": "same_parent_v2",
                    "snapshot": snapshot.as_dict(),
                }
            )

        journal = ModelDirectDeleteJournal(
            created_at=datetime.now(),
            updated_at=datetime.now(),
            action_log_id=action_log.id,
            batch_run_id=batch_run_id,
            run_id=run.id,
            group_id=group.id,
            candidate_id=candidate.id,
            keep_candidate_id=keep.id,
            operation_key=operation_key,
            status="planned",
            plan_digest=plan.plan_digest,
            manifest_json=_json(plan.manifest_dict()),
            unlink_json=_json(operations),
            operation_paths_json=_json(operation_paths),
            eligible_count=len(plan.eligible),
            excluded_count=len(plan.excluded),
            protected_count=len(plan.protected),
            deleted_count=0,
        )
        action_log.status = "direct_deleting"
        action_log.message = "영상과 전용 외부 자막의 직접 삭제 준비 중"
        try:
            # This is the mutation boundary: every exact source and randomized
            # same-parent tombstone path is durable before rename is attempted.
            F.db.session.add(journal)
            F.db.session.commit()
            beat()
        except Exception:
            F.db.session.rollback()
            raise RuntimeError("직접 삭제 작업 기록을 저장할 수 없습니다.") from None

        handed_off: List[str] = []
        active_tombstones: List[str] = []
        mutation_started = False
        stage = "journal_prepared"
        try:
            stage = "journal_preparing"
            journal.status = "preparing"
            self._commit(journal)
            stage = "journal_deleting"
            journal.status = "deleting"
            self._commit(journal)
            for index, raw in enumerate(operations):
                beat()
                kind = str(raw.get("kind") or "unknown")
                stage = "pre_handoff_%s_%s" % (kind, index)
                _verify_owner_directories(plan, handed_off, active_tombstones)
                self._verify_protected(plan)
                snapshot = _snapshot_from_dict(raw["snapshot"])
                verify_hash = raw.get("kind") == "subtitle"
                tombstone = str(raw["tombstone_path"])
                if os.path.lexists(tombstone):
                    raise DirectDeletePlanError("직접 삭제 임시 경로 충돌을 감지했습니다.")
                source_descriptor: Optional[int] = None
                unlink_descriptor: Optional[int] = None
                try:
                    if os.name != "nt":
                        source_descriptor, source_opened = _open_regular_nofollow(
                            snapshot.path
                        )
                        if not _stat_matches_snapshot(
                            snapshot, source_opened, require_identity=True
                        ):
                            raise DirectDeletePlanError(
                                "직접 삭제 대상 파일 상태가 변경되었습니다."
                            )
                        if not _descriptor_owns_path(
                            source_descriptor, snapshot.path, snapshot
                        ):
                            raise DirectDeletePlanError(
                                "직접 삭제 대상 경로 identity가 변경되었습니다."
                            )
                        if verify_hash:
                            source_hash = _descriptor_content_proof(
                                source_descriptor, snapshot, True
                            )
                            if not snapshot.sha256 or not secrets.compare_digest(
                                snapshot.sha256, source_hash
                            ):
                                raise DirectDeletePlanError(
                                    "직접 삭제 대상 자막 내용이 변경되었습니다."
                                )
                    elif not snapshot_matches(snapshot, verify_hash=verify_hash):
                        raise DirectDeletePlanError(
                            "직접 삭제 대상 파일 상태가 변경되었습니다."
                        )

                    stage = "rename_%s_%s" % (kind, index)
                    os.rename(snapshot.path, tombstone)
                    mutation_started = True
                    handed_off.append(snapshot.path)
                    active_tombstones.append(tombstone)
                    # Persisted by the common failure handler if fsync or the
                    # open-FD proof fails before the normal tombstoned commit.
                    raw["state"] = "handoff_unverified"

                    stage = "handoff_fsync_%s_%s" % (kind, index)
                    _fsync_directory(os.path.dirname(snapshot.path))
                    stage = "handoff_proof_%s_%s" % (kind, index)
                    if os.name != "nt":
                        if source_descriptor is None:
                            raise DirectDeletePlanError(
                                "직접 삭제 원본 descriptor가 없습니다."
                            )
                        _prove_posix_handoff(
                            source_descriptor,
                            tombstone,
                            snapshot,
                            bool(verify_hash),
                        )
                        raw["identity_proof"] = "open_fd_content_v1"
                    else:
                        moved = capture_file_snapshot(
                            tombstone, content_hash=verify_hash
                        )
                        if not _same_identity(snapshot, moved):
                            raise DirectDeletePlanError(
                                "직접 삭제 이동 identity를 확인할 수 없습니다."
                            )
                        raw["identity_proof"] = "strict_path_identity_v1"
                    raw["state"] = "tombstoned"
                    journal.unlink_json = _json(operations)
                    # No heartbeat between rename and this durable evidence.
                    stage = "journal_tombstoned_%s_%s" % (kind, index)
                    self._commit(journal)

                    beat()
                    stage = "pre_unlink_proof_%s_%s" % (kind, index)
                    _verify_owner_directories(
                        plan, handed_off, active_tombstones
                    )
                    self._verify_protected(plan)
                    if os.name != "nt":
                        if source_descriptor is None:
                            raise DirectDeletePlanError(
                                "직접 삭제 원본 descriptor가 없습니다."
                            )
                        unlink_descriptor = _prove_posix_handoff(
                            source_descriptor,
                            tombstone,
                            snapshot,
                            bool(verify_hash),
                            keep_target_open=True,
                        )
                    else:
                        final_current = capture_file_snapshot(
                            tombstone, content_hash=verify_hash
                        )
                        if not _same_identity(snapshot, final_current):
                            raise DirectDeletePlanError(
                                "영구 삭제 직전 파일 identity가 변경되었습니다."
                            )
                    stage = "unlink_%s_%s" % (kind, index)
                    if os.name != "nt":
                        if unlink_descriptor is None or not _descriptor_owns_path(
                            unlink_descriptor, tombstone, snapshot
                        ):
                            raise DirectDeletePlanError(
                                "영구 삭제 직전 handoff 경로 identity가 변경되었습니다."
                            )
                    os.unlink(tombstone)
                    raw["state"] = "unlink_unjournaled"
                    active_tombstones.remove(tombstone)
                    stage = "unlink_fsync_%s_%s" % (kind, index)
                    _fsync_directory(os.path.dirname(tombstone))
                    raw["state"] = "deleted"
                    raw["deleted_at"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
                    journal.unlink_json = _json(operations)
                    journal.deleted_count = sum(
                        1
                        for value in operations
                        if value.get("kind") == "subtitle"
                        and value.get("state") == "deleted"
                    )
                    stage = "journal_deleted_%s_%s" % (kind, index)
                    self._commit(journal)
                finally:
                    if unlink_descriptor is not None:
                        os.close(unlink_descriptor)
                    if source_descriptor is not None:
                        os.close(source_descriptor)

            self._verify_protected(plan)
            stage = "final_directory_proof"
            _verify_owner_directories(plan, handed_off, ())
            self._verify_protected(plan)
            journal.status = "deleted_pending_scan"
            action_log.status = "deleted_pending_scan"
            action_log.message = "영상과 전용 자막 영구 삭제 완료 · Plex 부분 스캔 대기"
            stage = "journal_complete"
            self._commit(journal)
            beat()
            return journal
        except Exception as exc:
            try:
                F.db.session.rollback()
            except Exception:
                pass
            current = ModelDirectDeleteJournal.get(journal.id) or journal
            error_errno = getattr(exc, "errno", None)
            if isinstance(exc, DirectDeletePlanError):
                safe_reason = "안전 검증 조건을 통과하지 못했습니다."
            elif isinstance(exc, OSError):
                safe_reason = "파일시스템 작업이 거부되거나 지원되지 않았습니다."
            else:
                safe_reason = "직접 삭제 내부 단계가 실패했습니다."
            diagnostic = (
                "stage=%s; error=%s; errno=%s; journal=%s; action=%s; reason=%s"
                % (
                    stage,
                    exc.__class__.__name__,
                    error_errno if error_errno is not None else "none",
                    getattr(journal, "id", None),
                    getattr(action_log, "id", None),
                    safe_reason,
                )
            )[:2000]
            no_mutation = not mutation_started
            current.status = (
                "failed_no_mutation" if no_mutation else "recovery_required"
            )
            current.finished_at = datetime.now() if no_mutation else None
            current.last_error = diagnostic
            current.unlink_json = _json(operations)
            current.operation_paths_json = _json(operation_paths)
            current.updated_at = datetime.now()
            current_log = ModelActionLog.get(action_log.id)
            current_group = ModelDuplicateGroup.get(group.id)
            retryable = False
            if no_mutation:
                try:
                    retryable = snapshot_matches(plan.video, verify_hash=False)
                    retryable = retryable and all(
                        decision.snapshot is not None
                        and snapshot_matches(decision.snapshot, verify_hash=True)
                        for decision in plan.eligible
                    )
                    if retryable:
                        self._verify_protected(plan)
                        _verify_owner_directories(plan)
                except Exception:
                    retryable = False
            if current_log is not None:
                current_log.status = "blocked" if no_mutation else "unknown"
                current_log.message = diagnostic
            if current_group is not None:
                if no_mutation:
                    current_group.safe_to_delete = bool(retryable)
                    current_group.resolution_status = "open"
                    current_group.safety_flags_json = _json(
                        [] if retryable else ["direct_delete_repreview_required"]
                    )
                else:
                    current_group.safe_to_delete = False
                    current_group.resolution_status = "manual_check_required"
                    current_group.safety_flags_json = _json(
                        ["direct_delete_recovery_required"]
                    )
            try:
                F.db.session.commit()
            except Exception:
                F.db.session.rollback()
            P.logger.warning(
                "Direct delete failed: journal=%s action=%s stage=%s error=%s errno=%s mutation=%s",
                getattr(journal, "id", None),
                getattr(action_log, "id", None),
                stage,
                exc.__class__.__name__,
                error_errno if error_errno is not None else "none",
                "yes" if mutation_started else "no",
            )
            if no_mutation:
                raise RuntimeError(
                    "직접 삭제 전 단계에서 실패했습니다 "
                    "(journal=%s, action=%s, stage=%s, error=%s, errno=%s). "
                    "원본 삭제는 시작되지 않았습니다."
                    % (
                        getattr(journal, "id", None),
                        getattr(action_log, "id", None),
                        stage,
                        exc.__class__.__name__,
                        error_errno if error_errno is not None else "none",
                    )
                ) from None
            raise RuntimeError(
                "직접 삭제가 완결되지 않았습니다 "
                "(journal=%s, action=%s, stage=%s, error=%s, errno=%s). "
                "작업 이력에서 수동 확인하세요."
                % (
                    getattr(journal, "id", None),
                    getattr(action_log, "id", None),
                    stage,
                    exc.__class__.__name__,
                    error_errno if error_errno is not None else "none",
                )
            ) from None

    def recover_interrupted(self) -> int:
        count = 0
        for journal in ModelDirectDeleteJournal.unfinished():
            if journal.status in ("deleted_pending_scan", "scan_running"):
                if (
                    journal.action_log_id
                    and ModelPostDeleteScanJob.active_for_action(journal.action_log_id)
                    is not None
                ):
                    continue
            operations: Any = []
            operation_paths: Any = []
            try:
                operations = json.loads(getattr(journal, "unlink_json", "[]") or "[]")
                operation_paths = json.loads(
                    getattr(journal, "operation_paths_json", "[]") or "[]"
                )
            except (TypeError, ValueError):
                operations = []
                operation_paths = []
            new_source_only = bool(operations) and not bool(operation_paths)
            if new_source_only:
                for raw in operations:
                    if (
                        not isinstance(raw, dict)
                        or raw.get("handoff_strategy") != "same_parent_v2"
                        or raw.get("state") != "pending"
                    ):
                        new_source_only = False
                        break
                    source = str(raw.get("source_path") or "")
                    tombstone = str(raw.get("tombstone_path") or "")
                    try:
                        if (
                            not source
                            or not os.path.lexists(source)
                            or not tombstone
                            or os.path.lexists(tombstone)
                        ):
                            new_source_only = False
                            break
                    except (OSError, ValueError):
                        new_source_only = False
                        break
            if new_source_only:
                journal.status = "failed_no_mutation"
                journal.finished_at = datetime.now()
                journal.last_error = (
                    "stage=startup_recovery; error=Interrupted; errno=none; "
                    "journal=%s; action=%s; message=원본 삭제 시작 전 작업이 중단되었습니다."
                    % (journal.id, journal.action_log_id)
                )
                journal.updated_at = datetime.now()
                action = (
                    ModelActionLog.get(journal.action_log_id)
                    if journal.action_log_id
                    else None
                )
                if action is not None:
                    action.status = "blocked"
                    action.message = journal.last_error
                group = ModelDuplicateGroup.get(journal.group_id)
                if group is not None:
                    group.safe_to_delete = False
                    group.resolution_status = "open"
                    group.safety_flags_json = _json(
                        ["direct_delete_repreview_required"]
                    )
                count += 1
                continue
            journal.status = "recovery_required"
            journal.last_error = (
                "FlaskFarm 재시작으로 직접 삭제 단계를 확정할 수 없습니다. "
                "자동 재시도하지 않으며 수동 확인이 필요합니다."
            )
            journal.updated_at = datetime.now()
            action = (
                ModelActionLog.get(journal.action_log_id)
                if journal.action_log_id
                else None
            )
            if action is not None:
                action.status = "unknown"
                action.message = journal.last_error
            group = ModelDuplicateGroup.get(journal.group_id)
            if group is not None:
                group.safe_to_delete = False
                group.resolution_status = "manual_check_required"
                group.safety_flags_json = _json(
                    ["direct_delete_recovery_required"]
                )
            count += 1
        if count:
            F.db.session.commit()
        return count

    def verify_deleted(
        self, journal: ModelDirectDeleteJournal, heartbeat: Optional[Any] = None
    ) -> Dict[str, int]:
        try:
            manifest = json.loads(journal.manifest_json or "{}")
            operations = json.loads(journal.unlink_json or "[]")
            operation_paths = json.loads(journal.operation_paths_json or "[]")
        except (TypeError, ValueError):
            raise DirectDeletePlanError("직접 삭제 journal을 읽을 수 없습니다.") from None
        if not isinstance(manifest, dict) or not isinstance(operations, list):
            raise DirectDeletePlanError("직접 삭제 journal이 올바르지 않습니다.")
        if not operations or not isinstance(operation_paths, list):
            raise DirectDeletePlanError("직접 삭제 작업 기록이 비어 있습니다.")

        video_count = 0
        verified = 0
        for raw in operations:
            if callable(heartbeat):
                heartbeat()
            if not isinstance(raw, dict) or raw.get("state") != "deleted":
                raise DirectDeletePlanError("일부 파일의 영구 삭제 상태를 확정할 수 없습니다.")
            source = str(raw.get("source_path") or "")
            tombstone = str(raw.get("tombstone_path") or "")
            if not source or not tombstone or os.path.lexists(source) or os.path.lexists(tombstone):
                raise DirectDeletePlanError("삭제 경로에 파일이 다시 생겨 자동 확정하지 않습니다.")
            if raw.get("kind") == "video":
                video_count += 1
            elif raw.get("kind") != "subtitle":
                raise DirectDeletePlanError("직접 삭제 작업 종류가 올바르지 않습니다.")
            verified += 1
        for raw in operation_paths:
            if not isinstance(raw, dict) or raw.get("state") != "removed":
                raise DirectDeletePlanError("직접 삭제 작업 폴더 정리가 완료되지 않았습니다.")
            if os.path.lexists(str(raw.get("path") or "")):
                raise DirectDeletePlanError("직접 삭제 작업 폴더가 남아 있습니다.")
        if video_count != 1:
            raise DirectDeletePlanError("영구 삭제된 영상 파일 수가 올바르지 않습니다.")

        for raw in manifest.get("survivors", []):
            if callable(heartbeat):
                heartbeat()
            snapshot = _snapshot_from_dict(raw)
            if not snapshot_matches(snapshot, verify_hash=False):
                raise DirectDeletePlanError("유지 영상이 직접 삭제 당시와 달라졌습니다.")
        for decision in manifest.get("protected", []):
            if callable(heartbeat):
                heartbeat()
            if not isinstance(decision, dict) or not isinstance(
                decision.get("snapshot"), dict
            ):
                raise DirectDeletePlanError("유지 자막 snapshot 기록이 없습니다.")
            snapshot = _snapshot_from_dict(decision["snapshot"])
            if not snapshot_matches(snapshot, verify_hash=True):
                raise DirectDeletePlanError("유지 자막이 직접 삭제 당시와 달라졌습니다.")
        return {"verified": verified, "videos": video_count}
