from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime
from typing import Any, Dict, Optional, Set, Tuple

from framework import F

from .direct_delete_manager import DirectDeleteManager
from .delete_budget import (
    delete_attempt_budget,
    require_delete_attempt_available,
)
from .deletion_lease import DeletionLeaseLost, DeletionLeaseService
from .models import (
    ModelActionLog,
    ModelDirectDeleteJournal,
    ModelDuplicateGroup,
    ModelMediaCandidate,
    ModelQuarantineJournal,
    ModelScanRun,
)
from .path_conflicts import group_has_cross_path_conflict
from .quarantine_manager import QuarantineManager
from .scan_manager import current_safety_policy
from .services.plex_gateway import (
    PlexDeleteOutcomeUnknown,
    PlexGateway,
    PlexGatewayError,
)
from .services.plex_mate_provider import PlexMateProvider
from .services.post_delete_scan_targets import build_scan_targets, validate_scan_target
from .services.safety import assess_group, validate_fresh_snapshot
from .setup import P


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _delete_enabled() -> bool:
    return P.ModelSetting.get("setting_delete_enabled") == "True"


def _timeout() -> int:
    try:
        return max(5, min(120, int(P.ModelSetting.get("setting_request_timeout") or "20")))
    except (TypeError, ValueError):
        return 20


