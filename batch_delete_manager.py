from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from framework import F

from .delete_service import DeleteService
from .deletion_lease import DeletionLeaseLost, DeletionLeaseService
from .models import (
    ModelActionLog,
    ModelBatchItem,
    ModelBatchRun,
    ModelDuplicateGroup,
    ModelMediaCandidate,
    ModelScanRun,
)
from .path_conflicts import candidate_paths, cross_group_path_conflicts
from .scan_manager import _config_snapshot, current_safety_policy, current_score_config
from .setup import P


_PREVIEW_SECONDS = 120
_TERMINAL_STATUSES = {
    "completed",
    "cancelled",
    "stopped",
    "interrupted",
    "expired",
}
_FAILURE_ITEM_STATUSES = {
    "failed",
    "blocked",
    "unknown",
    "verification_failed",
    "critical",
}
_SKIPPED_ITEM_STATUSES = {"skipped", "cancelled", "interrupted"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _setting_int(key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(P.ModelSetting.get(key) or str(default))))
    except (TypeError, ValueError):
        return default


def _delete_enabled() -> bool:
    return P.ModelSetting.get("setting_delete_enabled") == "True"


def _batch_enabled() -> bool:
    return P.ModelSetting.get("setting_batch_delete_enabled") == "True"


def _max_delete_per_run() -> int:
    return _setting_int("setting_max_delete_per_run", 1, 1, 100)


def _batch_max_items() -> int:
    return _setting_int("setting_batch_max_items", 10, 1, 100)


def _nonce_hash(nonce: str) -> str:
    return hashlib.sha256(str(nonce).encode("utf-8")).hexdigest()


def _expire_session() -> None:
    """Force cross-worker CAS/cancellation state to be read from the database."""
    expire_all = getattr(F.db.session, "expire_all", None)
    if callable(expire_all):
        expire_all()


