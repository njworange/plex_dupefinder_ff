from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any, Dict, Optional, Set, Tuple

from framework import F

from .deletion_lease import DeletionLeaseLost, DeletionLeaseService
from .models import ModelActionLog, ModelDuplicateGroup, ModelMediaCandidate, ModelScanRun
from .path_conflicts import group_has_cross_path_conflict
from .scan_manager import current_safety_policy
from .services.plex_gateway import PlexDeleteOutcomeUnknown, PlexGateway
from .services.plex_mate_provider import PlexMateProvider
from .services.post_delete_scan_targets import build_scan_targets, validate_scan_target
from .services.safety import assess_group, validate_fresh_snapshot
from .setup import P


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _delete_enabled() -> bool:
    return P.ModelSetting.get("setting_delete_enabled") == "True"


def _max_delete_per_run() -> int:
    try:
        return max(1, min(100, int(P.ModelSetting.get("setting_max_delete_per_run") or "1")))
    except (TypeError, ValueError):
        return 1


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

    @staticmethod
    def _post_delete_scan_mode() -> str:
        value = str(
            P.ModelSetting.get("setting_post_delete_scan_mode") or "none"
        ).strip().lower()
        return value if value in ("none", "binary", "web") else "none"

    def wake_post_delete_scans(self) -> None:
        if self.post_delete_scan_manager is not None:
            self.post_delete_scan_manager.wake()

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
        run: ModelScanRun, log: ModelActionLog, before_json: str
    ) -> None:
        try:
            if not ModelScanRun.claim_deletion_slot(run.id, _max_delete_per_run()):
                F.db.session.rollback()
                raise RuntimeError("이 스캔의 삭제 시도 개수 상한에 도달했습니다.")
            log.before_json = before_json
            log.status = "deleting"
            log.message = "Plex에 Media 삭제 요청 전송"
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
        self, exclude_delete_keys: Optional[Set[Tuple[int, int, int]]] = None
    ) -> Dict[str, int]:
        """Conservatively recover audit rows left mid-delete by a process restart."""
        counts = {"blocked": 0, "unknown": 0}
        excluded = exclude_delete_keys or set()
        with self._lock, F.app.app_context():
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
        lease_owner_token: str,
        lease_owner_kind: str,
        lease_owner_ref: str,
    ) -> Dict[str, Any]:

        with F.app.app_context():
            run, group, candidate, keep = self._load(group_id, candidate_id, keep_candidate_id)
            expected_confirmation = "DELETE %s" % candidate.media_id
            if str(confirmation) != expected_confirmation:
                raise ValueError("확인 문구가 일치하지 않습니다: %s" % expected_confirmation)
            if not group.safe_to_delete or group.resolution_status != "open":
                raise RuntimeError("이 그룹은 안전 삭제 조건을 충족하지 않습니다. 다시 스캔하세요.")
            if run.status not in ("completed", "completed_with_warnings"):
                raise RuntimeError("완료된 스캔의 결과만 삭제에 사용할 수 있습니다.")
            if (run.deletion_attempts or 0) >= _max_delete_per_run():
                raise RuntimeError("이 스캔의 삭제 시도 개수 상한에 도달했습니다.")

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
                if post_scan_mode != "none":
                    if self.post_delete_scan_manager is None:
                        raise RuntimeError(
                            "삭제 후 Plex 스캔 관리자가 초기화되지 않았습니다."
                        )
                    expected_section_type = (
                        "show" if group.media_type == "episode" else "movie"
                    )
                    section = next(
                        (
                            item
                            for item in gateway.list_sections()
                            if item.key == str(group.section_key)
                        ),
                        None,
                    )
                    if section is None or section.section_type != expected_section_type:
                        raise RuntimeError(
                            "삭제 대상의 Plex library section을 확인할 수 없습니다."
                        )
                    post_scan_locations = tuple(section.locations)
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