class DeleteService:
    def __init__(self, post_delete_scan_manager: Optional[Any] = None) -> None:
        self._lock = threading.Lock()
        self.lease_service = DeletionLeaseService()
        self.post_delete_scan_manager = post_delete_scan_manager
        self.quarantine_manager = QuarantineManager()
        self.direct_delete_manager = DirectDeleteManager()

    @staticmethod
    def _delete_backend() -> str:
        value = str(P.ModelSetting.get("setting_delete_backend") or "plex").strip().lower()
        if value not in ("plex", "quarantine", "direct"):
            raise RuntimeError("파일 처리 방식 설정이 올바르지 않습니다.")
        return value

    @staticmethod
    def _post_delete_scan_mode() -> str:
        value = str(
            P.ModelSetting.get("setting_post_delete_scan_mode") or "none"
        ).strip().lower()
        return value if value in ("none", "binary", "web") else "none"

    def wake_post_delete_scans(self) -> None:
        if self.post_delete_scan_manager is not None:
            self.post_delete_scan_manager.wake()

    @staticmethod
    def _sync_batch_after_scan(batch_id: Optional[int]) -> None:
        from .models import ModelBatchItem, ModelBatchRun

        if batch_id is None:
            return
        batch = ModelBatchRun.get(batch_id)
        if batch is None:
            return
        items = ModelBatchItem.by_batch(batch_id)
        succeeded = sum(1 for item in items if item.status == "success")
        failed = sum(
            1
            for item in items
            if item.status
            in ("failed", "blocked", "unknown", "verification_failed", "critical")
        )
        skipped = sum(
            1
            for item in items
            if item.status in ("skipped", "cancelled", "interrupted")
        )
        active = any(
            item.status in (
                "planned",
                "running",
                "scan_pending",
                "quarantined_pending_scan",
                "deleted_pending_scan",
            )
            for item in items
        )
        batch.succeeded_items = succeeded
        batch.failed_items = failed
        batch.skipped_items = skipped
        batch.processed_items = succeeded + failed
        if active:
            batch.status = "scan_pending"
            batch.current_message = "파일 처리 완료 · Plex 부분 스캔/재검증 대기"
            batch.finished_at = None
        else:
            batch.status = (
                "completed_with_errors"
                if failed
                else ("completed_with_warnings" if skipped else "completed")
            )
            batch.current_message = (
                "일부 항목은 수동 확인이 필요합니다."
                if failed
                else "파일 처리 및 Plex 재검증 완료"
            )
            batch.finished_at = datetime.now()

    @staticmethod
    def _batch_item_for_journal(
        journal: Any,
    ) -> Optional[Any]:
        from .models import ModelBatchItem

        if journal.batch_run_id is None:
            return None
        return (
            F.db.session.query(ModelBatchItem)
            .filter_by(
                batch_run_id=int(journal.batch_run_id),
                group_id=int(journal.group_id),
                delete_candidate_id=int(journal.candidate_id),
            )
            .order_by(ModelBatchItem.id.desc())
            .first()
        )

    @staticmethod
    def _quarantine_snapshot_state(
        before: Dict[str, Any], current: Any, delete_media_id: str
    ) -> str:
        """Return verified/trash_pending or raise a safe classification."""

        from .post_delete_scan import PostDeleteScanRetryable

        if current.identity_fingerprint() != str(
            before.get("identity_fingerprint") or ""
        ):
            raise RuntimeError("Plex 항목 identity가 격리 당시와 달라졌습니다.")
        raw_media = before.get("media", [])
        if not isinstance(raw_media, list):
            raise RuntimeError("격리 당시 Plex snapshot을 읽을 수 없습니다.")
        expected = {
            str(value.get("media_id") or ""): value
            for value in raw_media
            if isinstance(value, dict) and value.get("media_id")
        }
        if delete_media_id not in expected:
            raise RuntimeError("격리 대상 Media가 이전 snapshot에 없습니다.")
        current_by_id = {str(value.media_id): value for value in current.media}
        survivor_ids = set(expected) - {delete_media_id}
        current_ids = set(current_by_id)
        if delete_media_id not in current_ids:
            if current_ids != survivor_ids:
                raise RuntimeError("부분 스캔 후 Media 집합이 예상과 다릅니다.")
            state = "verified"
        else:
            if current_ids != set(expected):
                raise RuntimeError("부분 스캔 후 Media 집합이 예상과 다릅니다.")
            target = current_by_id[delete_media_id]
            expected_parts = expected[delete_media_id].get("parts", [])
            expected_paths = {
                str(value.get("file") or "")
                for value in expected_parts
                if isinstance(value, dict) and value.get("file")
            }
            current_paths = {str(part.file or "") for part in target.parts if part.file}
            if current_paths != expected_paths:
                raise RuntimeError("격리 대상 Part 구성이 예상과 다릅니다.")
            exists_values = [part.exists for part in target.parts]
            if not exists_values or any(value is not False for value in exists_values):
                raise PostDeleteScanRetryable(
                    "Plex가 격리된 영상 파일을 아직 반영하지 않았습니다."
                )
            state = "trash_pending"

        for media_id in survivor_ids:
            current_version = current_by_id.get(media_id)
            expected_fingerprint = str(expected[media_id].get("fingerprint") or "")
            if (
                current_version is None
                or not expected_fingerprint
                or current_version.fingerprint() != expected_fingerprint
            ):
                raise RuntimeError("유지 Media snapshot이 격리 당시와 달라졌습니다.")
        return state

    @staticmethod
    def _post_scan_action_ids(job: Any) -> list:
        try:
            action_ids = json.loads(job.action_ids_json or "[]")
        except (TypeError, ValueError):
            action_ids = []
        parsed = []
        for raw in action_ids if isinstance(action_ids, list) else []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value not in parsed:
                parsed.append(value)
        try:
            primary = int(job.action_log_id or 0)
        except (TypeError, ValueError):
            primary = 0
        if primary and primary not in parsed:
            parsed.insert(0, primary)
        return parsed

    def finalize_direct_scan(self, job: Any) -> None:
        """Finalize direct unlinks only after filesystem and Plex proofs agree."""

        from .post_delete_scan import (
            PostDeleteScanBlocked,
            PostDeleteScanRefreshRequired,
            PostDeleteScanRetryable,
        )

        heartbeat = getattr(job, "_pdff_heartbeat", None)
        if callable(heartbeat):
            heartbeat()
        action_ids = self._post_scan_action_ids(job)
        if not action_ids:
            raise PostDeleteScanBlocked(
                "직접 삭제 사후검증 작업에 연결된 작업 이력이 없습니다."
            )

        connection = PlexMateProvider().resolve(require_machine_id=True)
        gateway = PlexGateway(connection, timeout=(5, _timeout()))
        identity = gateway.validate_identity(connection.machine_id, require_match=True)
        if identity.machine_id != str(job.server_machine_id):
            raise PostDeleteScanBlocked("직접 삭제 당시 Plex 서버와 현재 서버가 다릅니다.")
        if callable(heartbeat):
            heartbeat()

        retry_needed = False
        critical_found = False
        for action_id in action_ids:
            if callable(heartbeat):
                heartbeat()
            action = ModelActionLog.get(action_id)
            journal = ModelDirectDeleteJournal.for_action(action_id)
            if action is None or journal is None:
                message = "직접 삭제 사후검증에 필요한 작업 이력 또는 journal을 찾을 수 없습니다."
                if journal is not None:
                    journal.status = "critical"
                    journal.last_error = message
                    journal.updated_at = datetime.now()
                    group = ModelDuplicateGroup.get(journal.group_id)
                    if group is not None:
                        group.safe_to_delete = False
                        group.resolution_status = "manual_check_required"
                        group.safety_flags_json = _json(
                            ["direct_delete_postscan_record_missing"]
                        )
                    batch_item = self._batch_item_for_journal(journal)
                    if batch_item is not None:
                        batch_item.status = "failed"
                        batch_item.message = message
                        batch_item.finished_at = datetime.now()
                    self._sync_batch_after_scan(journal.batch_run_id)
                if action is not None:
                    action.status = "critical"
                    action.message = message
                    group = ModelDuplicateGroup.get(job.group_id)
                    if group is not None:
                        group.safe_to_delete = False
                        group.resolution_status = "manual_check_required"
                        group.safety_flags_json = _json(
                            ["direct_delete_postscan_record_missing"]
                        )
                F.db.session.commit()
                critical_found = True
                continue
            if journal.status in ("verified", "trash_pending"):
                continue
            if journal.status in ("critical", "recovery_required"):
                critical_found = True
                continue

            group = ModelDuplicateGroup.get(journal.group_id)
            candidate = ModelMediaCandidate.get(journal.candidate_id)
            run = ModelScanRun.get(journal.run_id)
            if group is None or candidate is None or run is None:
                journal.status = "critical"
                journal.last_error = "직접 삭제 사후검증에 필요한 DB 항목을 찾을 수 없습니다."
                journal.updated_at = datetime.now()
                if group is not None:
                    group.safe_to_delete = False
                    group.resolution_status = "manual_check_required"
                    group.safety_flags_json = _json(
                        ["direct_delete_postscan_record_missing"]
                    )
                F.db.session.commit()
                critical_found = True
                continue

            journal.status = "scan_running"
            journal.updated_at = datetime.now()
            action.status = "scan_running"
            action.message = "Plex 부분 스캔 후 직접 삭제 결과 재검증 중"
            F.db.session.commit()
            try:
                current = gateway.get_metadata(group.rating_key)
                if callable(heartbeat):
                    heartbeat()
                try:
                    before = json.loads(action.before_json or "{}")
                except (TypeError, ValueError):
                    raise RuntimeError("직접 삭제 당시 Plex snapshot을 읽을 수 없습니다.") from None
                filesystem = self.direct_delete_manager.verify_deleted(
                    journal, heartbeat=heartbeat if callable(heartbeat) else None
                )
                if int(filesystem.get("restored", 0) or 0):
                    journal.last_error = (
                        "PMS DELETE가 제거한 유지 자막 보호본을 복원했습니다. "
                        "Plex 재스캔 후 다시 확인합니다."
                    )
                    action.message = journal.last_error
                    F.db.session.commit()
                    raise PostDeleteScanRefreshRequired(journal.last_error)
                state = self._quarantine_snapshot_state(
                    before, current, str(candidate.media_id)
                )
                if not candidate.deleted:
                    candidate.deleted = True
                    candidate.deleted_at = datetime.now()
                    run.successful_deletions = (run.successful_deletions or 0) + 1
                group.safe_to_delete = False
                group.resolution_status = "rescan_required"
                group.safety_flags_json = _json(
                    [
                        "plex_trash_pending_after_direct_delete"
                        if state == "trash_pending"
                        else "rescan_required_after_direct_delete"
                    ]
                )
                journal.status = state
                journal.finished_at = datetime.now()
                journal.updated_at = datetime.now()
                journal.last_error = ""
                action.status = "success"
                action.message = (
                    "영구 삭제 완료 · Plex 휴지통에 누락 Media 기록이 남아 있습니다."
                    if state == "trash_pending"
                    else "파일 영구 삭제 및 Plex 재검증 완료"
                )
                action.after_json = _json(current.as_dict())
                batch_item = self._batch_item_for_journal(journal)
                if batch_item is not None:
                    batch_item.status = "success"
                    batch_item.message = action.message
                    batch_item.action_log_id = action.id
                    batch_item.finished_at = datetime.now()
                self._sync_batch_after_scan(journal.batch_run_id)
                F.db.session.commit()
                # Backup copies are erased only after the verified success is
                # durable.  A cleanup problem must never turn a proven media
                # deletion into an ambiguous/critical result; startup recovery
                # can safely retry this idempotent, internal-only cleanup.
                try:
                    self.direct_delete_manager.cleanup_backups(
                        journal,
                        heartbeat=heartbeat if callable(heartbeat) else None,
                    )
                    journal.last_error = ""
                    F.db.session.commit()
                except DeletionLeaseLost:
                    F.db.session.rollback()
                    raise
                except Exception as cleanup_exc:
                    F.db.session.rollback()
                    journal = ModelDirectDeleteJournal.for_action(action_id)
                    if journal is not None:
                        journal.last_error = (
                            "삭제 검증은 완료됐지만 내부 자막 보호본 정리가 남아 있습니다."
                        )
                        journal.updated_at = datetime.now()
                        F.db.session.commit()
                    P.logger.warning(
                        "Direct delete backup cleanup deferred: journal=%s error=%s",
                        getattr(journal, "id", None),
                        cleanup_exc.__class__.__name__,
                    )
            except PostDeleteScanRefreshRequired:
                F.db.session.rollback()
                raise
            except PostDeleteScanRetryable:
                F.db.session.rollback()
                retry_needed = True
            except DeletionLeaseLost:
                F.db.session.rollback()
                raise
            except Exception as exc:
                if isinstance(exc, PlexGatewayError):
                    F.db.session.rollback()
                    retry_needed = True
                    continue
                F.db.session.rollback()
                journal = ModelDirectDeleteJournal.for_action(action_id)
                action = ModelActionLog.get(action_id)
                group = ModelDuplicateGroup.get(journal.group_id) if journal else None
                message = (str(exc) or "직접 삭제 사후검증에 실패했습니다.")[:2000]
                if journal is not None:
                    journal.status = "critical"
                    journal.last_error = message
                    journal.updated_at = datetime.now()
                if action is not None:
                    action.status = "critical"
                    action.message = message
                if group is not None:
                    group.safe_to_delete = False
                    group.resolution_status = "manual_check_required"
                    group.safety_flags_json = _json(
                        ["direct_delete_postscan_critical"]
                    )
                if journal is not None:
                    batch_item = self._batch_item_for_journal(journal)
                    if batch_item is not None:
                        batch_item.status = "failed"
                        batch_item.message = message
                        batch_item.finished_at = datetime.now()
                    self._sync_batch_after_scan(journal.batch_run_id)
                F.db.session.commit()
                critical_found = True
        if critical_found:
            raise PostDeleteScanBlocked(
                "일부 직접 삭제 항목은 수동 확인이 필요합니다."
            )
        if retry_needed:
            raise PostDeleteScanRetryable(
                "Plex 직접 삭제 반영을 제한적으로 재확인합니다."
            )

    def finalize_quarantine_scan(self, job: Any) -> None:
        """Finalize every quarantined action coalesced into a scan job."""

        from .post_delete_scan import (
            PostDeleteScanBlocked,
            PostDeleteScanRefreshRequired,
            PostDeleteScanRetryable,
        )

        heartbeat = getattr(job, "_pdff_heartbeat", None)
        if callable(heartbeat):
            heartbeat()

        action_ids_for_backend = self._post_scan_action_ids(job)
        if any(
            ModelDirectDeleteJournal.for_action(action_id) is not None
            for action_id in action_ids_for_backend
        ):
            if any(
                ModelQuarantineJournal.for_action(action_id) is not None
                for action_id in action_ids_for_backend
            ):
                raise PostDeleteScanBlocked(
                    "서로 다른 파일 처리 방식의 사후검증 작업이 섞여 수동 확인이 필요합니다."
                )
            return self.finalize_direct_scan(job)
        if not any(
            ModelQuarantineJournal.for_action(action_id) is not None
            for action_id in action_ids_for_backend
        ):
            # Legacy Plex API DELETE jobs only need the scan command itself;
            # they do not own a filesystem journal finalizer.
            actions = [ModelActionLog.get(action_id) for action_id in action_ids_for_backend]
            if actions and all(
                action is not None and str(action.status or "") == "success"
                for action in actions
            ):
                return

        try:
            action_ids = json.loads(job.action_ids_json or "[]")
        except (TypeError, ValueError):
            action_ids = []
        parsed_ids = []
        for raw in action_ids if isinstance(action_ids, list) else []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value not in parsed_ids:
                parsed_ids.append(value)
        if int(job.action_log_id or 0) and int(job.action_log_id) not in parsed_ids:
            parsed_ids.insert(0, int(job.action_log_id))
        if not parsed_ids:
            raise PostDeleteScanBlocked(
                "격리 사후검증 작업에 연결된 작업 이력이 없습니다."
            )

        connection = PlexMateProvider().resolve(require_machine_id=True)
        gateway = PlexGateway(connection, timeout=(5, _timeout()))
        identity = gateway.validate_identity(connection.machine_id, require_match=True)
        if identity.machine_id != str(job.server_machine_id):
            raise PostDeleteScanBlocked("격리 당시 Plex 서버와 현재 서버가 다릅니다.")
        if callable(heartbeat):
            heartbeat()

        retry_needed = False
        critical_found = False
        for action_id in parsed_ids:
            if callable(heartbeat):
                heartbeat()
            action = ModelActionLog.get(action_id)
            journal = ModelQuarantineJournal.for_action(action_id)
            if action is None or journal is None:
                message = (
                    "격리 사후검증에 필요한 작업 이력 또는 journal을 "
                    "찾을 수 없습니다."
                )
                if journal is not None:
                    journal.status = "critical"
                    journal.last_error = message
                    journal.updated_at = datetime.now()
                    group = ModelDuplicateGroup.get(journal.group_id)
                    if group is not None:
                        group.safe_to_delete = False
                        group.resolution_status = "manual_check_required"
                        group.safety_flags_json = _json(
                            ["quarantine_postscan_record_missing"]
                        )
                    batch_item = self._batch_item_for_journal(journal)
                    if batch_item is not None:
                        batch_item.status = "failed"
                        batch_item.message = message
                        batch_item.finished_at = datetime.now()
                    self._sync_batch_after_scan(journal.batch_run_id)
                if action is not None:
                    action.status = "critical"
                    action.message = message
                    group = ModelDuplicateGroup.get(job.group_id)
                    if group is not None:
                        group.safe_to_delete = False
                        group.resolution_status = "manual_check_required"
                        group.safety_flags_json = _json(
                            ["quarantine_postscan_record_missing"]
                        )
                F.db.session.commit()
                if callable(heartbeat):
                    heartbeat()
                critical_found = True
                continue
            if journal.status in ("verified", "trash_pending"):
                continue
            if journal.status in ("critical", "recovery_required"):
                critical_found = True
                continue
            group = ModelDuplicateGroup.get(journal.group_id)
            candidate = ModelMediaCandidate.get(journal.candidate_id)
            run = ModelScanRun.get(journal.run_id)
            if group is None or candidate is None or run is None:
                journal.status = "critical"
                journal.last_error = "격리 사후검증에 필요한 DB 항목을 찾을 수 없습니다."
                critical_found = True
                continue
            journal.status = "scan_running"
            journal.updated_at = datetime.now()
            action.status = "scan_running"
            action.message = "Plex 부분 스캔 후 격리 결과 재검증 중"
            F.db.session.commit()
            try:
                current = gateway.get_metadata(group.rating_key)
                if callable(heartbeat):
                    heartbeat()
                try:
                    before = json.loads(action.before_json or "{}")
                except (TypeError, ValueError):
                    raise RuntimeError("격리 당시 Plex snapshot을 읽을 수 없습니다.") from None
                verify_quarantined = getattr(
                    self.quarantine_manager, "verify_quarantined", None
                )
                if callable(verify_quarantined):
                    if callable(heartbeat):
                        verify_quarantined(journal, heartbeat=heartbeat)
                    else:
                        verify_quarantined(journal)
                state = self._quarantine_snapshot_state(
                    before, current, str(candidate.media_id)
                )
                if callable(heartbeat):
                    protected = self.quarantine_manager.verify_or_restore_protected(
                        journal, heartbeat=heartbeat
                    )
                else:
                    protected = self.quarantine_manager.verify_or_restore_protected(
                        journal
                    )
                if protected.get("restored", 0):
                    journal.last_error = (
                        "유지 자막 보호본을 복구했습니다. Plex 재스캔 후 다시 확인합니다."
                    )
                    action.message = journal.last_error
                    F.db.session.commit()
                    raise PostDeleteScanRefreshRequired(journal.last_error)

                if not candidate.deleted:
                    candidate.deleted = True
                    candidate.deleted_at = datetime.now()
                    run.successful_deletions = (run.successful_deletions or 0) + 1
                group.safe_to_delete = False
                group.resolution_status = "rescan_required"
                group.safety_flags_json = _json(
                    [
                        "plex_trash_pending_after_quarantine"
                        if state == "trash_pending"
                        else "rescan_required_after_quarantine"
                    ]
                )
                journal.status = state
                journal.finished_at = datetime.now()
                journal.updated_at = datetime.now()
                journal.last_error = ""
                action.status = "success"
                action.message = (
                    "격리 완료 · Plex 휴지통에 누락 Media 기록이 남아 있습니다."
                    if state == "trash_pending"
                    else "격리 및 Plex 재검증 완료"
                )
                action.after_json = _json(current.as_dict())
                batch_item = self._batch_item_for_journal(journal)
                if batch_item is not None:
                    batch_item.status = "success"
                    batch_item.message = action.message
                    batch_item.action_log_id = action.id
                    batch_item.finished_at = datetime.now()
                self._sync_batch_after_scan(journal.batch_run_id)
                F.db.session.commit()
            except PostDeleteScanRefreshRequired:
                F.db.session.rollback()
                raise
            except PostDeleteScanRetryable:
                F.db.session.rollback()
                retry_needed = True
            except DeletionLeaseLost:
                F.db.session.rollback()
                raise
            except Exception as exc:
                if isinstance(exc, PlexGatewayError):
                    F.db.session.rollback()
                    retry_needed = True
                    continue
                F.db.session.rollback()
                journal = ModelQuarantineJournal.for_action(action_id)
                action = ModelActionLog.get(action_id)
                group = ModelDuplicateGroup.get(journal.group_id) if journal else None
                message = (str(exc) or "격리 사후검증에 실패했습니다.")[:2000]
                if journal is not None:
                    journal.status = "critical"
                    journal.last_error = message
                    journal.updated_at = datetime.now()
                if action is not None:
                    action.status = "critical"
                    action.message = message
                if group is not None:
                    group.safe_to_delete = False
                    group.resolution_status = "manual_check_required"
                    group.safety_flags_json = _json(
                        ["quarantine_postscan_critical"]
                    )
                if journal is not None:
                    batch_item = self._batch_item_for_journal(journal)
                    if batch_item is not None:
                        batch_item.status = "failed"
                        batch_item.message = message
                        batch_item.finished_at = datetime.now()
                    self._sync_batch_after_scan(journal.batch_run_id)
                F.db.session.commit()
                critical_found = True
        if critical_found:
            raise PostDeleteScanBlocked("일부 격리 항목은 수동 확인이 필요합니다.")
        if retry_needed:
            raise PostDeleteScanRetryable("Plex 격리 반영을 제한적으로 재확인합니다.")

    def fail_direct_scan(self, job: Any, status: str, message: str) -> None:
        """Persist terminal scan failure for permanently removed files."""

        heartbeat = getattr(job, "_pdff_heartbeat", None)
        if callable(heartbeat):
            heartbeat()
        for action_id in self._post_scan_action_ids(job):
            if callable(heartbeat):
                heartbeat()
            journal = ModelDirectDeleteJournal.for_action(action_id)
            if journal is None or journal.status in (
                "verified",
                "trash_pending",
                "critical",
                "recovery_required",
            ):
                continue
            journal.status = "recovery_required"
            journal.last_error = str(message)[:2000]
            journal.updated_at = datetime.now()
            action = ModelActionLog.get(action_id)
            if action is not None:
                action.status = "unknown"
                action.message = journal.last_error
            group = ModelDuplicateGroup.get(journal.group_id)
            if group is not None:
                group.safe_to_delete = False
                group.resolution_status = "manual_check_required"
                group.safety_flags_json = _json(["direct_delete_scan_failed"])
            batch_item = self._batch_item_for_journal(journal)
            if batch_item is not None:
                batch_item.status = "failed"
                batch_item.message = journal.last_error
                batch_item.finished_at = datetime.now()
            self._sync_batch_after_scan(journal.batch_run_id)
        F.db.session.commit()
        if callable(heartbeat):
            heartbeat()

    def fail_quarantine_scan(self, job: Any, status: str, message: str) -> None:
        """Turn terminal scan failure into an explicit manual-check state."""

        heartbeat = getattr(job, "_pdff_heartbeat", None)
        if callable(heartbeat):
            heartbeat()
        action_ids_for_backend = self._post_scan_action_ids(job)
        if any(
            ModelDirectDeleteJournal.for_action(action_id) is not None
            for action_id in action_ids_for_backend
        ):
            # A correctly-created scan job owns one backend, but corrupted or
            # legacy coalescing must still fail every journal closed. Process
            # direct rows first, then continue through quarantine rows below.
            self.fail_direct_scan(job, status, message)
        try:
            action_ids = json.loads(job.action_ids_json or "[]")
        except (TypeError, ValueError):
            action_ids = []
        values = list(action_ids) if isinstance(action_ids, list) else []
        if job.action_log_id not in values:
            values.append(job.action_log_id)
        for raw in values:
            if callable(heartbeat):
                heartbeat()
            try:
                action_id = int(raw)
            except (TypeError, ValueError):
                continue
            journal = ModelQuarantineJournal.for_action(action_id)
            if journal is None or journal.status in (
                "verified",
                "trash_pending",
                "critical",
                "recovery_required",
            ):
                continue
            journal.status = "recovery_required"
            journal.last_error = str(message)[:2000]
            journal.updated_at = datetime.now()
            action = ModelActionLog.get(action_id)
            if action is not None:
                action.status = "unknown"
                action.message = journal.last_error
            group = ModelDuplicateGroup.get(journal.group_id)
            if group is not None:
                group.safe_to_delete = False
                group.resolution_status = "manual_check_required"
                group.safety_flags_json = _json(["quarantine_scan_failed"])
            batch_item = self._batch_item_for_journal(journal)
            if batch_item is not None:
                batch_item.status = "failed"
                batch_item.message = journal.last_error
                batch_item.finished_at = datetime.now()
            self._sync_batch_after_scan(journal.batch_run_id)
        F.db.session.commit()
        if callable(heartbeat):
            heartbeat()

    def _load(
        self, group_id: int, candidate_id: int, keep_candidate_id: int
    ) -> Tuple[ModelScanRun, ModelDuplicateGroup, ModelMediaCandidate, ModelMediaCandidate]:
        group = ModelDuplicateGroup.get(group_id)
        candidate = ModelMediaCandidate.get(candidate_id)
        keep = ModelMediaCandidate.get(keep_candidate_id)
        if group is None or candidate is None or keep is None:
            raise ValueError("삭제 대상 정보를 찾을 수 없습니다.")
        if candidate.group_id != group.id or keep.group_id != group.id:
            raise ValueError("후보가 동일한 중복 그룹에 속하지 않습니다.")
        if candidate.id == keep.id:
            raise ValueError("유지 버전과 삭제 버전은 달라야 합니다.")
        if candidate.deleted or keep.deleted:
            raise ValueError("이미 삭제 처리된 후보가 포함되어 있습니다.")
        run = ModelScanRun.get(group.run_id)
        if run is None:
            raise ValueError("원본 스캔 이력을 찾을 수 없습니다.")
        return run, group, candidate, keep

    def preview(
        self, group_id: int, candidate_id: int, keep_candidate_id: int
    ) -> Dict[str, Any]:
        """Perform a mutation-free live preview and bind filesystem state."""

        if not _delete_enabled():
            raise RuntimeError("설정에서 수동 삭제를 먼저 활성화해야 합니다.")
        with F.app.app_context():
            run, group, candidate, keep = self._load(
                group_id, candidate_id, keep_candidate_id
            )
            if not group.safe_to_delete or group.resolution_status != "open":
                raise RuntimeError("이 그룹은 안전 삭제 조건을 충족하지 않습니다. 다시 스캔하세요.")
            if run.status not in ("completed", "completed_with_warnings"):
                raise RuntimeError("완료된 스캔의 결과만 처리할 수 있습니다.")
            budget = require_delete_attempt_available(run)
            if group_has_cross_path_conflict(run.id, group.id):
                raise RuntimeError("다른 Plex metadata 그룹과 Part 파일 경로가 겹칩니다.")

            connection = PlexMateProvider().resolve(require_machine_id=True)
            gateway = PlexGateway(connection, timeout=(5, _timeout()))
            identity = gateway.validate_identity(connection.machine_id, require_match=True)
            if identity.machine_id != run.server_machine_id:
                raise RuntimeError("스캔 당시 Plex 서버와 현재 서버가 다릅니다.")
            current = gateway.get_metadata(group.rating_key)
            active_candidates = ModelMediaCandidate.by_group(group.id, include_deleted=False)
            freshness = validate_fresh_snapshot(
                current,
                group.identity_fingerprint,
                {item.media_id: item.fingerprint for item in active_candidates},
            )
            if not freshness.safe:
                raise RuntimeError("스캔 이후 Plex 항목이 변경되었습니다: %s" % ", ".join(freshness.flags))
            safety_policy = current_safety_policy()
            safety = assess_group(current, safety_policy)
            if not safety.safe:
                raise RuntimeError("현재 항목이 안전 정책을 통과하지 못했습니다: %s" % ", ".join(safety.flags))
            current_ids = {version.media_id for version in current.media}
            if candidate.media_id not in current_ids or keep.media_id not in current_ids:
                raise RuntimeError("유지 또는 처리할 Media ID가 Plex에 존재하지 않습니다.")

            backend = self._delete_backend()
            plan_digest = ""
            cleanup: Dict[str, Any]
            if backend in ("quarantine", "direct"):
                sections = gateway.list_sections()
                expected_type = "show" if group.media_type == "episode" else "movie"
                section = next(
                    (
                        item
                        for item in sections
                        if item.key == str(group.section_key)
                        and item.section_type == expected_type
                    ),
                    None,
                )
                if section is None or not section.locations:
                    raise RuntimeError("삭제 대상의 Plex library section을 확인할 수 없습니다.")
                if backend == "quarantine":
                    all_locations = tuple(
                        location for item in sections for location in item.locations
                    )
                    plan = self.quarantine_manager.preview(
                        current,
                        candidate.media_id,
                        safety_policy.allowed_roots,
                        all_locations,
                    )
                else:
                    plan = self.direct_delete_manager.preview(
                        current,
                        candidate.media_id,
                        safety_policy.allowed_roots,
                        tuple(section.locations),
                    )
                cleanup = plan.public_dict()
                plan_digest = plan.plan_digest
                if backend == "quarantine":
                    confirmation = "QUARANTINE %s SUBTITLES %s %s" % (
                        candidate.media_id,
                        len(plan.eligible),
                        plan.plan_digest[:12],
                    )
                else:
                    confirmation = "DELETE MEDIA %s SUBTITLES %s %s" % (
                        candidate.media_id,
                        len(plan.eligible),
                        plan.plan_digest[:12],
                    )
            else:
                cleanup = {
                    "enabled": False,
                    "backend": "plex",
                    "status": "disabled",
                    "eligible": [],
                    "excluded": [],
                    "counts": {
                        "eligible": 0,
                        "excluded": 0,
                        "protected": 0,
                        "quarantined": 0,
                    },
                }
                confirmation = "DELETE %s" % candidate.media_id
            return {
                "confirmation": confirmation,
                "delete_media_id": candidate.media_id,
                "keep_media_id": keep.media_id,
                "backend": backend,
                "plan_digest": plan_digest,
                "subtitle_cleanup": cleanup,
                "delete_budget": budget,
            }

    def _claim_group_and_create_log(
        self,
        run: ModelScanRun,
        group: ModelDuplicateGroup,
        candidate: ModelMediaCandidate,
        keep: ModelMediaCandidate,
    ) -> ModelActionLog:
        try:
            if not ModelDuplicateGroup.claim_for_delete(group.id):
                F.db.session.rollback()
                raise RuntimeError("다른 작업이 이 중복 그룹을 이미 처리 중입니다. 다시 스캔하세요.")
            log = ModelActionLog(
                run_id=run.id,
                group_id=group.id,
                candidate_id=candidate.id,
                keep_candidate_id=keep.id,
                action="delete_media",
                status="validating",
                message="삭제 전 재검증 중",
            )
            F.db.session.add(log)
            F.db.session.commit()
            return log
        except Exception:
            F.db.session.rollback()
            raise

    @staticmethod
    def _reserve_attempt_and_mark_deleting(
        run: ModelScanRun,
        log: ModelActionLog,
        before_json: str,
        status: str = "deleting",
        message: str = "Plex에 Media 삭제 요청 전송",
    ) -> None:
        try:
            if not ModelScanRun.claim_deletion_slot(run.id):
                F.db.session.rollback()
                raise RuntimeError(
                    "삭제 시도를 안전하게 기록할 수 없습니다. "
                    "스캔 상태를 다시 확인하세요."
                )
            log.before_json = before_json
            log.status = status
            log.message = message
            F.db.session.commit()
        except Exception:
            F.db.session.rollback()
            raise

    @staticmethod
    def _finish_log(log: ModelActionLog, status: str, message: str, **values: Any) -> None:
        log.status = status
        log.message = message
        for key, value in values.items():
            setattr(log, key, value)
        F.db.session.commit()

    @staticmethod
    def _lock_group(group: ModelDuplicateGroup, flag: str) -> None:
        group.safe_to_delete = False
        group.resolution_status = "manual_check_required"
        group.safety_flags_json = _json([flag])
        F.db.session.commit()

    def recover_interrupted(
        self,
        exclude_delete_keys: Optional[Set[Tuple[int, int, int]]] = None,
        recovery_lease_token: str = "",
        recovery_lease_owner_ref: str = "plugin_load",
    ) -> Dict[str, int]:
        """Conservatively recover audit rows left mid-delete by a process restart."""
        counts = {"blocked": 0, "unknown": 0}
        excluded = exclude_delete_keys or set()
        with self._lock, F.app.app_context():
            # File moves have their own durable manifest. Classify those first
            # so generic delete recovery never hides a partial quarantine.
            quarantine_recover = getattr(
                self.quarantine_manager, "recover_interrupted", None
            )
            if callable(quarantine_recover):
                quarantine_recover()
            direct_recover = getattr(
                self.direct_delete_manager, "recover_interrupted", None
            )
            if callable(direct_recover):
                if recovery_lease_token:
                    direct_recover(
                        heartbeat=lambda: self.lease_service.renew(
                            recovery_lease_token,
                            "recovery",
                            recovery_lease_owner_ref or "plugin_load",
                        )
                    )
                else:
                    # Backward-compatible diagnostic entry point.  The real
                    # startup path always supplies the singleton recovery
                    # claim; hybrid filesystem recovery skips itself without
                    # that heartbeat.
                    direct_recover()
            logs = ModelActionLog.interrupted()
            for log in logs:
                key = (
                    int(log.run_id or 0),
                    int(log.group_id or 0),
                    int(log.candidate_id or 0),
                )
                if key in excluded:
                    # A prior plugin instance can still be completing its fresh
                    # read/delete/post-read sequence during an in-process reload.
                    # Its live batch worker owns this audit row; do not race it.
                    continue
                previous = log.status
                if previous == "deleting":
                    log.status = "unknown"
                    log.message = (
                        "FlaskFarm 재시작으로 삭제 결과를 확정할 수 없습니다. 자동 재시도하지 않습니다."
                    )
                    flag = "restart_delete_outcome_unknown"
                    counts["unknown"] += 1
                else:
                    log.status = "blocked"
                    log.message = "FlaskFarm 재시작으로 삭제 전 검증이 중단되었습니다. 다시 스캔하세요."
                    flag = "restart_delete_validation_interrupted"
                    counts["blocked"] += 1

                group = ModelDuplicateGroup.get(log.group_id) if log.group_id else None
                if group is not None:
                    group.safe_to_delete = False
                    group.resolution_status = "manual_check_required"
                    group.safety_flags_json = _json([flag])
            F.db.session.commit()
        return counts

    def delete(
        self,
        group_id: int,
        candidate_id: int,
        keep_candidate_id: int,
        confirmation: str,
        plan_digest: str = "",
        lease_owner_token: str = "",
        lease_owner_kind: str = "",
        lease_owner_ref: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            return self._delete_locked(
                group_id,
                candidate_id,
                keep_candidate_id,
                confirmation,
                plan_digest,
                lease_owner_token,
                lease_owner_kind,
                lease_owner_ref,
            )

    def _delete_locked(
        self,
        group_id: int,
        candidate_id: int,
        keep_candidate_id: int,
        confirmation: str,
        plan_digest: str = "",
        lease_owner_token: str = "",
        lease_owner_kind: str = "",
        lease_owner_ref: str = "",
    ) -> Dict[str, Any]:
        if not _delete_enabled():
            raise RuntimeError("설정에서 수동 삭제를 먼저 활성화해야 합니다.")

        owns_lease = not bool(lease_owner_token)
        owner_kind = lease_owner_kind or "manual"
        owner_ref = lease_owner_ref or "%s:%s:%s" % (
            group_id,
            candidate_id,
            keep_candidate_id,
        )
        if owns_lease:
            lease_owner_token = self.lease_service.acquire(owner_kind, owner_ref)
        else:
            self.lease_service.renew(lease_owner_token, owner_kind, owner_ref)
        result: Optional[Dict[str, Any]] = None
        try:
            result = self._delete_transaction(
                group_id,
                candidate_id,
                keep_candidate_id,
                confirmation,
                plan_digest,
                lease_owner_token,
                owner_kind,
                owner_ref,
            )
            return result
        finally:
            if owns_lease:
                try:
                    self.lease_service.release(lease_owner_token)
                finally:
                    if result is not None:
                        self.wake_post_delete_scans()

    def _delete_transaction(
        self,
        group_id: int,
        candidate_id: int,
        keep_candidate_id: int,
        confirmation: str,
        expected_plan_digest: str,
        lease_owner_token: str,
        lease_owner_kind: str,
        lease_owner_ref: str,
    ) -> Dict[str, Any]:

        with F.app.app_context():
            run, group, candidate, keep = self._load(group_id, candidate_id, keep_candidate_id)
            delete_backend = self._delete_backend()
            if delete_backend == "plex":
                expected_confirmation = "DELETE %s" % candidate.media_id
                if str(confirmation) != expected_confirmation:
                    raise ValueError("확인 문구가 일치하지 않습니다: %s" % expected_confirmation)
            elif len(str(expected_plan_digest)) != 64:
                raise ValueError("파일 처리 사전확인 정보가 없거나 올바르지 않습니다.")
            if not group.safe_to_delete or group.resolution_status != "open":
                raise RuntimeError("이 그룹은 안전 삭제 조건을 충족하지 않습니다. 다시 스캔하세요.")
            if run.status not in ("completed", "completed_with_warnings"):
                raise RuntimeError("완료된 스캔의 결과만 삭제에 사용할 수 있습니다.")
            require_delete_attempt_available(run)

            log = self._claim_group_and_create_log(run, group, candidate, keep)
            try:
                if group_has_cross_path_conflict(run.id, group.id):
                    raise RuntimeError(
                        "다른 Plex metadata 그룹과 Part 파일 경로가 겹쳐 삭제를 차단했습니다."
                    )
                connection = PlexMateProvider().resolve(require_machine_id=True)
                gateway = PlexGateway(connection, timeout=(5, _timeout()))
                identity = gateway.validate_identity(connection.machine_id, require_match=True)
                if identity.machine_id != run.server_machine_id:
                    raise RuntimeError("스캔 당시 Plex 서버와 현재 서버가 다릅니다.")

                current = gateway.get_metadata(group.rating_key)
                active_candidates = ModelMediaCandidate.by_group(group.id, include_deleted=False)
                expected_fingerprints = {item.media_id: item.fingerprint for item in active_candidates}
                freshness = validate_fresh_snapshot(
                    current, group.identity_fingerprint, expected_fingerprints
                )
                if not freshness.safe:
                    raise RuntimeError("스캔 이후 Plex 항목이 변경되었습니다: %s" % ", ".join(freshness.flags))

                safety_policy = current_safety_policy()
                safety = assess_group(current, safety_policy)
                if not safety.safe:
                    raise RuntimeError("현재 항목이 안전 정책을 통과하지 못했습니다: %s" % ", ".join(safety.flags))

                current_ids = {version.media_id for version in current.media}
                if candidate.media_id not in current_ids or keep.media_id not in current_ids:
                    raise RuntimeError("유지 또는 삭제할 Media ID가 Plex에 존재하지 않습니다.")
                if len(current_ids) < 2:
                    raise RuntimeError("마지막 Media 버전은 삭제할 수 없습니다.")

                post_scan_mode = self._post_delete_scan_mode()
                post_scan_locations: Tuple[str, ...] = ()
                all_section_locations: Tuple[str, ...] = ()
                if post_scan_mode != "none":
                    if self.post_delete_scan_manager is None:
                        raise RuntimeError(
                            "삭제 후 Plex 스캔 관리자가 초기화되지 않았습니다."
                        )
                    expected_section_type = (
                        "show" if group.media_type == "episode" else "movie"
                    )
                    sections = gateway.list_sections()
                    section = next(
                        (
                            item
                            for item in sections
                            if item.key == str(group.section_key)
                        ),
                        None,
                    )
                    if section is None or section.section_type != expected_section_type:
                        raise RuntimeError(
                            "삭제 대상의 Plex library section을 확인할 수 없습니다."
                        )
                    post_scan_locations = tuple(section.locations)
                    all_section_locations = tuple(
                        location for item in sections for location in item.locations
                    )
                    post_scan_targets = build_scan_targets(
                        group, candidate, current, post_scan_locations
                    )
                    if not post_scan_targets or any(
                        not validate_scan_target(
                            target,
                            post_scan_locations,
                            safety_policy.allowed_roots,
                        )
                        for target in post_scan_targets
                    ):
                        raise RuntimeError(
                            "삭제 대상의 정확한 Plex 부분 스캔 폴더를 확인할 수 없습니다."
                        )

                if delete_backend == "quarantine":
                    if post_scan_mode not in ("binary", "web"):
                        raise RuntimeError(
                            "안전 격리는 Binary 또는 Web 부분 스캔이 필수입니다."
                        )
                    plan = self.quarantine_manager.preview(
                        current,
                        candidate.media_id,
                        safety_policy.allowed_roots,
                        all_section_locations,
                    )
                    expected_confirmation = "QUARANTINE %s SUBTITLES %s %s" % (
                        candidate.media_id,
                        len(plan.eligible),
                        plan.plan_digest[:12],
                    )
                    if not secrets.compare_digest(
                        str(expected_plan_digest), str(plan.plan_digest)
                    ) or not secrets.compare_digest(
                        str(confirmation), expected_confirmation
                    ):
                        raise ValueError(
                            "안전 격리 계획이 사전확인과 일치하지 않습니다. 다시 확인하세요."
                        )
                    self.lease_service.renew(
                        lease_owner_token, lease_owner_kind, lease_owner_ref
                    )
                    self._reserve_attempt_and_mark_deleting(
                        run,
                        log,
                        _json(current.as_dict()),
                        status="quarantining",
                        message="영상과 전용 외부 자막 격리 시작",
                    )
                    batch_run_id: Optional[int] = None
                    if lease_owner_kind == "batch":
                        try:
                            batch_run_id = int(lease_owner_ref)
                        except (TypeError, ValueError):
                            raise RuntimeError(
                                "일괄 처리의 작업 식별자를 확인할 수 없습니다."
                            )
                    journal = self.quarantine_manager.stage(
                        plan=plan,
                        expected_digest=expected_plan_digest,
                        run=run,
                        group=group,
                        candidate=candidate,
                        keep=keep,
                        action_log=log,
                        batch_run_id=batch_run_id,
                        heartbeat=lambda: self.lease_service.renew(
                            lease_owner_token,
                            lease_owner_kind,
                            lease_owner_ref,
                        ),
                    )
                    # Stage may spend significant time hashing/copying large
                    # protected sidecars. Prove ownership once more after its
                    # final durable commit and immediately before enqueueing
                    # the scan outbox row.
                    self.lease_service.renew(
                        lease_owner_token, lease_owner_kind, lease_owner_ref
                    )
                    try:
                        post_scan_jobs = self.post_delete_scan_manager.enqueue_confirmed(
                            run=run,
                            group=group,
                            candidate=candidate,
                            action_log=log,
                            current_item=current,
                            section_locations=post_scan_locations,
                            mode=post_scan_mode,
                            batch_run_id=batch_run_id,
                        )
                        if not post_scan_jobs:
                            raise RuntimeError(
                                "격리 후 Plex 부분 스캔 작업이 생성되지 않았습니다."
                            )
                        F.db.session.commit()
                    except Exception:
                        F.db.session.rollback()
                        journal = ModelQuarantineJournal.get(journal.id)
                        if journal is not None:
                            journal.status = "recovery_required"
                            journal.last_error = (
                                "격리 후 Plex 부분 스캔 작업을 저장하지 못했습니다."
                            )
                            journal.updated_at = datetime.now()
                        current_log = ModelActionLog.get(log.id)
                        if current_log is not None:
                            current_log.status = "unknown"
                            current_log.message = (
                                "파일은 격리되었지만 Plex 부분 스캔 작업을 저장하지 못했습니다."
                            )
                        current_group = ModelDuplicateGroup.get(group.id)
                        if current_group is not None:
                            current_group.safe_to_delete = False
                            current_group.resolution_status = "manual_check_required"
                            current_group.safety_flags_json = _json(
                                ["quarantine_scan_enqueue_failed"]
                            )
                        F.db.session.commit()
                        raise RuntimeError(
                            "파일은 격리되었지만 Plex 부분 스캔 작업을 저장하지 못했습니다. "
                            "작업 이력을 확인하세요."
                        ) from None
                    return {
                        "action_id": log.id,
                        "deleted_media_id": candidate.media_id,
                        "kept_media_id": keep.media_id,
                        "response_status": None,
                        "verification": "quarantined_pending_scan",
                        "subtitle_cleanup": journal.cleanup_api(True),
                        "post_delete_scan": {
                            "mode": post_scan_mode,
                            "status": "queued",
                            "job_ids": [
                                job.id for job in post_scan_jobs if job.id is not None
                            ],
                        },
                    }

                if delete_backend == "direct":
                    if post_scan_mode not in ("binary", "web"):
                        raise RuntimeError(
                            "직접 삭제는 Binary 또는 Web 부분 스캔이 필수입니다."
                        )
                    plan = self.direct_delete_manager.preview(
                        current,
                        candidate.media_id,
                        safety_policy.allowed_roots,
                        post_scan_locations,
                    )
                    expected_confirmation = "DELETE MEDIA %s SUBTITLES %s %s" % (
                        candidate.media_id,
                        len(plan.eligible),
                        plan.plan_digest[:12],
                    )
                    if not secrets.compare_digest(
                        str(expected_plan_digest), str(plan.plan_digest)
                    ) or not secrets.compare_digest(
                        str(confirmation), expected_confirmation
                    ):
                        raise ValueError(
                            "직접 삭제 계획이 사전확인과 일치하지 않습니다. 다시 확인하세요."
                        )
                    if plan.blocking:
                        raise RuntimeError(
                            "보호본을 만들 수 없는 관련 자막이 있어 Plex Media "
                            "DELETE를 실행하지 않습니다. 예외 목록을 확인하세요."
                        )
                    self.lease_service.renew(
                        lease_owner_token, lease_owner_kind, lease_owner_ref
                    )
                    self._reserve_attempt_and_mark_deleting(
                        run,
                        log,
                        _json(current.as_dict()),
                        status="direct_deleting",
                        message="영상과 전용 외부 자막 직접 삭제 시작",
                    )
                    batch_run_id = None
                    if lease_owner_kind == "batch":
                        try:
                            batch_run_id = int(lease_owner_ref)
                        except (TypeError, ValueError):
                            raise RuntimeError(
                                "일괄 처리의 작업 식별자를 확인할 수 없습니다."
                            )
                    journal = self.direct_delete_manager.execute(
                        plan=plan,
                        expected_digest=expected_plan_digest,
                        run=run,
                        group=group,
                        candidate=candidate,
                        keep=keep,
                        action_log=log,
                        batch_run_id=batch_run_id,
                        gateway=gateway,
                        current_item=current,
                        heartbeat=lambda: self.lease_service.renew(
                            lease_owner_token,
                            lease_owner_kind,
                            lease_owner_ref,
                        ),
                    )
                    self.lease_service.renew(
                        lease_owner_token, lease_owner_kind, lease_owner_ref
                    )
                    try:
                        post_scan_jobs = self.post_delete_scan_manager.enqueue_confirmed(
                            run=run,
                            group=group,
                            candidate=candidate,
                            action_log=log,
                            current_item=current,
                            section_locations=post_scan_locations,
                            mode=post_scan_mode,
                            batch_run_id=batch_run_id,
                        )
                        if not post_scan_jobs:
                            raise RuntimeError(
                                "직접 삭제 후 Plex 부분 스캔 작업이 생성되지 않았습니다."
                            )
                        F.db.session.commit()
                    except Exception:
                        F.db.session.rollback()
                        journal = ModelDirectDeleteJournal.get(journal.id)
                        if journal is not None:
                            journal.status = "recovery_required"
                            journal.last_error = (
                                "영구 삭제 후 Plex 부분 스캔 작업을 저장하지 못했습니다."
                            )
                            journal.updated_at = datetime.now()
                        current_log = ModelActionLog.get(log.id)
                        if current_log is not None:
                            current_log.status = "unknown"
                            current_log.message = (
                                "파일은 영구 삭제되었지만 Plex 부분 스캔 작업을 저장하지 못했습니다."
                            )
                        current_group = ModelDuplicateGroup.get(group.id)
                        if current_group is not None:
                            current_group.safe_to_delete = False
                            current_group.resolution_status = "manual_check_required"
                            current_group.safety_flags_json = _json(
                                ["direct_delete_scan_enqueue_failed"]
                            )
                        F.db.session.commit()
                        raise RuntimeError(
                            "파일은 영구 삭제되었지만 Plex 부분 스캔 작업을 저장하지 못했습니다. "
                            "작업 이력을 확인하세요."
                        ) from None
                    return {
                        "action_id": log.id,
                        "deleted_media_id": candidate.media_id,
                        "kept_media_id": keep.media_id,
                        "response_status": getattr(log, "response_status", None),
                        "verification": "deleted_pending_scan",
                        "subtitle_cleanup": journal.cleanup_api(True),
                        "post_delete_scan": {
                            "mode": post_scan_mode,
                            "status": "queued",
                            "job_ids": [
                                job.id for job in post_scan_jobs if job.id is not None
                            ],
                        },
                    }

                self.lease_service.renew(
                    lease_owner_token, lease_owner_kind, lease_owner_ref
                )
                self._reserve_attempt_and_mark_deleting(run, log, _json(current.as_dict()))

                response_status: Optional[int] = None
                outcome_unknown = False
                try:
                    response_status = gateway.delete_media(group.rating_key, candidate.media_id)
                except PlexDeleteOutcomeUnknown:
                    outcome_unknown = True

                self.lease_service.renew(
                    lease_owner_token, lease_owner_kind, lease_owner_ref
                )
                try:
                    after = gateway.get_metadata(group.rating_key)
                except Exception as verify_exc:
                    message = "삭제 요청 후 Plex 상태를 재확인할 수 없습니다. 자동 재시도하지 않았습니다."
                    self._lock_group(group, "delete_outcome_unknown")
                    self._finish_log(
                        log,
                        "unknown",
                        message,
                        response_status=response_status,
                    )
                    raise RuntimeError(message) from verify_exc
                after_ids = {version.media_id for version in after.media}
                if candidate.media_id in after_ids:
                    status = "unknown" if outcome_unknown else "verification_failed"
                    message = (
                        "삭제 응답을 확정할 수 없습니다. 자동 재시도하지 않았습니다."
                        if outcome_unknown
                        else "Plex 재조회에서 삭제 대상이 여전히 확인됩니다."
                    )
                    self._lock_group(
                        group,
                        "delete_outcome_unknown" if outcome_unknown else "delete_verification_failed",
                    )
                    self._finish_log(
                        log,
                        status,
                        message,
                        response_status=response_status,
                        after_json=_json(after.as_dict()),
                    )
                    raise RuntimeError(message)
                if keep.media_id not in after_ids or not after_ids:
                    message = "삭제 후 유지 버전을 확인할 수 없습니다. Plex를 즉시 점검하세요."
                    self._lock_group(group, "delete_postcheck_critical")
                    self._finish_log(
                        log,
                        "critical",
                        message,
                        response_status=response_status,
                        after_json=_json(after.as_dict()),
                    )
                    raise RuntimeError(message)

                expected_after_ids = current_ids - {candidate.media_id}
                if after_ids != expected_after_ids:
                    message = (
                        "삭제 후 Media 집합이 예상과 다릅니다. 다른 버전의 동시 변경 여부를 "
                        "Plex에서 즉시 점검하세요."
                    )
                    self._lock_group(group, "delete_postcheck_media_set_changed")
                    self._finish_log(
                        log,
                        "critical",
                        message,
                        response_status=response_status,
                        after_json=_json(after.as_dict()),
                    )
                    raise RuntimeError(message)

                expected_after_fingerprints = {
                    version.media_id: version.fingerprint()
                    for version in current.media
                    if version.media_id != candidate.media_id
                }
                after_fingerprints = {
                    version.media_id: version.fingerprint() for version in after.media
                }
                if (
                    after.identity_fingerprint() != current.identity_fingerprint()
                    or after_fingerprints != expected_after_fingerprints
                ):
                    message = (
                        "삭제 후 남은 Media 스냅샷이 예상과 다릅니다. 동시 변경 여부를 "
                        "Plex에서 즉시 점검하세요."
                    )
                    self._lock_group(group, "delete_postcheck_snapshot_changed")
                    self._finish_log(
                        log,
                        "critical",
                        message,
                        response_status=response_status,
                        after_json=_json(after.as_dict()),
                    )
                    raise RuntimeError(message)

                self.lease_service.renew(
                    lease_owner_token, lease_owner_kind, lease_owner_ref
                )
                candidate.deleted = True
                candidate.deleted_at = datetime.now()
                group.safe_to_delete = False
                group.resolution_status = "rescan_required"
                group.safety_flags_json = _json(["rescan_required_after_delete"])
                run.successful_deletions = (run.successful_deletions or 0) + 1
                log.status = "success"
                log.message = "삭제 후 Plex 재검증 완료"
                log.response_status = response_status
                log.after_json = _json(after.as_dict())
                post_scan_jobs = []
                if post_scan_mode != "none":
                    batch_run_id: Optional[int] = None
                    if lease_owner_kind == "batch":
                        try:
                            batch_run_id = int(lease_owner_ref)
                        except (TypeError, ValueError):
                            raise RuntimeError(
                                "일괄 삭제의 작업 식별자를 확인할 수 없습니다."
                            )
                    post_scan_jobs = self.post_delete_scan_manager.enqueue_confirmed(
                        run=run,
                        group=group,
                        candidate=candidate,
                        action_log=log,
                        current_item=current,
                        section_locations=post_scan_locations,
                        mode=post_scan_mode,
                        batch_run_id=batch_run_id,
                    )
                F.db.session.commit()
                return {
                    "action_id": log.id,
                    "deleted_media_id": candidate.media_id,
                    "kept_media_id": keep.media_id,
                    "response_status": response_status,
                    "verification": "confirmed",
                    "post_delete_scan": {
                        "mode": post_scan_mode,
                        "status": "queued" if post_scan_jobs else "disabled",
                        "job_ids": [job.id for job in post_scan_jobs if job.id is not None],
                    },
                }
            except DeletionLeaseLost:
                # The recovery CAS owner is now solely responsible for turning
                # validating/deleting into blocked/unknown. Do not race it.
                F.db.session.rollback()
                raise
            except Exception as exc:
                F.db.session.rollback()
                log = F.db.session.query(ModelActionLog).filter_by(id=log.id).first()
                if log is not None and log.status not in (
                    "unknown",
                    "blocked",
                    "verification_failed",
                    "critical",
                    "success",
                ):
                    if log.status == "deleting":
                        self._lock_group(group, "delete_outcome_unknown")
                        log.status = "unknown"
                        log.message = (
                            "삭제 요청 결과를 확정할 수 없습니다. 자동 재시도하지 말고 Plex를 확인하세요."
                        )
                    else:
                        self._lock_group(group, "delete_precheck_blocked")
                        log.status = "blocked"
                        log.message = str(exc)[:2000]
                    F.db.session.commit()
                raise