class BatchDeleteManager:
    """Persisted, explicitly-approved sequential deletion coordinator.

    The manager never stores a Plex token, never retries DELETE, and delegates
    every mutation and its fresh Plex validation to :class:`DeleteService`.
    Database compare-and-swap transitions protect worker/item claims, while a
    nullable unique lease permits only one approved batch across all web workers.
    """

    def __init__(self, delete_service: Optional[DeleteService] = None) -> None:
        self.delete_service = delete_service or DeleteService()
        self.lease_service = DeletionLeaseService()
        self.last_delete_recovery_counts = {"blocked": 0, "unknown": 0}
        self._threads: Dict[int, threading.Thread] = {}
        self._thread_lock = threading.Lock()
        self._unloading = threading.Event()

    @staticmethod
    def _require_enabled() -> None:
        if not _delete_enabled():
            raise RuntimeError("설정에서 수동 삭제를 먼저 활성화해야 합니다.")
        if not _batch_enabled():
            raise RuntimeError("설정에서 일괄 승인 삭제를 먼저 활성화해야 합니다.")

    @staticmethod
    def _assert_settings_snapshot(run: ModelScanRun) -> None:
        stored = _json_load(run.settings_snapshot_json, None)
        current = json.loads(
            _json(_config_snapshot(current_score_config(), current_safety_policy()))
        )
        if not isinstance(stored, dict) or stored != current:
            raise RuntimeError("점수 또는 안전 설정이 스캔 이후 변경되었습니다. 다시 스캔하세요.")

    @staticmethod
    def _eligible_pair(
        group: ModelDuplicateGroup,
    ) -> Optional[Tuple[ModelMediaCandidate, ModelMediaCandidate]]:
        if not group.safe_to_delete or group.resolution_status != "open":
            return None
        candidates = ModelMediaCandidate.by_group(group.id, include_deleted=False)
        if len(candidates) != 2 or group.recommended_candidate_id is None:
            return None
        highest = max(float(candidate.score or 0) for candidate in candidates)
        winners = [
            candidate
            for candidate in candidates
            if abs(float(candidate.score or 0) - highest) < 0.0001
        ]
        if len(winners) != 1 or winners[0].id != group.recommended_candidate_id:
            return None
        keep = winners[0]
        targets = [candidate for candidate in candidates if candidate.id != keep.id]
        if len(targets) != 1:
            return None
        return keep, targets[0]

    @staticmethod
    def _cross_group_path_conflicts(run_id: int) -> set:
        return cross_group_path_conflicts(run_id)

    def preview(self, run_id: int) -> Dict[str, Any]:
        self._require_enabled()
        with F.app.app_context():
            run = ModelScanRun.get(run_id)
            if run is None:
                raise ValueError("스캔 이력을 찾을 수 없습니다.")
            if run.status not in ("completed", "completed_with_warnings"):
                raise RuntimeError("완료된 스캔 결과만 일괄 계획에 사용할 수 있습니다.")
            self._assert_settings_snapshot(run)

            remaining = max(0, _max_delete_per_run() - int(run.deletion_attempts or 0))
            plan_limit = min(_batch_max_items(), remaining)
            if plan_limit <= 0:
                raise RuntimeError("이 스캔의 삭제 시도 개수 상한에 도달했습니다.")

            conflicts = self._cross_group_path_conflicts(run.id)
            pairs: List[Tuple[ModelDuplicateGroup, ModelMediaCandidate, ModelMediaCandidate]] = []
            for group in ModelDuplicateGroup.safe_open_by_run(run.id):
                if group.id in conflicts:
                    continue
                pair = self._eligible_pair(group)
                if pair is None:
                    continue
                pairs.append((group, pair[0], pair[1]))
                if len(pairs) >= plan_limit:
                    break
            if not pairs:
                raise RuntimeError(
                    "일괄 처리 가능한 그룹이 없습니다. 안전·미처리·2개 버전·단독 유지 추천 조건을 확인하세요."
                )

            nonce = secrets.token_urlsafe(32)
            now = datetime.now()
            expires = now + timedelta(seconds=_PREVIEW_SECONDS)
            batch = ModelBatchRun(
                scan_run_id=run.id,
                created_at=now,
                expires_at=expires,
                status="preview",
                nonce_hash=_nonce_hash(nonce),
                total_items=len(pairs),
                current_message="사용자 일괄 승인 대기 중",
            )
            try:
                F.db.session.add(batch)
                F.db.session.flush()
                batch.confirmation = "BATCH DELETE %s ITEMS %s" % (batch.id, len(pairs))
                for group, keep, target in pairs:
                    F.db.session.add(
                        ModelBatchItem(
                            batch_run_id=batch.id,
                            scan_run_id=run.id,
                            group_id=group.id,
                            keep_candidate_id=keep.id,
                            delete_candidate_id=target.id,
                            created_at=now,
                            status="planned",
                            message="승인 대기 중",
                            title=group.title or group.grandparent_title or "",
                            media_type=group.media_type or "",
                            keep_media_id=keep.media_id,
                            delete_media_id=target.media_id,
                            keep_score=keep.score or 0,
                            delete_score=target.score or 0,
                            keep_paths_json=_json(candidate_paths(keep)),
                            delete_paths_json=_json(candidate_paths(target)),
                        )
                    )
                F.db.session.commit()
            except Exception:
                F.db.session.rollback()
                raise

            payload = self._status_locked(batch.id)
            payload.update(
                {
                    "nonce": nonce,
                    "confirmation": batch.confirmation,
                    "expires_at": int(time.time()) + _PREVIEW_SECONDS,
                }
            )
            return payload

    def _validate_plan_unchanged(self, batch: ModelBatchRun) -> ModelScanRun:
        run = ModelScanRun.get(batch.scan_run_id)
        if run is None or run.status not in ("completed", "completed_with_warnings"):
            raise RuntimeError("원본 스캔이 더 이상 일괄 삭제 가능한 상태가 아닙니다.")
        self._assert_settings_snapshot(run)
        items = ModelBatchItem.by_batch(batch.id)
        conflicts = self._cross_group_path_conflicts(run.id)
        current_limit = min(
            _batch_max_items(),
            max(0, _max_delete_per_run() - int(run.deletion_attempts or 0)),
        )
        if not items or len(items) != int(batch.total_items or 0) or len(items) > current_limit:
            raise RuntimeError("삭제 가능 수가 계획 이후 변경되었습니다. 다시 사전확인하세요.")
        for item in items:
            group = ModelDuplicateGroup.get(item.group_id)
            if group is None or group.run_id != run.id or group.id in conflicts:
                raise RuntimeError("계획의 중복 그룹을 다시 확인할 수 없습니다.")
            pair = self._eligible_pair(group)
            if (
                pair is None
                or pair[0].id != item.keep_candidate_id
                or pair[1].id != item.delete_candidate_id
                or pair[0].media_id != item.keep_media_id
                or pair[1].media_id != item.delete_media_id
            ):
                raise RuntimeError("계획 항목이 변경되었습니다. 다시 스캔하고 사전확인하세요.")
        return run

    def approve(
        self, batch_id: int, nonce: str, confirmation: str
    ) -> Dict[str, Any]:
        self._require_enabled()
        digest = _nonce_hash(nonce)
        lease_token = ""
        with F.app.app_context():
            batch = ModelBatchRun.get(batch_id)
            if batch is None:
                raise ValueError("일괄 삭제 계획을 찾을 수 없습니다.")
            now = datetime.now()
            if batch.status != "preview" or batch.expires_at < now:
                raise ValueError("일괄 삭제 사전확인이 만료되었거나 이미 사용되었습니다.")
            if not batch.nonce_hash or not secrets.compare_digest(batch.nonce_hash, digest):
                raise ValueError("일괄 삭제 사전확인 정보가 일치하지 않습니다.")
            if not secrets.compare_digest(
                str(batch.confirmation or ""), str(confirmation or "")
            ):
                raise ValueError("일괄 삭제 확인 문구가 일치하지 않습니다.")
            lease_token = self.lease_service.acquire("batch", str(batch.id))
            try:
                _expire_session()
                batch = ModelBatchRun.get(batch_id)
                if batch is None or batch.status != "preview" or batch.expires_at < datetime.now():
                    raise RuntimeError("일괄 삭제 계획 상태가 변경되었습니다.")
                active = ModelBatchRun.active()
                if active is not None and active.id != batch.id:
                    raise RuntimeError("다른 일괄 삭제 작업이 이미 실행 중입니다.")
                self._validate_plan_unchanged(batch)
                if not ModelBatchRun.claim_for_approval(
                    batch.id, digest, lease_token, datetime.now()
                ):
                    F.db.session.rollback()
                    raise RuntimeError("다른 요청이 이 일괄 삭제 계획을 이미 승인했습니다.")
                # ``lease_key`` is a secondary batch-only guard; the singleton
                # deletion lease also excludes manual DELETE transactions.
                F.db.session.commit()
            except RuntimeError:
                F.db.session.rollback()
                self.lease_service.release(lease_token)
                raise
            except Exception:
                F.db.session.rollback()
                self.lease_service.release(lease_token)
                # claim_for_approval/commit includes the internal deletion
                # lease token in SQL parameters. Suppress the driver exception
                # chain so request traceback logging cannot disclose it.
                raise RuntimeError("다른 삭제 작업이 이미 실행 중입니다.") from None

        self._start_worker(batch_id)
        return self.status(batch_id=batch_id)

    def _start_worker(self, batch_id: int) -> None:
        with self._thread_lock:
            existing = self._threads.get(int(batch_id))
            if existing is not None and existing.is_alive():
                return
            thread = threading.Thread(
                target=self._worker,
                args=(int(batch_id),),
                name="plex-dupefinder-batch-%s" % batch_id,
                daemon=True,
            )
            self._threads[int(batch_id)] = thread
            try:
                thread.start()
            except Exception as exc:
                self._threads.pop(int(batch_id), None)
                with F.app.app_context():
                    F.db.session.rollback()
                    self._mark_remaining(
                        batch_id, "interrupted", "worker를 시작하지 못해 실행하지 않음"
                    )
                    self._finish_batch(
                        batch_id, "interrupted", "백그라운드 worker 시작 실패", str(exc)
                    )
                raise RuntimeError("일괄 삭제 worker를 시작할 수 없습니다.") from exc

    @staticmethod
    def _refresh_counts(batch: ModelBatchRun, items: List[ModelBatchItem]) -> None:
        batch.total_items = len(items)
        batch.succeeded_items = len([item for item in items if item.status == "success"])
        batch.failed_items = len([item for item in items if item.status in _FAILURE_ITEM_STATUSES])
        batch.skipped_items = len([item for item in items if item.status in _SKIPPED_ITEM_STATUSES])
        batch.processed_items = batch.succeeded_items + batch.failed_items

    @staticmethod
    def _mark_remaining(batch_id: int, status: str, message: str) -> None:
        now = datetime.now()
        for item in ModelBatchItem.by_batch(batch_id):
            if item.status == "planned":
                item.status = status
                item.message = message
                item.finished_at = now

    def _finish_batch(
        self,
        batch_id: int,
        status: str,
        message: str,
        error_summary: str = "",
        release_deletion_lease: bool = True,
    ) -> None:
        batch = ModelBatchRun.get(batch_id)
        if batch is None:
            return
        deletion_lease_token = getattr(batch, "deletion_lease_token", "") or ""
        items = ModelBatchItem.by_batch(batch.id)
        self._refresh_counts(batch, items)
        batch.status = status
        batch.finished_at = datetime.now()
        batch.current_message = message[:512]
        batch.error_summary = error_summary[:4000]
        batch.nonce_hash = ""
        batch.lease_key = None
        batch.deletion_lease_token = ""
        F.db.session.commit()
        if release_deletion_lease and deletion_lease_token:
            self.lease_service.release(deletion_lease_token)

    def _worker_should_stop(self, batch_id: int) -> Optional[Tuple[str, str, str]]:
        _expire_session()
        batch = ModelBatchRun.get(batch_id)
        if batch is None:
            return ("interrupted", "작업 정보를 찾을 수 없어 중단됨", "interrupted")
        # Establish that this worker still owns the cross-process deletion
        # lease before it is allowed to mutate the batch for *any* stop reason.
        # An expired owner can otherwise observe unload/cancel first and race
        # the recovery CAS winner while marking remaining items terminal.
        try:
            self.lease_service.renew(
                batch.deletion_lease_token, "batch", str(batch.id)
            )
        except DeletionLeaseLost as exc:
            return ("lease_lost", str(exc), "")
        if self._unloading.is_set():
            return ("interrupted", "플러그인 종료로 작업이 중단됨", "interrupted")
        if batch.cancellation_requested or batch.status == "cancelling":
            return ("cancelled", "사용자 요청으로 일괄 삭제 취소", "cancelled")
        if not _delete_enabled() or not _batch_enabled():
            return ("stopped", "삭제 설정이 꺼져 다음 항목 전에 중단됨", "skipped")
        run = ModelScanRun.get(batch.scan_run_id)
        try:
            if run is None:
                raise RuntimeError("원본 스캔을 찾을 수 없습니다.")
            self._assert_settings_snapshot(run)
        except Exception as exc:
            return ("stopped", str(exc), "skipped")
        return None

    def _worker(self, batch_id: int) -> None:
        try:
            with F.app.app_context():
                batch = ModelBatchRun.get(batch_id)
                if batch is None:
                    return
                self.lease_service.renew(
                    batch.deletion_lease_token, "batch", str(batch.id)
                )
                if not ModelBatchRun.claim_for_worker(batch_id, datetime.now()):
                    F.db.session.rollback()
                    return
                F.db.session.commit()
                item_ids = [item.id for item in ModelBatchItem.by_batch(batch_id)]
                for item_id in item_ids:
                    stop = self._worker_should_stop(batch_id)
                    if stop is not None:
                        terminal, message, item_status = stop
                        if terminal == "lease_lost":
                            P.logger.warning(
                                "Batch worker lost DB deletion lease; recovery owner will finalize plan %s",
                                batch_id,
                            )
                            return
                        self._mark_remaining(batch_id, item_status, message)
                        self._finish_batch(batch_id, terminal, message)
                        return

                    if not ModelBatchItem.claim_for_worker(item_id, datetime.now()):
                        F.db.session.rollback()
                        self._mark_remaining(
                            batch_id,
                            "skipped",
                            "항목 선점 실패로 안전 중단",
                        )
                        self._finish_batch(
                            batch_id,
                            "stopped",
                            "다른 worker가 항목을 처리 중이어서 중단됨",
                        )
                        return
                    F.db.session.commit()
                    _expire_session()
                    item = ModelBatchItem.get(item_id)
                    batch = ModelBatchRun.get(batch_id)
                    if item is None or batch is None:
                        raise RuntimeError("일괄 삭제 작업 정보를 다시 읽을 수 없습니다.")
                    batch.current_message = "Group #%s 삭제 전 재검증 중" % item.group_id
                    F.db.session.commit()

                    try:
                        result = self.delete_service.delete(
                            group_id=item.group_id,
                            candidate_id=item.delete_candidate_id,
                            keep_candidate_id=item.keep_candidate_id,
                            confirmation="DELETE %s" % item.delete_media_id,
                            lease_owner_token=batch.deletion_lease_token,
                            lease_owner_kind="batch",
                            lease_owner_ref=str(batch.id),
                        )
                    except DeletionLeaseLost:
                        # An expired lease can only be finalized by the worker
                        # that won the DB recovery CAS.
                        F.db.session.rollback()
                        return
                    except Exception as exc:
                        F.db.session.rollback()
                        _expire_session()
                        item = ModelBatchItem.get(item_id)
                        batch = ModelBatchRun.get(batch_id)
                        if item is None or batch is None:
                            raise
                        log = ModelActionLog.latest_for_delete(
                            item.scan_run_id, item.group_id, item.delete_candidate_id
                        )
                        item.status = (
                            log.status
                            if log is not None and log.status in _FAILURE_ITEM_STATUSES
                            else "failed"
                        )
                        item.action_log_id = log.id if log is not None else None
                        item.message = (
                            log.message if log is not None else str(exc)
                        )[:2000]
                        item.finished_at = datetime.now()
                        self._mark_remaining(
                            batch_id,
                            "skipped",
                            "이전 항목 실패로 실행하지 않음",
                        )
                        F.db.session.commit()
                        self._finish_batch(
                            batch_id,
                            "stopped",
                            "항목 실패로 즉시 중단됨",
                            str(exc),
                        )
                        return

                    _expire_session()
                    item = ModelBatchItem.get(item_id)
                    batch = ModelBatchRun.get(batch_id)
                    if item is None or batch is None:
                        raise RuntimeError("삭제 후 작업 정보를 다시 읽을 수 없습니다.")
                    item.status = "success"
                    item.message = "삭제 후 Plex 재검증 완료"
                    item.action_log_id = result.get("action_id")
                    item.finished_at = datetime.now()
                    items = ModelBatchItem.by_batch(batch_id)
                    self._refresh_counts(batch, items)
                    batch.current_message = "Group #%s 완료" % item.group_id
                    F.db.session.commit()

                self._finish_batch(batch_id, "completed", "일괄 승인 삭제 완료")
        except DeletionLeaseLost:
            P.logger.warning(
                "Batch worker could not renew DB deletion lease; recovery owner will finalize plan %s",
                batch_id,
            )
        except Exception as exc:
            try:
                with F.app.app_context():
                    F.db.session.rollback()
                    self._mark_remaining(batch_id, "interrupted", "worker 예외로 실행하지 않음")
                    self._finish_batch(batch_id, "interrupted", "worker 예외로 중단됨", str(exc))
            except Exception:
                P.logger.error("Batch delete recovery failed for plan %s", batch_id)
            P.logger.exception("Batch delete worker failed: plan=%s", batch_id)
        finally:
            try:
                with F.app.app_context():
                    F.db.session.remove()
            except Exception:
                pass
            with self._thread_lock:
                self._threads.pop(int(batch_id), None)

    def _status_locked(self, batch_id: int) -> Dict[str, Any]:
        batch = ModelBatchRun.get(batch_id)
        if batch is None:
            raise ValueError("일괄 삭제 계획을 찾을 수 없습니다.")
        payload = batch.as_api()
        payload["items"] = [item.as_api() for item in ModelBatchItem.by_batch(batch.id)]
        return payload

    def status(
        self, batch_id: Optional[int] = None, run_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        with F.app.app_context():
            batch: Optional[ModelBatchRun]
            if batch_id is not None:
                batch = ModelBatchRun.get(batch_id)
            elif run_id is not None:
                batch = ModelBatchRun.latest_for_scan(run_id)
            else:
                batch = ModelBatchRun.active()
            if batch is None:
                return None
            return self._status_locked(batch.id)

    def cancel(self, batch_id: int) -> Dict[str, Any]:
        with F.app.app_context():
            batch = ModelBatchRun.get(batch_id)
            if batch is None:
                raise ValueError("일괄 삭제 계획을 찾을 수 없습니다.")
            if batch.status in _TERMINAL_STATUSES:
                return self._status_locked(batch.id)
            now = datetime.now()
            if batch.status == "preview":
                batch.status = "cancelled"
                batch.finished_at = now
                batch.current_message = "승인 전 계획 취소"
                batch.nonce_hash = ""
                self._mark_remaining(batch.id, "cancelled", "승인 전 계획 취소")
                self._refresh_counts(batch, ModelBatchItem.by_batch(batch.id))
                F.db.session.commit()
                return self._status_locked(batch.id)

            # queued -> cancelled wins against worker's queued -> running CAS.
            deletion_lease_token = getattr(batch, "deletion_lease_token", "") or ""
            updated = (
                F.db.session.query(ModelBatchRun)
                .filter(ModelBatchRun.id == batch.id, ModelBatchRun.status == "queued")
                .update(
                    {
                        ModelBatchRun.status: "cancelled",
                        ModelBatchRun.cancellation_requested: True,
                        ModelBatchRun.finished_at: now,
                        ModelBatchRun.current_message: "시작 전 사용자 취소",
                        ModelBatchRun.lease_key: None,
                        ModelBatchRun.deletion_lease_token: "",
                    },
                    synchronize_session=False,
                )
            )
            if updated == 1:
                F.db.session.commit()
                _expire_session()
                self._mark_remaining(batch.id, "cancelled", "시작 전 사용자 취소")
                batch = ModelBatchRun.get(batch.id)
                if batch is not None:
                    self._refresh_counts(batch, ModelBatchItem.by_batch(batch.id))
                F.db.session.commit()
                if deletion_lease_token:
                    self.lease_service.release(deletion_lease_token)
                return self._status_locked(batch_id)

            updated = (
                F.db.session.query(ModelBatchRun)
                .filter(ModelBatchRun.id == batch.id, ModelBatchRun.status == "running")
                .update(
                    {
                        ModelBatchRun.status: "cancelling",
                        ModelBatchRun.cancellation_requested: True,
                        ModelBatchRun.current_message: "현재 항목 검증 후 취소 예정",
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                F.db.session.rollback()
                batch = ModelBatchRun.get(batch_id)
                if batch is None:
                    raise RuntimeError("취소 중 작업 상태를 다시 확인할 수 없습니다.")
                if batch.status not in _TERMINAL_STATUSES and batch.status != "cancelling":
                    raise RuntimeError("현재 상태에서는 일괄 삭제를 취소할 수 없습니다.")
            else:
                F.db.session.commit()
                _expire_session()
            return self._status_locked(batch_id)

    def recover_interrupted(self) -> int:
        """Recover only while holding the DB singleton's recovery CAS lease."""
        self.last_delete_recovery_counts = {"blocked": 0, "unknown": 0}
        recovery_state = self.lease_service.recovery_state()
        if recovery_state == "busy":
            return 0
        if recovery_state == "free":
            with F.app.app_context():
                if not ModelBatchRun.unfinished() and not ModelActionLog.interrupted():
                    return 0
        recovery_claim = self.lease_service.acquire_for_recovery()
        if recovery_claim is None:
            # A valid manual or batch owner belongs to another live web worker.
            return 0
        try:
            self.last_delete_recovery_counts = self.delete_service.recover_interrupted()
            with F.app.app_context():
                batches = ModelBatchRun.unfinished()
                now = datetime.now()
                for batch in batches:
                    self._mark_remaining(
                        batch.id,
                        "interrupted",
                        "FlaskFarm 재시작으로 실행하지 않음",
                    )
                    for item in ModelBatchItem.by_batch(batch.id):
                        if item.status == "running":
                            log = ModelActionLog.latest_for_delete(
                                item.scan_run_id, item.group_id, item.delete_candidate_id
                            )
                            if log is not None and log.status in (
                                "success",
                                "blocked",
                                "unknown",
                                "verification_failed",
                                "critical",
                            ):
                                item.status = log.status
                                item.action_log_id = log.id
                                item.message = log.message or "삭제 감사 이력에서 복구됨"
                            else:
                                item.status = "unknown"
                                item.message = "FlaskFarm 재시작으로 삭제 결과를 확정할 수 없음"
                            item.finished_at = now
                    batch.status = "interrupted"
                    batch.finished_at = now
                    batch.current_message = "재시작으로 보수적 중단 · 자동 재개하지 않음"
                    batch.nonce_hash = ""
                    batch.lease_key = None
                    batch.deletion_lease_token = ""
                    self._refresh_counts(batch, ModelBatchItem.by_batch(batch.id))
                F.db.session.commit()
                return len(batches)
        finally:
            self.lease_service.release(recovery_claim.token)

    def live_delete_keys(self) -> set:
        """Audit keys protected by a valid DB-owned batch lease."""
        batch_id = self.lease_service.active_batch_id()
        if batch_id is None:
            return set()
        keys = set()
        with F.app.app_context():
            for item in ModelBatchItem.by_batch(batch_id):
                if item.status == "running":
                    keys.add(
                        (
                            int(item.scan_run_id),
                            int(item.group_id),
                            int(item.delete_candidate_id),
                        )
                    )
        return keys

    def unload(self) -> None:
        self._unloading.set()
        deadline = time.monotonic() + 10.0
        with self._thread_lock:
            threads = list(self._threads.values())
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        alive = [thread.name for thread in threads if thread.is_alive()]
        if alive:
            P.logger.warning(
                "Batch delete worker still verifying during unload; it will not start another item: %s",
                ", ".join(alive),
            )
