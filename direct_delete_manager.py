from __future__ import annotations

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


def _safe_error(exc: Exception) -> str:
    return (str(exc) or exc.__class__.__name__)[:2000]


def _fsync_directory(path: str) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
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
        parents: List[str] = []
        for snapshot, _kind in source_items:
            parent = os.path.dirname(snapshot.path)
            if os.path.normcase(parent) not in {os.path.normcase(p) for p in parents}:
                parents.append(parent)
        operation_paths: List[Dict[str, Any]] = []
        operation_by_parent: Dict[str, str] = {}
        for index, parent in enumerate(parents):
            path = os.path.join(
                parent, ".pdff-direct-%s-%s" % (operation_key, index)
            )
            operation_paths.append({"path": path, "state": "pending"})
            operation_by_parent[os.path.normcase(parent)] = path
        operations: List[Dict[str, Any]] = []
        for index, (snapshot, kind) in enumerate(source_items):
            operation_path = operation_by_parent[os.path.normcase(os.path.dirname(snapshot.path))]
            operations.append(
                {
                    "source_path": snapshot.path,
                    "tombstone_path": os.path.join(
                        operation_path, "%03d" % index
                    ),
                    "kind": kind,
                    "state": "pending",
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
            # This commit is the mutation boundary: every path and expected
            # identity is durable before even an operation directory is made.
            F.db.session.add(journal)
            F.db.session.commit()
            beat()
        except Exception:
            F.db.session.rollback()
            raise RuntimeError("직접 삭제 작업 기록을 저장할 수 없습니다.") from None

        removed: List[str] = []
        try:
            journal.status = "preparing"
            self._commit(journal)
            for raw in operation_paths:
                beat()
                created_paths = [
                    value["path"]
                    for value in operation_paths
                    if value.get("state") == "created"
                ]
                _verify_owner_directories(plan, removed, created_paths)
                path = str(raw["path"])
                if os.path.lexists(path):
                    raise DirectDeletePlanError("직접 삭제 작업 폴더 경로가 이미 존재합니다.")
                os.mkdir(path, 0o700)
                current = os.lstat(path)
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or stat.S_ISLNK(current.st_mode)
                    or bool(getattr(current, "st_file_attributes", 0) & 0x0400)
                ):
                    raise DirectDeletePlanError("직접 삭제 작업 폴더가 안전하지 않습니다.")
                _fsync_directory(os.path.dirname(path))
                raw.update(
                    {
                        "state": "created",
                        "device": int(current.st_dev),
                        "inode": int(current.st_ino),
                    }
                )
                journal.operation_paths_json = _json(operation_paths)
                self._commit(journal)

            journal.status = "deleting"
            self._commit(journal)
            for raw in operations:
                beat()
                created_paths = [
                    value["path"]
                    for value in operation_paths
                    if value.get("state") == "created"
                ]
                _verify_operation_paths(operation_paths)
                _verify_owner_directories(plan, removed, created_paths)
                self._verify_protected(plan)
                snapshot = _snapshot_from_dict(raw["snapshot"])
                verify_hash = raw.get("kind") == "subtitle"
                if not snapshot_matches(snapshot, verify_hash=verify_hash):
                    raise DirectDeletePlanError("직접 삭제 대상 파일 상태가 변경되었습니다.")
                tombstone = str(raw["tombstone_path"])
                if os.path.lexists(tombstone):
                    raise DirectDeletePlanError("직접 삭제 임시 경로 충돌을 감지했습니다.")
                # The destination is inside a freshly-created, operation-owned
                # 0700 directory. Rename gives an atomic source identity handoff.
                os.rename(snapshot.path, tombstone)
                _fsync_directory(os.path.dirname(snapshot.path))
                _fsync_directory(os.path.dirname(tombstone))
                moved = capture_file_snapshot(tombstone, content_hash=verify_hash)
                if not _same_identity(snapshot, moved):
                    raise DirectDeletePlanError("직접 삭제 이동 identity를 확인할 수 없습니다.")
                raw["state"] = "tombstoned"
                journal.unlink_json = _json(operations)
                # No heartbeat between rename and this durable evidence.
                self._commit(journal)

                beat()
                _verify_operation_paths(operation_paths)
                current = capture_file_snapshot(tombstone, content_hash=verify_hash)
                if not _same_identity(snapshot, current):
                    raise DirectDeletePlanError("영구 삭제 직전 파일 identity가 변경되었습니다.")
                os.unlink(tombstone)
                _fsync_directory(os.path.dirname(tombstone))
                removed.append(snapshot.path)
                raw["state"] = "deleted"
                raw["deleted_at"] = datetime.now().isoformat(timespec="seconds")
                journal.unlink_json = _json(operations)
                journal.deleted_count = sum(
                    1
                    for value in operations
                    if value.get("kind") == "subtitle"
                    and value.get("state") == "deleted"
                )
                self._commit(journal)

            self._verify_protected(plan)
            for raw in reversed(operation_paths):
                beat()
                _verify_operation_paths(operation_paths)
                path = str(raw["path"])
                os.rmdir(path)
                _fsync_directory(os.path.dirname(path))
                raw["state"] = "removed"
                journal.operation_paths_json = _json(operation_paths)
                self._commit(journal)
            _verify_owner_directories(plan, removed, ())
            self._verify_protected(plan)
            journal.status = "deleted_pending_scan"
            action_log.status = "deleted_pending_scan"
            action_log.message = "영상과 전용 자막 영구 삭제 완료 · Plex 부분 스캔 대기"
            self._commit(journal)
            beat()
            return journal
        except Exception as exc:
            try:
                F.db.session.rollback()
            except Exception:
                pass
            current = ModelDirectDeleteJournal.get(journal.id) or journal
            current.status = "recovery_required"
            current.last_error = _safe_error(exc)
            current.unlink_json = _json(operations)
            current.operation_paths_json = _json(operation_paths)
            current.updated_at = datetime.now()
            current_log = ModelActionLog.get(action_log.id)
            if current_log is not None:
                current_log.status = "unknown"
                current_log.message = (
                    "직접 삭제가 완결되지 않았습니다. 작업 이력과 원본 경로를 확인하세요."
                )
            current_group = ModelDuplicateGroup.get(group.id)
            if current_group is not None:
                current_group.safe_to_delete = False
                current_group.resolution_status = "manual_check_required"
                current_group.safety_flags_json = _json(
                    ["direct_delete_recovery_required"]
                )
            try:
                F.db.session.commit()
            except Exception:
                F.db.session.rollback()
            raise RuntimeError(
                "직접 삭제가 완결되지 않았습니다. 작업 이력에서 수동 확인하세요."
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
