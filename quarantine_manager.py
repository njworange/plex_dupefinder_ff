from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from framework import F

from .models import (
    ModelActionLog,
    ModelDuplicateGroup,
    ModelPostDeleteScanJob,
    ModelQuarantineJournal,
)
from .services.quarantine_delete import (
    FileSnapshot,
    QuarantinePlan,
    QuarantinePlanError,
    QuarantinePlanner,
    capture_file_snapshot,
    directory_snapshot_matches,
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
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_verified(
    source: FileSnapshot, destination: str, heartbeat: Optional[Any] = None
) -> None:
    if callable(heartbeat):
        heartbeat()
    if not snapshot_matches(source, verify_hash=True):
        raise QuarantinePlanError("보호할 자막 파일이 사전확인 이후 변경되었습니다.")
    temporary = destination + ".tmp-" + secrets.token_hex(8)
    source_flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        source_flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    source_descriptor = os.open(source.path, source_flags)
    try:
        opened = os.fstat(source_descriptor)
        opened_mtime = int(
            getattr(opened, "st_mtime_ns", int(opened.st_mtime * 1e9))
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or int(opened.st_dev) != source.device
            or int(opened.st_ino) != source.inode
            or int(opened.st_size) != source.size
            or opened_mtime != source.mtime_ns
            or int(getattr(opened, "st_nlink", 1) or 1) != 1
        ):
            raise QuarantinePlanError("보호할 자막 파일이 복사 직전에 변경되었습니다.")
        try:
            with open(temporary, "xb") as writer:
                while True:
                    if callable(heartbeat):
                        heartbeat()
                    chunk = os.read(source_descriptor, 128 * 1024)
                    if not chunk:
                        break
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
        finally:
            os.close(source_descriptor)
        copied = hashlib.sha256()
        with open(temporary, "rb") as reader:
            while True:
                if callable(heartbeat):
                    heartbeat()
                chunk = reader.read(128 * 1024)
                if not chunk:
                    break
                copied.update(chunk)
        if source.sha256 and copied.hexdigest() != source.sha256:
            raise QuarantinePlanError("보호 자막 복사본 해시 검증에 실패했습니다.")
        if callable(heartbeat):
            heartbeat()
        os.replace(temporary, destination)
        _fsync_directory(os.path.dirname(destination))
    except Exception:
        try:
            os.close(source_descriptor)
        except OSError:
            pass
        raise
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _entry_name(path: str) -> str:
    digest = hashlib.sha256(path.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    return "%s-%s" % (digest, os.path.basename(path))


def _verify_quarantine_root(plan: QuarantinePlan) -> None:
    root = os.path.normpath(os.path.abspath(plan.quarantine_root))
    _safe_parent_directory(os.path.join(root, ".pdff-root-check"))
    try:
        value = os.lstat(root)
    except OSError as exc:
        raise QuarantinePlanError("격리 루트 상태를 확인할 수 없습니다.") from exc
    if (
        not stat.S_ISDIR(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or bool(getattr(value, "st_file_attributes", 0) & 0x0400)
        or int(value.st_dev) != int(plan.quarantine_device)
        or int(value.st_ino) != int(plan.quarantine_inode)
        or os.path.normcase(os.path.realpath(root)) != os.path.normcase(root)
    ):
        raise QuarantinePlanError("격리 루트가 사전확인 이후 변경되었습니다.")


def _verify_owner_directories(
    plan: QuarantinePlan, moved_paths: Sequence[str] = ()
) -> None:
    if any(
        not directory_snapshot_matches(snapshot, moved_paths)
        for snapshot in plan.watched_directories
    ):
        raise QuarantinePlanError(
            "영상·자막 폴더 내용이 사전확인 이후 변경되었습니다. "
            "모호한 자막은 이동하지 않고 수동 확인이 필요합니다."
        )


def _safe_parent_directory(path: str) -> str:
    parent = os.path.normpath(os.path.abspath(os.path.dirname(path)))
    if os.path.normcase(os.path.realpath(parent)) != os.path.normcase(parent):
        raise QuarantinePlanError("유지 자막의 상위 경로가 변경되었습니다.")
    current = parent
    while True:
        try:
            value = os.lstat(current)
        except OSError as exc:
            raise QuarantinePlanError("유지 자막의 상위 경로를 확인할 수 없습니다.") from exc
        if (
            not stat.S_ISDIR(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or bool(getattr(value, "st_file_attributes", 0) & 0x0400)
        ):
            raise QuarantinePlanError("유지 자막의 상위 경로가 안전하지 않습니다.")
        ancestor = os.path.dirname(current)
        if ancestor == current:
            break
        current = ancestor
    return parent


def _copy_backup_without_overwrite(
    backup_path: str, source_path: str, expected_size: int, expected_hash: str
) -> None:
    parent = _safe_parent_directory(source_path)
    if os.path.lexists(source_path):
        raise QuarantinePlanError("유지 자막 경로에 다른 파일이 생겨 자동 복구하지 않습니다.")

    read_flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        read_flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    reader = os.open(backup_path, read_flags)
    temporary = os.path.join(parent, ".pdff-restore-%s" % secrets.token_hex(12))
    writer: Optional[int] = None
    try:
        backup_stat = os.fstat(reader)
        if (
            not stat.S_ISREG(backup_stat.st_mode)
            or int(backup_stat.st_size) != int(expected_size)
            or int(getattr(backup_stat, "st_nlink", 1) or 1) != 1
        ):
            raise QuarantinePlanError("유지 자막 보호본의 identity가 올바르지 않습니다.")
        write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            write_flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            write_flags |= os.O_NOFOLLOW
        writer = os.open(temporary, write_flags, 0o600)
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(reader, 128 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(writer, view)
                if written <= 0:
                    raise QuarantinePlanError("유지 자막 보호본 복사에 실패했습니다.")
                view = view[written:]
            copied += len(chunk)
            digest.update(chunk)
        os.fsync(writer)
        os.close(writer)
        writer = None
        if copied != int(expected_size) or digest.hexdigest() != str(expected_hash):
            raise QuarantinePlanError("유지 자막 보호본의 해시 검증에 실패했습니다.")
        # link() is an atomic no-overwrite publish. Removing the temporary
        # name immediately afterwards leaves the restored file with nlink=1.
        os.link(temporary, source_path)
        os.unlink(temporary)
        _fsync_directory(parent)
    finally:
        os.close(reader)
        if writer is not None:
            try:
                os.close(writer)
            except OSError:
                pass
        if os.path.lexists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


class QuarantineManager:
    def __init__(self) -> None:
        self.planner = QuarantinePlanner()

    @staticmethod
    def enabled() -> bool:
        value = str(P.ModelSetting.get("setting_delete_backend") or "plex").strip().lower()
        return value == "quarantine"

    @staticmethod
    def root() -> str:
        return str(P.ModelSetting.get("setting_quarantine_root") or "").strip()

    def preview(
        self,
        item: Any,
        delete_media_id: str,
        allowed_roots: Sequence[str],
        section_locations: Sequence[str],
    ) -> QuarantinePlan:
        if not self.enabled():
            raise QuarantinePlanError("안전 격리 방식이 활성화되지 않았습니다.")
        mode = str(P.ModelSetting.get("setting_post_delete_scan_mode") or "none").strip().lower()
        if mode not in ("binary", "web"):
            raise QuarantinePlanError("안전 격리는 Binary 또는 Web 부분 스캔이 필수입니다.")
        return self.planner.plan(
            item,
            str(delete_media_id),
            tuple(allowed_roots),
            self.root(),
            section_locations=tuple(section_locations),
        )

    @staticmethod
    def _journal_commit(journal: ModelQuarantineJournal) -> None:
        journal.updated_at = datetime.now()
        F.db.session.commit()

    def stage(
        self,
        plan: QuarantinePlan,
        expected_digest: str,
        run: Any,
        group: Any,
        candidate: Any,
        keep: Any,
        action_log: ModelActionLog,
        batch_run_id: Optional[int] = None,
        heartbeat: Optional[Any] = None,
    ) -> ModelQuarantineJournal:
        def beat() -> None:
            if callable(heartbeat):
                heartbeat()

        beat()
        if not expected_digest or not secrets.compare_digest(
            str(expected_digest), str(plan.plan_digest)
        ):
            raise QuarantinePlanError(
                "자막·파일 계획이 사전확인 이후 변경되었습니다. 다시 사전확인하세요."
            )
        if not snapshot_matches(plan.video, verify_hash=False):
            raise QuarantinePlanError("삭제 대상 영상이 사전확인 이후 변경되었습니다.")
        for decision in plan.eligible + plan.protected:
            if decision.snapshot is None or not snapshot_matches(
                decision.snapshot, verify_hash=True
            ):
                raise QuarantinePlanError(
                    "자막 파일이 사전확인 이후 변경되었습니다. 다시 사전확인하세요."
                )
        _verify_quarantine_root(plan)
        _verify_owner_directories(plan)

        operation_key = secrets.token_hex(24)
        operation_path = os.path.join(plan.quarantine_root, "pdff-%s" % operation_key)
        journal = ModelQuarantineJournal(
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
            moved_json="[]",
            backups_json="[]",
            operation_path=operation_path,
            eligible_count=len(plan.eligible),
            excluded_count=len(plan.excluded),
            protected_count=len(plan.protected),
            quarantined_count=0,
        )
        action_log.status = "quarantining"
        action_log.message = "영상과 전용 외부 자막의 안전 격리 준비 중"
        try:
            beat()
            F.db.session.add(journal)
            F.db.session.commit()
            beat()
        except Exception:
            F.db.session.rollback()
            raise RuntimeError("격리 작업 기록을 저장할 수 없습니다.") from None

        moved: List[Dict[str, Any]] = []
        backups: List[Dict[str, Any]] = []
        try:
            beat()
            _verify_quarantine_root(plan)
            _verify_owner_directories(plan)
            os.mkdir(operation_path, 0o700)
            backup_dir = os.path.join(operation_path, "protected")
            staged_dir = os.path.join(operation_path, "quarantined")
            os.mkdir(backup_dir, 0o700)
            os.mkdir(staged_dir, 0o700)
            _fsync_directory(plan.quarantine_root)

            journal.status = "backing_up"
            beat()
            self._journal_commit(journal)
            beat()
            for decision in plan.protected:
                if decision.snapshot is None:
                    raise QuarantinePlanError("유지본 보호 자막 상태를 확인할 수 없습니다.")
                destination = os.path.join(backup_dir, _entry_name(decision.path))
                _copy_verified(
                    decision.snapshot, destination, heartbeat=heartbeat
                )
                backups.append(
                    {
                        "source_path": decision.path,
                        "backup_path": destination,
                        "sha256": decision.snapshot.sha256,
                        "size": decision.snapshot.size,
                    }
                )
                journal.backups_json = _json(backups)
                self._journal_commit(journal)
                beat()

            journal.status = "quarantining"
            beat()
            self._journal_commit(journal)
            beat()
            # Move the video first and exclusive sidecars afterwards.  A crash
            # must never leave the candidate video active after some of its
            # approved subtitles have already disappeared. Every completed rename
            # is committed to the journal before the next source is touched.
            move_items = [(plan.video, "video")] + [
                (decision.snapshot, "subtitle") for decision in plan.eligible
            ]
            for snapshot, kind in move_items:
                beat()
                _verify_owner_directories(
                    plan, [item.get("source_path", "") for item in moved]
                )
                if snapshot is None or not snapshot_matches(
                    snapshot, verify_hash=(kind == "subtitle")
                ):
                    raise QuarantinePlanError("격리 대상 파일 상태가 변경되었습니다.")
                destination = os.path.join(staged_dir, _entry_name(snapshot.path))
                if os.path.lexists(destination):
                    raise QuarantinePlanError("격리 목적지 충돌을 감지했습니다.")
                # No heartbeat may run between the rename and its durable
                # journal commit: lease renewal commits its own DB transaction.
                os.replace(snapshot.path, destination)
                destination_stat = os.lstat(destination)
                moved_entry = {
                    "source_path": snapshot.path,
                    "destination_path": destination,
                    "kind": kind,
                    "size": snapshot.size,
                    "mtime_ns": snapshot.mtime_ns,
                    "device": snapshot.device,
                    "inode": snapshot.inode,
                    "links": snapshot.links,
                    "sha256": snapshot.sha256,
                }
                moved.append(moved_entry)
                if (
                    int(destination_stat.st_dev) != snapshot.device
                    or int(destination_stat.st_ino) != snapshot.inode
                    or int(destination_stat.st_size) != snapshot.size
                ):
                    raise QuarantinePlanError("격리 이동의 파일 identity를 확인할 수 없습니다.")
                _fsync_directory(os.path.dirname(snapshot.path))
                _fsync_directory(staged_dir)
                journal.moved_json = _json(moved)
                journal.quarantined_count = sum(
                    1 for item in moved if item.get("kind") == "subtitle"
                )
                self._journal_commit(journal)
                beat()
                _verify_owner_directories(
                    plan, [item.get("source_path", "") for item in moved]
                )

            journal.status = "quarantined_pending_scan"
            action_log.status = "quarantined_pending_scan"
            action_log.message = "영상과 전용 자막 격리 완료 · Plex 부분 스캔 대기"
            beat()
            self._journal_commit(journal)
            beat()
            return journal
        except Exception as exc:
            journal.status = "recovery_required"
            journal.last_error = _safe_error(exc)
            journal.moved_json = _json(moved)
            journal.backups_json = _json(backups)
            journal.updated_at = datetime.now()
            action_log.status = "unknown"
            action_log.message = (
                "격리 이동이 완결되지 않았습니다. 작업 이력과 원본/격리 경로를 확인하세요."
            )
            group.safe_to_delete = False
            group.resolution_status = "manual_check_required"
            group.safety_flags_json = _json(["quarantine_recovery_required"])
            try:
                F.db.session.commit()
            except Exception:
                F.db.session.rollback()
            raise RuntimeError(action_log.message) from None

    def recover_interrupted(self) -> int:
        count = 0
        for journal in ModelQuarantineJournal.unfinished():
            if journal.status in ("quarantined_pending_scan", "scan_running"):
                # These states are complete filesystem transactions. Preserve
                # them only when a durable active scan job still owns the
                # follow-up; otherwise fail closed for manual recovery.
                if (
                    journal.action_log_id
                    and ModelPostDeleteScanJob.active_for_action(
                        journal.action_log_id
                    )
                    is not None
                ):
                    continue
            journal.status = "recovery_required"
            journal.last_error = (
                "FlaskFarm 재시작으로 격리 단계가 중단되었습니다. 자동 이동·복구하지 않습니다."
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
                group.safety_flags_json = _json(["quarantine_recovery_required"])
            count += 1
        if count:
            F.db.session.commit()
        return count

    def verify_or_restore_protected(
        self, journal: ModelQuarantineJournal, heartbeat: Optional[Any] = None
    ) -> Dict[str, int]:
        """Verify kept-version sidecars, restoring only into an absent path.

        Protection copies are never used to overwrite a path. Any ambiguity
        leaves the copy and journal intact for manual recovery.
        """

        try:
            manifest = json.loads(journal.manifest_json or "{}")
            backups = json.loads(journal.backups_json or "[]")
        except (TypeError, ValueError):
            raise QuarantinePlanError("유지 자막 보호 기록을 읽을 수 없습니다.") from None
        protected = manifest.get("protected", []) if isinstance(manifest, dict) else []
        if not isinstance(protected, list) or not isinstance(backups, list):
            raise QuarantinePlanError("유지 자막 보호 기록이 올바르지 않습니다.")
        backup_by_source = {
            str(value.get("source_path") or ""): value
            for value in backups
            if isinstance(value, dict) and value.get("source_path")
        }
        verified = 0
        restored = 0
        for value in protected:
            if callable(heartbeat):
                heartbeat()
            if not isinstance(value, dict):
                raise QuarantinePlanError("유지 자막 보호 기록이 올바르지 않습니다.")
            raw = value.get("snapshot")
            if not isinstance(raw, dict):
                raise QuarantinePlanError("유지 자막 snapshot이 없습니다.")
            try:
                snapshot = FileSnapshot(
                    path=str(raw["path"]),
                    size=int(raw["size"]),
                    mtime_ns=int(raw["mtime_ns"]),
                    device=int(raw["device"]),
                    inode=int(raw["inode"]),
                    links=int(raw["links"]),
                    sha256=str(raw["sha256"]),
                )
            except (KeyError, TypeError, ValueError):
                raise QuarantinePlanError("유지 자막 snapshot이 올바르지 않습니다.") from None
            backup = backup_by_source.get(snapshot.path)
            restored_snapshot: Optional[FileSnapshot] = None
            if isinstance(backup, dict) and isinstance(
                backup.get("restored_snapshot"), dict
            ):
                restored_raw = backup["restored_snapshot"]
                try:
                    restored_snapshot = FileSnapshot(
                        path=str(restored_raw["path"]),
                        size=int(restored_raw["size"]),
                        mtime_ns=int(restored_raw["mtime_ns"]),
                        device=int(restored_raw["device"]),
                        inode=int(restored_raw["inode"]),
                        links=int(restored_raw["links"]),
                        sha256=str(restored_raw["sha256"]),
                    )
                except (KeyError, TypeError, ValueError):
                    raise QuarantinePlanError(
                        "복구된 유지 자막 snapshot이 올바르지 않습니다."
                    ) from None
                if restored_snapshot.path != snapshot.path:
                    raise QuarantinePlanError(
                        "복구된 유지 자막 경로가 작업 기록과 다릅니다."
                    )
            if snapshot_matches(snapshot, verify_hash=True) or (
                restored_snapshot is not None
                and snapshot_matches(restored_snapshot, verify_hash=True)
            ):
                verified += 1
                if callable(heartbeat):
                    heartbeat()
                continue
            if os.path.lexists(snapshot.path):
                raise QuarantinePlanError(
                    "유지 자막이 변경되어 보호본으로 덮어쓰지 않습니다: %s" % snapshot.path
                )
            if not isinstance(backup, dict):
                raise QuarantinePlanError("유지 자막 보호본을 찾을 수 없습니다.")
            backup_path = str(backup.get("backup_path") or "")
            expected_hash = str(backup.get("sha256") or snapshot.sha256)
            if not backup_path or not expected_hash:
                raise QuarantinePlanError("유지 자막 보호본 정보가 올바르지 않습니다.")
            _copy_backup_without_overwrite(
                backup_path, snapshot.path, snapshot.size, expected_hash
            )
            restored_snapshot = capture_file_snapshot(snapshot.path, True)
            # The approved manifest (and therefore plan_digest) is immutable.
            # A restore necessarily has a new inode, so retain that identity
            # only in the internal backup record for idempotent re-verification.
            backup["restored_snapshot"] = restored_snapshot.as_dict()
            journal.backups_json = _json(backups)
            journal.updated_at = datetime.now()
            try:
                F.db.session.commit()
            except Exception:
                F.db.session.rollback()
                raise QuarantinePlanError(
                    "복구된 유지 자막 identity를 작업 기록에 저장하지 못했습니다."
                ) from None
            restored += 1
            if callable(heartbeat):
                heartbeat()
        return {"verified": verified, "restored": restored}

    def verify_quarantined(
        self, journal: ModelQuarantineJournal, heartbeat: Optional[Any] = None
    ) -> Dict[str, int]:
        """Prove every moved file still exists only at its quarantine path."""

        try:
            moved = json.loads(journal.moved_json or "[]")
        except (TypeError, ValueError):
            raise QuarantinePlanError("격리 이동 기록을 읽을 수 없습니다.") from None
        if not isinstance(moved, list) or not moved:
            raise QuarantinePlanError("격리 이동 기록이 비어 있습니다.")
        operation_root = os.path.normcase(
            os.path.realpath(os.path.abspath(str(journal.operation_path or "")))
        )
        verified = 0
        video_count = 0
        for raw in moved:
            if callable(heartbeat):
                heartbeat()
            if not isinstance(raw, dict):
                raise QuarantinePlanError("격리 이동 기록이 올바르지 않습니다.")
            source = str(raw.get("source_path") or "")
            destination = str(raw.get("destination_path") or "")
            kind = str(raw.get("kind") or "")
            if not source or not destination or kind not in ("video", "subtitle"):
                raise QuarantinePlanError("격리 이동 기록이 올바르지 않습니다.")
            try:
                destination_root = os.path.commonpath(
                    (operation_root, os.path.normcase(os.path.realpath(destination)))
                )
            except (OSError, ValueError):
                destination_root = ""
            if destination_root != operation_root:
                raise QuarantinePlanError("격리 파일이 작업 폴더 밖을 가리킵니다.")
            if os.path.lexists(source):
                raise QuarantinePlanError(
                    "격리 후 원본 경로에 파일이 다시 생겨 자동 확정하지 않습니다."
                )
            current = capture_file_snapshot(
                destination, content_hash=(kind == "subtitle")
            )
            try:
                expected_size = int(raw["size"])
                expected_mtime = int(raw["mtime_ns"])
                expected_device = int(raw["device"])
                expected_inode = int(raw["inode"])
                expected_links = int(raw.get("links", 1))
                expected_hash = str(raw.get("sha256") or "")
            except (KeyError, TypeError, ValueError):
                raise QuarantinePlanError("격리 파일 identity 기록이 올바르지 않습니다.") from None
            if (
                current.size != expected_size
                or current.mtime_ns != expected_mtime
                or current.device != expected_device
                or current.inode != expected_inode
                or current.links != expected_links
                or (expected_hash and current.sha256 != expected_hash)
            ):
                raise QuarantinePlanError("격리 파일 identity가 이동 당시와 다릅니다.")
            verified += 1
            if kind == "video":
                video_count += 1
            if callable(heartbeat):
                heartbeat()
        if video_count != 1:
            raise QuarantinePlanError("격리된 영상 파일 수가 올바르지 않습니다.")
        return {"verified": verified, "videos": video_count}
