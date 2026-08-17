from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from framework import F

from .delete_budget import delete_attempt_budget
from .delete_service import DeleteService
from .deletion_lease import DeletionLeaseLost, DeletionLeaseService
from .models import (
    ModelActionLog,
    ModelBatchExclusion,
    ModelBatchItem,
    ModelBatchRun,
    ModelDirectDeleteJournal,
    ModelDuplicateGroup,
    ModelMediaCandidate,
    ModelQuarantineJournal,
    ModelScanRun,
)
from .path_conflicts import candidate_paths, cross_group_path_conflicts
from .scan_manager import _config_snapshot, current_safety_policy, current_score_config
from .services.score_engine import stable_media_id_key
from .setup import P


_PREVIEW_SECONDS = 120
_TERMINAL_STATUSES = {
    "completed",
    "completed_with_errors",
    "completed_with_warnings",
    "cancelled",
    "stopped",
    "interrupted",
    "expired",
    "scan_pending",
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


def _delete_enabled() -> bool:
    return P.ModelSetting.get("setting_delete_enabled") == "True"


def _batch_enabled() -> bool:
    return P.ModelSetting.get("setting_batch_delete_enabled") == "True"


def _delete_backend() -> str:
    value = str(P.ModelSetting.get("setting_delete_backend") or "plex").strip().lower()
    if value not in ("plex", "quarantine", "direct"):
        raise RuntimeError("파일 처리 방식 설정이 올바르지 않습니다.")
    return value


def _post_delete_scan_mode() -> str:
    return str(P.ModelSetting.get("setting_post_delete_scan_mode") or "none").strip().lower()


def _nonce_hash(nonce: str) -> str:
    return hashlib.sha256(str(nonce).encode("utf-8")).hexdigest()


def _expire_session() -> None:
    """Force cross-worker CAS/cancellation state to be read from the database."""
    expire_all = getattr(F.db.session, "expire_all", None)
    if callable(expire_all):
        expire_all()


class BatchDeleteManager:
    """Persisted, server-validated sequential deletion coordinator.

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

    def _wake_post_delete_scans(self) -> None:
        wake = getattr(self.delete_service, "wake_post_delete_scans", None)
        if callable(wake):
            wake()

    @staticmethod
    def _require_enabled() -> None:
        if not _delete_enabled():
            raise RuntimeError("설정에서 선택 버전 삭제를 먼저 활성화해야 합니다.")
        if not _batch_enabled():
            raise RuntimeError("설정에서 중복 자동 정리를 먼저 활성화해야 합니다.")

    @staticmethod
    def _assert_settings_snapshot(run: ModelScanRun) -> None:
        stored = _json_load(run.settings_snapshot_json, None)
        current = json.loads(
            _json(_config_snapshot(current_score_config(), current_safety_policy()))
        )
        if not isinstance(stored, dict) or stored != current:
            raise RuntimeError("점수 또는 안전 설정이 스캔 이후 변경되었습니다. 다시 스캔하세요.")

    @staticmethod
    def _winner_key(candidate: ModelMediaCandidate) -> tuple:
        return (
            stable_media_id_key(getattr(candidate, "media_id", "")),
            int(getattr(candidate, "id", 0) or 0),
        )

    @classmethod
    def _highest_score_winners(
        cls, candidates: Sequence[ModelMediaCandidate]
    ) -> Tuple[ModelMediaCandidate, ...]:
        highest = max(float(candidate.score or 0) for candidate in candidates)
        return tuple(
            candidate
            for candidate in candidates
            if abs(float(candidate.score or 0) - highest) < 0.0001
        )

    @classmethod
    def _eligible_group(
        cls, group: ModelDuplicateGroup,
    ) -> Optional[Tuple[ModelMediaCandidate, Tuple[ModelMediaCandidate, ...]]]:
        if not group.safe_to_delete or group.resolution_status != "open":
            return None
        candidates = ModelMediaCandidate.by_group(group.id, include_deleted=False)
        if len(candidates) < 2:
            return None
        winners = cls._highest_score_winners(candidates)
        keep = min(winners, key=cls._winner_key)
        stored_recommendation = group.recommended_candidate_id
        if stored_recommendation is not None and keep.id != stored_recommendation:
            return None
        if len(winners) == 1 and stored_recommendation is None:
            return None
        targets = tuple(candidate for candidate in candidates if candidate.id != keep.id)
        if not targets:
            return None
        return keep, targets

    @classmethod
    def _eligible_pair(
        cls, group: ModelDuplicateGroup
    ) -> Optional[Tuple[ModelMediaCandidate, ModelMediaCandidate]]:
        """Compatibility helper for legacy callers that require one target."""

        planned = cls._eligible_group(group)
        if planned is None or len(planned[1]) != 1:
            return None
        return planned[0], planned[1][0]

    @staticmethod
    def _excluded_group(
        group: ModelDuplicateGroup, reason: str, message: str
    ) -> Dict[str, Any]:
        return {
            "group_id": int(group.id),
            "title": str(group.title or group.grandparent_title or ""),
            "media_type": str(group.media_type or ""),
            "reason": str(reason),
            "message": str(message),
        }

    @classmethod
    def _plan_groups(
        cls,
        run_id: int,
        conflicts: set,
        backend: str,
    ) -> Tuple[
        List[Tuple[ModelDuplicateGroup, ModelMediaCandidate, ModelMediaCandidate]],
        List[Dict[str, Any]],
        int,
    ]:
        """Plan every unambiguous target and explain every excluded group."""

        loader = getattr(ModelDuplicateGroup, "all_by_run", None)
        groups = (
            loader(run_id)
            if callable(loader)
            else ModelDuplicateGroup.safe_open_by_run(run_id)
        )
        pairs: List[
            Tuple[ModelDuplicateGroup, ModelMediaCandidate, ModelMediaCandidate]
        ] = []
        excluded: List[Dict[str, Any]] = []
        eligible_groups = 0
        for group in groups:
            if group.id in conflicts:
                excluded.append(
                    cls._excluded_group(
                        group,
                        "cross_group_path_conflict",
                        "다른 Plex 항목과 영상 경로를 공유하여 자동 정리하지 않습니다.",
                    )
                )
                continue
            if not group.safe_to_delete:
                flags = _json_load(getattr(group, "safety_flags_json", "[]"), [])
                suffix = " · ".join(str(value) for value in flags) if flags else "안전 조건 불충족"
                excluded.append(
                    cls._excluded_group(group, "unsafe_group", suffix)
                )
                continue
            if group.resolution_status != "open":
                excluded.append(
                    cls._excluded_group(
                        group,
                        "not_open",
                        "현재 상태(%s)는 자동 정리 대상이 아닙니다."
                        % (group.resolution_status or "unknown"),
                    )
                )
                continue
            candidates = ModelMediaCandidate.by_group(
                group.id, include_deleted=False
            )
            if len(candidates) < 2:
                excluded.append(
                    cls._excluded_group(
                        group,
                        "less_than_two_versions",
                        "현재 Media 버전이 2개 미만입니다.",
                    )
                )
                continue
            winners = cls._highest_score_winners(candidates)
            keep = min(winners, key=cls._winner_key)
            stored_recommendation = group.recommended_candidate_id
            if (
                stored_recommendation is not None
                and keep.id != stored_recommendation
            ) or (len(winners) == 1 and stored_recommendation is None):
                excluded.append(
                    cls._excluded_group(
                        group,
                        "recommendation_mismatch",
                        "현재 최고 점수 유지 결정과 저장된 유지 추천이 일치하지 않습니다.",
                    )
                )
                continue
            targets = tuple(
                candidate for candidate in candidates if candidate.id != keep.id
            )
            if backend == "quarantine" and len(targets) > 1:
                excluded.append(
                    cls._excluded_group(
                        group,
                        "quarantine_multi_version_requires_rescan",
                        "안전 격리는 각 이동 뒤 Plex 반영이 필요하여 3개 이상 버전 그룹을 "
                        "한 자동 작업에서 처리하지 않습니다.",
                    )
                )
                continue
            eligible_groups += 1
            pairs.extend((group, keep, target) for target in targets)
        return pairs, excluded, eligible_groups

    @staticmethod
    def _cross_group_path_conflicts(run_id: int) -> set:
        return cross_group_path_conflicts(run_id)

    @staticmethod
    def _batch_binding() -> Dict[str, str]:
        backend = _delete_backend()
        return {
            "backend": backend,
            "post_delete_scan_mode": _post_delete_scan_mode(),
            "quarantine_root": str(
                P.ModelSetting.get("setting_quarantine_root") or ""
            ).strip() if backend == "quarantine" else "",
        }

    @staticmethod
    def _planned_backend(batch: ModelBatchRun) -> str:
        confirmation = str(getattr(batch, "confirmation", "") or "")
        if confirmation.startswith("BATCH REVIEW QUARANTINE"):
            return "quarantine"
        if confirmation.startswith("BATCH REVIEW DIRECT"):
            return "direct"
        if confirmation.startswith("BATCH REVIEW PLEX"):
            return "plex"
        if confirmation.startswith("BATCH QUARANTINE "):
            return "quarantine"
        if confirmation.startswith("BATCH DELETE MEDIA "):
            return "direct"
        if confirmation.startswith("BATCH DELETE FILES "):
            return "legacy_direct"
        # v1.2 previews and every legacy/test row are Plex DELETE plans.
        return "plex"

    @staticmethod
    def _preview_manifest(cleanup: Dict[str, Any], binding: Dict[str, str]) -> Dict[str, Any]:
        """Persist only the approved public paths plus the exact runtime binding.

        Filesystem identity remains represented by ``plan_digest``.  The full
        private snapshots are recalculated by DeleteService immediately before
        mutation and never copied into an existing batch table.
        """

        def public_entries(values: Any) -> List[Dict[str, str]]:
            result: List[Dict[str, str]] = []
            for raw in values or []:
                if not isinstance(raw, dict):
                    continue
                path = str(raw.get("path") or raw.get("source_path") or "")
                if not path:
                    continue
                item = {
                    "path": path,
                    "source_path": path,
                    "reason": str(raw.get("reason") or ""),
                }
                if raw.get("reason_code"):
                    item["reason_code"] = str(raw["reason_code"])
                result.append(item)
            return result

        return {
            "eligible": public_entries(cleanup.get("eligible")),
            "excluded": public_entries(cleanup.get("excluded")),
            "protected": public_entries(
                cleanup.get("protected")
                or cleanup.get("protected_subtitles")
            ),
            "batch_binding": dict(binding),
        }

    @staticmethod
    def _assert_source_sets_disjoint(
        previews: Sequence[Dict[str, Any]],
        group_ids: Optional[Sequence[int]] = None,
    ) -> None:
        conflicts = BatchDeleteManager._source_conflict_group_ids(
            previews, group_ids
        )
        if conflicts:
            raise RuntimeError(
                "일괄 파일 처리 항목들이 같은 영상 또는 자막 파일을 공유합니다. "
                "개별 사전확인으로 처리하세요."
            )

    @staticmethod
    def _source_conflict_group_ids(
        previews: Sequence[Dict[str, Any]],
        group_ids: Optional[Sequence[int]] = None,
    ) -> set:
        """Return every group sharing a video/subtitle/protection source.

        The caller can safely remove all owners of a conflicting source while
        retaining unrelated groups.  Same-group repetitions are expected for
        multi-version direct plans and are intentionally not conflicts.
        """

        owners: Dict[str, int] = {}
        conflicts: set = set()
        for index, preview in enumerate(previews):
            group_id = int(group_ids[index]) if group_ids is not None else index
            cleanup = preview.get("subtitle_cleanup") or {}
            paths: List[str] = []
            video = cleanup.get("video") or {}
            if video.get("path"):
                paths.append(str(video["path"]))
            for collection in (
                cleanup.get("eligible") or [],
                cleanup.get("excluded") or [],
                cleanup.get("protected")
                or cleanup.get("protected_subtitles")
                or [],
            ):
                for entry in collection:
                    if isinstance(entry, dict) and (
                        entry.get("path") or entry.get("source_path")
                    ):
                        paths.append(
                            str(entry.get("path") or entry.get("source_path"))
                        )
            for path in paths:
                key = os.path.normcase(os.path.realpath(os.path.abspath(path)))
                previous = owners.get(key)
                if previous is not None and previous != group_id:
                    conflicts.add(int(previous))
                    conflicts.add(group_id)
                else:
                    owners[key] = group_id
        return conflicts

    @staticmethod
    def _preview_journal(batch_id: int, candidate_id: int) -> ModelQuarantineJournal:
        journal = ModelQuarantineJournal.for_batch_candidate(
            batch_id, candidate_id, status="batch_preview"
        )
        if journal is None or len(str(journal.plan_digest or "")) != 64:
            raise RuntimeError("승인된 안전 격리 계획을 찾을 수 없습니다. 다시 사전확인하세요.")
        return journal

    def _fresh_quarantine_preview(
        self, item: ModelBatchItem, journal: ModelQuarantineJournal
    ) -> Dict[str, Any]:
        manifest = _json_load(journal.manifest_json, {})
        stored_binding = manifest.get("batch_binding") if isinstance(manifest, dict) else None
        if stored_binding != self._batch_binding():
            raise RuntimeError("격리 또는 부분 스캔 설정이 승인 이후 변경되었습니다. 다시 사전확인하세요.")
        preview = self.delete_service.preview(
            group_id=item.group_id,
            candidate_id=item.delete_candidate_id,
            keep_candidate_id=item.keep_candidate_id,
        )
        return self._validate_quarantine_preview(item, journal, preview)

    def _validate_quarantine_preview(
        self,
        item: ModelBatchItem,
        journal: ModelQuarantineJournal,
        preview: Dict[str, Any],
    ) -> Dict[str, Any]:
        manifest = _json_load(journal.manifest_json, {})
        stored_binding = (
            manifest.get("batch_binding") if isinstance(manifest, dict) else None
        )
        if stored_binding != self._batch_binding():
            raise RuntimeError(
                "격리 또는 부분 스캔 설정이 승인 이후 변경되었습니다. 다시 사전확인하세요."
            )
        if preview.get("backend") != "quarantine" or not secrets.compare_digest(
            str(preview.get("plan_digest") or ""), str(journal.plan_digest or "")
        ):
            raise RuntimeError("자막·파일 계획이 승인 이후 변경되었습니다. 다시 사전확인하세요.")
        return preview

    @staticmethod
    def _direct_preview_journal(
        batch_id: int, candidate_id: int
    ) -> ModelDirectDeleteJournal:
        journal = ModelDirectDeleteJournal.for_batch_candidate(
            batch_id, candidate_id, status="batch_preview"
        )
        if journal is None or len(str(journal.plan_digest or "")) != 64:
            raise RuntimeError("승인된 직접 삭제 계획을 찾을 수 없습니다. 다시 사전확인하세요.")
        return journal

    def _fresh_direct_preview(
        self, item: ModelBatchItem, journal: ModelDirectDeleteJournal
    ) -> Dict[str, Any]:
        manifest = _json_load(journal.manifest_json, {})
        stored_binding = manifest.get("batch_binding") if isinstance(manifest, dict) else None
        if stored_binding != self._batch_binding():
            raise RuntimeError("직접 삭제 또는 부분 스캔 설정이 승인 이후 변경되었습니다.")
        preview = self.delete_service.preview(
            group_id=item.group_id,
            candidate_id=item.delete_candidate_id,
            keep_candidate_id=item.keep_candidate_id,
        )
        return self._validate_direct_preview(item, journal, preview)

    def _validate_direct_preview(
        self,
        item: ModelBatchItem,
        journal: ModelDirectDeleteJournal,
        preview: Dict[str, Any],
    ) -> Dict[str, Any]:
        manifest = _json_load(journal.manifest_json, {})
        stored_binding = (
            manifest.get("batch_binding") if isinstance(manifest, dict) else None
        )
        if stored_binding != self._batch_binding():
            raise RuntimeError("직접 삭제 또는 부분 스캔 설정이 승인 이후 변경되었습니다.")
        if preview.get("backend") != "direct" or not secrets.compare_digest(
            str(preview.get("plan_digest") or ""), str(journal.plan_digest or "")
        ):
            raise RuntimeError("직접 삭제할 영상·자막 계획이 승인 이후 변경되었습니다.")
        if (preview.get("subtitle_cleanup") or {}).get("executable") is False:
            raise RuntimeError("보호할 수 없는 관련 자막이 있어 새 사전확인이 필요합니다.")
        return preview

    def _preview_pairs(
        self,
        pairs: Sequence[
            Tuple[ModelDuplicateGroup, ModelMediaCandidate, ModelMediaCandidate]
        ],
    ) -> Tuple[Dict[Tuple[int, int, int], Dict[str, Any]], Dict[int, str]]:
        """Build live previews with the service's shared Plex context when available."""

        requests = tuple(
            (int(group.id), int(target.id), int(keep.id))
            for group, keep, target in pairs
        )
        preview_many = getattr(self.delete_service, "preview_many", None)
        if callable(preview_many):
            return preview_many(requests)

        # Compatibility for tests and rolling deployments whose DeleteService
        # predates the shared-context API.  Production always takes the path
        # above, where fatal provider/Plex errors abort once rather than being
        # converted into one exclusion per group.
        previews: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
        errors: Dict[int, str] = {}
        grouped: Dict[int, list] = {}
        for pair in pairs:
            grouped.setdefault(int(pair[0].id), []).append(pair)
        for group_id, group_pairs in grouped.items():
            local: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
            try:
                for group, keep, target in group_pairs:
                    key = (int(group.id), int(target.id), int(keep.id))
                    local[key] = self.delete_service.preview(
                        group.id, target.id, keep.id
                    )
                previews.update(local)
            except Exception as exc:
                errors[group_id] = str(exc) or exc.__class__.__name__
        return previews, errors

    @staticmethod
    def _normalized_exclusions(
        excluded_groups: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Normalize public review fields and keep one reason per group."""

        values = list(excluded_groups) or [
            {
                "group_id": 0,
                "title": "자동 처리 대상 없음",
                "media_type": "",
                "reason": "no_eligible_groups",
                "message": "현재 스캔에 안전하게 자동 정리할 중복 그룹이 없습니다.",
            }
        ]
        result: List[Dict[str, Any]] = []
        seen = set()
        for raw in values:
            try:
                group_id = max(0, int(raw.get("group_id", 0)))
            except (TypeError, ValueError):
                group_id = 0
            if group_id in seen:
                continue
            seen.add(group_id)
            result.append(
                {
                    "group_id": group_id,
                    "title": str(raw.get("title") or "")[:512],
                    "media_type": str(raw.get("media_type") or "")[:32],
                    "reason": str(raw.get("reason") or "excluded")[:64],
                    "message": str(raw.get("message") or "자동 처리 제외"),
                }
            )
        return result

    @staticmethod
    def _add_exclusion_rows(
        batch_id: int,
        run_id: int,
        excluded_groups: Sequence[Dict[str, Any]],
        now: datetime,
    ) -> None:
        for item in excluded_groups:
            F.db.session.add(
                ModelBatchExclusion(
                    batch_run_id=int(batch_id),
                    scan_run_id=int(run_id),
                    group_id=int(item["group_id"]),
                    created_at=now,
                    title=item["title"],
                    media_type=item["media_type"],
                    reason=item["reason"],
                    message=item["message"],
                )
            )

    def _persist_exclusion_review(
        self,
        run: ModelScanRun,
        backend: str,
        excluded_groups: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Persist a terminal, non-approvable review when nothing may run."""

        exclusions = self._normalized_exclusions(excluded_groups)
        now = datetime.now()
        batch = ModelBatchRun(
            scan_run_id=run.id,
            created_at=now,
            finished_at=now,
            expires_at=now,
            status="completed_with_warnings",
            confirmation="BATCH REVIEW %s" % str(backend).upper(),
            nonce_hash="",
            total_items=0,
            current_message="자동 처리 대상 없음 · 제외 사유 확인",
        )
        try:
            locked_run = (
                F.db.session.query(ModelScanRun)
                .filter(
                    ModelScanRun.id == int(run.id),
                    ModelScanRun.status.in_(
                        ("completed", "completed_with_warnings")
                    ),
                )
                .populate_existing()
                .with_for_update()
                .first()
            )
            if locked_run is None:
                raise RuntimeError(
                    "원본 스캔 상태가 변경되어 자동 제외 검토를 저장하지 않습니다."
                )
            self._assert_settings_snapshot(locked_run)
            F.db.session.add(batch)
            F.db.session.flush()
            self._add_exclusion_rows(batch.id, run.id, exclusions, now)
            F.db.session.commit()
        except Exception:
            F.db.session.rollback()
            raise
        payload = self._status_locked(batch.id)
        payload.update(
            {
                "backend": backend,
                "eligible_groups": 0,
                "planned_deletions": 0,
                "executable": False,
            }
        )
        return payload

    def _rebind_direct_preview(
        self, item: ModelBatchItem, journal: ModelDirectDeleteJournal
    ) -> Dict[str, Any]:
        """Bind the next exact plan after earlier targets in this group moved.

        The short-lived batch nonce approves the immutable keep/delete policy.
        A group with three or more versions necessarily has a different set of
        survivors after its first deletion, so each later filesystem manifest
        is recalculated, persisted, and then checked once more by DeleteService
        immediately before PMS DELETE.
        """

        manifest = _json_load(journal.manifest_json, {})
        stored_binding = (
            manifest.get("batch_binding") if isinstance(manifest, dict) else None
        )
        current_binding = self._batch_binding()
        if stored_binding != current_binding:
            raise RuntimeError(
                "직접 삭제 또는 부분 스캔 설정이 승인 이후 변경되었습니다."
            )

        preview = self.delete_service.preview(
            group_id=item.group_id,
            candidate_id=item.delete_candidate_id,
            keep_candidate_id=item.keep_candidate_id,
        )
        cleanup = preview.get("subtitle_cleanup") or {}
        if preview.get("backend") != "direct":
            raise RuntimeError("직접 삭제 방식이 계획 이후 변경되었습니다.")
        if cleanup.get("executable") is False:
            raise RuntimeError("보호할 수 없는 관련 자막이 있어 이 항목을 자동 정리하지 않습니다.")
        if self._batch_binding() != stored_binding:
            raise RuntimeError(
                "직접 삭제 또는 부분 스캔 설정이 재검증 중 변경되었습니다."
            )
        counts = cleanup.get("counts") or {}
        journal.plan_digest = str(preview.get("plan_digest") or "")
        if len(journal.plan_digest) != 64:
            raise RuntimeError("직접 삭제의 새 plan digest를 만들 수 없습니다.")
        journal.manifest_json = _json(
            self._preview_manifest(cleanup, stored_binding)
        )
        journal.eligible_count = int(counts.get("eligible", 0))
        journal.excluded_count = int(counts.get("excluded", 0))
        journal.protected_count = int(counts.get("protected", 0))
        journal.updated_at = datetime.now()
        F.db.session.commit()
        return preview

    def preview(self, run_id: int) -> Dict[str, Any]:
        self._require_enabled()
        with F.app.app_context():
            run = ModelScanRun.get(run_id)
            if run is None:
                raise ValueError("스캔 이력을 찾을 수 없습니다.")
            if run.status not in ("completed", "completed_with_warnings"):
                raise RuntimeError("완료된 스캔 결과만 일괄 계획에 사용할 수 있습니다.")
            self._assert_settings_snapshot(run)

            conflicts = self._cross_group_path_conflicts(run.id)
            backend = _delete_backend()
            pairs, excluded_groups, eligible_group_count = self._plan_groups(
                run.id, conflicts, backend
            )
            if not pairs:
                return self._persist_exclusion_review(
                    run, backend, excluded_groups
                )

            binding = self._batch_binding()
            previews: List[Dict[str, Any]] = []
            if backend in ("quarantine", "direct"):
                if binding["post_delete_scan_mode"] not in ("binary", "web"):
                    raise RuntimeError("파일 처리는 Binary 또는 Web 부분 스캔이 필수입니다.")
                executable_pairs: List[
                    Tuple[
                        ModelDuplicateGroup,
                        ModelMediaCandidate,
                        ModelMediaCandidate,
                    ]
                ] = []
                executable_previews: List[Dict[str, Any]] = []
                preview_by_key, preview_errors = self._preview_pairs(pairs)
                grouped: Dict[int, List[Tuple[Any, Any, Any]]] = {}
                for group, keep, target in pairs:
                    grouped.setdefault(int(group.id), []).append(
                        (group, keep, target)
                    )
                for group_pairs in grouped.values():
                    local_previews: List[Dict[str, Any]] = []
                    try:
                        group_id = int(group_pairs[0][0].id)
                        if group_id in preview_errors:
                            raise RuntimeError(preview_errors[group_id])
                        for group, keep, target in group_pairs:
                            key = (int(group.id), int(target.id), int(keep.id))
                            item_preview = preview_by_key.get(key)
                            if item_preview is None:
                                raise RuntimeError(
                                    "서버가 이 항목의 파일 계획을 반환하지 않았습니다."
                                )
                            if str(item_preview.get("backend") or "") != backend:
                                raise RuntimeError(
                                    "파일 처리 방식이 계획 중 변경되었습니다."
                                )
                            if len(str(item_preview.get("plan_digest") or "")) != 64:
                                raise RuntimeError(
                                    "영상·자막 계획 digest가 올바르지 않습니다."
                                )
                            if backend == "direct" and (
                                item_preview.get("subtitle_cleanup") or {}
                            ).get("executable") is False:
                                raise RuntimeError(
                                    "보호할 수 없는 관련 자막이 있습니다."
                                )
                            local_previews.append(item_preview)
                        self._assert_source_sets_disjoint(
                            local_previews,
                            [group_pairs[0][0].id] * len(local_previews),
                        )
                    except Exception as exc:
                        excluded_groups.append(
                            self._excluded_group(
                                group_pairs[0][0],
                                "filesystem_plan_blocked",
                                "영상·자막 안전 계획을 만들 수 없어 제외했습니다: %s"
                                % (str(exc) or exc.__class__.__name__),
                            )
                        )
                        continue
                    executable_pairs.extend(group_pairs)
                    executable_previews.extend(local_previews)
                pairs = executable_pairs
                previews = executable_previews
                eligible_group_count = len({int(group.id) for group, _keep, _target in pairs})
                if not pairs:
                    return self._persist_exclusion_review(
                        run, backend, excluded_groups
                    )
                try:
                    conflict_group_ids = self._source_conflict_group_ids(
                        previews,
                        [group.id for group, _keep, _target in pairs],
                    )
                except Exception as exc:
                    # If source identity itself cannot be classified, no item
                    # from this filesystem plan is allowed to execute.  Keep a
                    # durable structured review instead of returning an opaque
                    # request error with no exclusions.
                    conflict_group_ids = {
                        int(group.id) for group, _keep, _target in pairs
                    }
                    conflict_message = (
                        "영상·자막 공유 경로의 귀속을 확인할 수 없어 전체 파일 계획을 "
                        "제외했습니다: %s"
                        % (str(exc) or exc.__class__.__name__)
                    )
                else:
                    conflict_message = (
                        "다른 자동 정리 그룹과 영상·자막 또는 보호 대상 경로를 "
                        "공유하여 제외했습니다."
                    )
                if conflict_group_ids:
                    groups_by_id = {
                        int(group.id): group for group, _keep, _target in pairs
                    }
                    for group_id in sorted(conflict_group_ids):
                        group = groups_by_id.get(int(group_id))
                        if group is not None:
                            excluded_groups.append(
                                self._excluded_group(
                                    group,
                                    "cross_group_path_conflict",
                                    conflict_message,
                                )
                            )
                    kept = [
                        (pair, preview)
                        for pair, preview in zip(pairs, previews)
                        if int(pair[0].id) not in conflict_group_ids
                    ]
                    pairs = [value[0] for value in kept]
                    previews = [value[1] for value in kept]
                    eligible_group_count = len(
                        {int(group.id) for group, _keep, _target in pairs}
                    )
                    if not pairs:
                        return self._persist_exclusion_review(
                            run, backend, excluded_groups
                        )
                if self._batch_binding() != binding:
                    raise RuntimeError(
                        "파일 처리 또는 부분 스캔 설정이 계획 중 변경되었습니다."
                    )

            nonce = secrets.token_urlsafe(32)
            now = datetime.now()
            excluded_groups = self._normalized_exclusions(excluded_groups)
            expires = now + timedelta(seconds=_PREVIEW_SECONDS)
            batch = ModelBatchRun(
                scan_run_id=run.id,
                created_at=now,
                expires_at=expires,
                status="preview",
                nonce_hash=_nonce_hash(nonce),
                total_items=len(pairs),
                current_message="자동 정리 실행 준비 중",
            )
            try:
                # Serialize the final preview materialization with scan-result
                # cleanup. Network-backed fresh previews above can take long
                # enough for the parent run to become ineligible meanwhile.
                locked_run = (
                    F.db.session.query(ModelScanRun)
                    .filter(
                        ModelScanRun.id == int(run.id),
                        ModelScanRun.status.in_(
                            ("completed", "completed_with_warnings")
                        ),
                    )
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if locked_run is None or locked_run.status not in (
                    "completed",
                    "completed_with_warnings",
                ):
                    raise RuntimeError(
                        "원본 스캔 상태가 변경되어 일괄 사전확인을 저장하지 않습니다."
                    )
                self._assert_settings_snapshot(locked_run)
                F.db.session.add(batch)
                F.db.session.flush()
                self._add_exclusion_rows(
                    batch.id, run.id, excluded_groups, now
                )
                if backend in ("quarantine", "direct"):
                    aggregate_payload = [
                        {
                            "group_id": group.id,
                            "candidate_id": target.id,
                            "keep_candidate_id": keep.id,
                            "plan_digest": preview["plan_digest"],
                            "eligible": int(
                                (preview.get("subtitle_cleanup") or {})
                                .get("counts", {})
                                .get("eligible", 0)
                            ),
                        }
                        for (group, keep, target), preview in zip(pairs, previews)
                    ]
                    aggregate = hashlib.sha256(_json(aggregate_payload).encode("utf-8")).hexdigest()
                    total_subtitles = sum(item["eligible"] for item in aggregate_payload)
                    if backend == "quarantine":
                        batch.confirmation = (
                            "BATCH QUARANTINE %s ITEMS %s SUBTITLES %s %s"
                            % (batch.id, len(pairs), total_subtitles, aggregate[:12])
                        )
                    else:
                        batch.confirmation = (
                            "BATCH DELETE MEDIA %s ITEMS %s SUBTITLES %s %s"
                            % (batch.id, len(pairs), total_subtitles, aggregate[:12])
                        )
                else:
                    batch.confirmation = "BATCH DELETE %s ITEMS %s" % (batch.id, len(pairs))
                tied_group_ids = {
                    int(group.id)
                    for group, keep, target in pairs
                    if abs(float(keep.score or 0) - float(target.score or 0)) < 0.0001
                }
                for index, (group, keep, target) in enumerate(pairs):
                    F.db.session.add(
                        ModelBatchItem(
                            batch_run_id=batch.id,
                            scan_run_id=run.id,
                            group_id=group.id,
                            keep_candidate_id=keep.id,
                            delete_candidate_id=target.id,
                            created_at=now,
                            status="planned",
                            message=(
                                "최고 점수 동률 · Plex Media ID가 가장 작은 Media #%s 유지"
                                % keep.media_id
                                if int(group.id) in tied_group_ids
                                else "승인 대기 중"
                            ),
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
                    if backend in ("quarantine", "direct"):
                        preview = previews[index]
                        cleanup = preview.get("subtitle_cleanup") or {}
                        counts = cleanup.get("counts") or {}
                        journal_values = dict(
                                created_at=now,
                                updated_at=now,
                                action_log_id=None,
                                batch_run_id=batch.id,
                                run_id=run.id,
                                group_id=group.id,
                                candidate_id=target.id,
                                keep_candidate_id=keep.id,
                                operation_key="batch-preview-%s" % secrets.token_hex(20),
                                status="batch_preview",
                                plan_digest=str(preview["plan_digest"]),
                                manifest_json=_json(
                                    self._preview_manifest(cleanup, binding)
                                ),
                                eligible_count=int(counts.get("eligible", 0)),
                                excluded_count=int(counts.get("excluded", 0)),
                                protected_count=int(counts.get("protected", 0)),
                        )
                        if backend == "quarantine":
                            F.db.session.add(
                                ModelQuarantineJournal(
                                    moved_json="[]",
                                    backups_json="[]",
                                    operation_path="",
                                    quarantined_count=0,
                                    **journal_values
                                )
                            )
                        else:
                            F.db.session.add(
                                ModelDirectDeleteJournal(
                                    unlink_json="[]",
                                    operation_paths_json="[]",
                                    deleted_count=0,
                                    **journal_values
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
                    "eligible_groups": eligible_group_count,
                    "planned_deletions": len(pairs),
                    "excluded_groups": excluded_groups,
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
        if not items or len(items) != int(batch.total_items or 0):
            raise RuntimeError("삭제 가능 수가 계획 이후 변경되었습니다. 다시 사전확인하세요.")
        planned_backend = self._planned_backend(batch)
        if not str(getattr(batch, "confirmation", "") or ""):
            # Compatibility for an already-persisted preview created while a
            # rolling plugin reload was replacing the batch serializer.  The
            # new-table journal is authoritative and forbids Plex fallback.
            if any(
                ModelQuarantineJournal.for_batch_candidate(
                    batch.id, item.delete_candidate_id, status="batch_preview"
                )
                is not None
                for item in items
            ):
                planned_backend = "quarantine"
            elif any(
                ModelDirectDeleteJournal.for_batch_candidate(
                    batch.id, item.delete_candidate_id, status="batch_preview"
                )
                is not None
                for item in items
            ):
                planned_backend = "direct"
        expected_pairs, _excluded, _eligible_groups = self._plan_groups(
            run.id, conflicts, planned_backend
        )
        expected = {
            (
                int(group.id),
                int(keep.id),
                int(target.id),
                str(keep.media_id),
                str(target.media_id),
            )
            for group, keep, target in expected_pairs
        }
        actual = {
            (
                int(item.group_id),
                int(item.keep_candidate_id),
                int(item.delete_candidate_id),
                str(item.keep_media_id),
                str(item.delete_media_id),
            )
            for item in items
        }
        if not actual.issubset(expected) or len(actual) != len(items):
            raise RuntimeError(
                "자동 정리 대상 전체가 계획 이후 변경되었습니다. 다시 계획을 만드세요."
            )
        expected_by_group: Dict[int, set] = {}
        actual_by_group: Dict[int, set] = {}
        for value in expected:
            expected_by_group.setdefault(value[0], set()).add(value)
        for value in actual:
            actual_by_group.setdefault(value[0], set()).add(value)
        if any(
            actual_by_group[group_id] != expected_by_group.get(group_id, set())
            for group_id in actual_by_group
        ):
            raise RuntimeError(
                "한 그룹의 자동 정리 대상 일부가 계획에서 누락되었습니다. 다시 계획을 만드세요."
            )
        if planned_backend in ("quarantine", "direct") and _delete_backend() != planned_backend:
            raise RuntimeError(
                "사전확인 이후 파일 처리 방식이 변경되었습니다. Plex 삭제로 전환하지 않습니다."
            )
        fresh_by_key: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
        fresh_errors: Dict[int, str] = {}
        fresh_conflict_group_ids: set = set()
        if planned_backend in ("quarantine", "direct"):
            fresh_by_key, fresh_errors = self._preview_pairs(expected_pairs)
            fresh_preview_values: List[Dict[str, Any]] = []
            fresh_preview_group_ids: List[int] = []
            for group, keep, target in expected_pairs:
                key = (int(group.id), int(target.id), int(keep.id))
                preview = fresh_by_key.get(key)
                if preview is not None:
                    fresh_preview_values.append(preview)
                    fresh_preview_group_ids.append(int(group.id))
            fresh_conflict_group_ids = self._source_conflict_group_ids(
                fresh_preview_values, fresh_preview_group_ids
            )
            if fresh_conflict_group_ids.intersection(actual_by_group):
                raise RuntimeError(
                    "승인 직전 영상·자막 공유 경로가 계획된 그룹과 충돌합니다. "
                    "다시 계획을 만드세요."
                )
        missing_group_ids = set(expected_by_group) - set(actual_by_group)
        if missing_group_ids:
            if planned_backend not in ("quarantine", "direct"):
                raise RuntimeError(
                    "자동 정리 가능한 그룹이 계획에서 누락되었습니다. 다시 계획을 만드세요."
                )
            expected_objects: Dict[int, List[Tuple[Any, Any, Any]]] = {}
            for group, keep, target in expected_pairs:
                expected_objects.setdefault(int(group.id), []).append(
                    (group, keep, target)
                )
            for group_id in missing_group_ids:
                still_blocked = (
                    group_id in fresh_errors
                    or group_id in fresh_conflict_group_ids
                )
                if not still_blocked:
                    for group, keep, target in expected_objects[group_id]:
                        omitted_preview = fresh_by_key.get(
                            (int(group.id), int(target.id), int(keep.id))
                        )
                        if omitted_preview is None or (
                            planned_backend == "direct"
                            and (omitted_preview.get("subtitle_cleanup") or {}).get(
                                "executable"
                            )
                            is False
                        ):
                            still_blocked = True
                            break
                if not still_blocked:
                    raise RuntimeError(
                        "이전에 제외된 그룹이 이제 자동 정리 가능해졌습니다. 다시 계획을 만드세요."
                    )
        if planned_backend == "quarantine":
            if _delete_backend() != "quarantine":
                raise RuntimeError(
                    "사전확인 이후 파일 처리 방식이 변경되었습니다. Plex 삭제로 전환하지 않습니다."
                )
            fresh_previews: List[Dict[str, Any]] = []
            for item in items:
                journal = self._preview_journal(batch.id, item.delete_candidate_id)
                key = (
                    int(item.group_id),
                    int(item.delete_candidate_id),
                    int(item.keep_candidate_id),
                )
                if int(item.group_id) in fresh_errors or key not in fresh_by_key:
                    raise RuntimeError(
                        "격리할 영상·자막 계획이 승인 직전 변경되었습니다: %s"
                        % fresh_errors.get(int(item.group_id), "계획 없음")
                    )
                fresh_previews.append(
                    self._validate_quarantine_preview(
                        item, journal, fresh_by_key[key]
                    )
                )
            self._assert_source_sets_disjoint(
                fresh_previews, [item.group_id for item in items]
            )
        elif planned_backend == "legacy_direct":
            raise RuntimeError(
                "이 사전확인은 이전 파일 직접 삭제 방식으로 생성되었습니다. "
                "Plex Media DELETE 방식으로 다시 사전확인하세요."
            )
        elif planned_backend == "direct":
            if _delete_backend() != "direct":
                raise RuntimeError(
                    "사전확인 이후 파일 처리 방식이 변경되었습니다. Plex 삭제로 전환하지 않습니다."
                )
            fresh_previews = []
            for item in items:
                journal = self._direct_preview_journal(
                    batch.id, item.delete_candidate_id
                )
                key = (
                    int(item.group_id),
                    int(item.delete_candidate_id),
                    int(item.keep_candidate_id),
                )
                if int(item.group_id) in fresh_errors or key not in fresh_by_key:
                    raise RuntimeError(
                        "직접 삭제할 영상·자막 계획이 승인 직전 변경되었습니다: %s"
                        % fresh_errors.get(int(item.group_id), "계획 없음")
                    )
                fresh_previews.append(
                    self._validate_direct_preview(item, journal, fresh_by_key[key])
                )
            self._assert_source_sets_disjoint(
                fresh_previews, [item.group_id for item in items]
            )
        elif _delete_backend() != planned_backend:
            raise RuntimeError(
                "사전확인 이후 파일 처리 방식이 변경되었습니다. 다시 사전확인하세요."
            )
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
    def _advance_group_after_success(
        batch_id: int, item: ModelBatchItem
    ) -> None:
        """Make the next target in one group eligible without a rescan gap."""

        candidate = ModelMediaCandidate.get(item.delete_candidate_id)
        group = ModelDuplicateGroup.get(item.group_id)
        run = ModelScanRun.get(item.scan_run_id)
        if candidate is None or group is None or run is None:
            raise RuntimeError("자동 정리 완료 상태를 DB에 반영할 수 없습니다.")
        if not candidate.deleted:
            candidate.deleted = True
            candidate.deleted_at = datetime.now()
        remaining = any(
            other.id != item.id
            and other.group_id == item.group_id
            and other.status == "planned"
            for other in ModelBatchItem.by_batch(batch_id)
        )
        group.safe_to_delete = bool(remaining)
        group.resolution_status = "open" if remaining else "rescan_required"
        group.safety_flags_json = _json(
            ["batch_auto_delete_in_progress"]
            if remaining
            else ["rescan_required_after_delete"]
        )

    @staticmethod
    def _close_partially_processed_groups(batch_id: int) -> None:
        """Do not leave a cancelled/stopped auto group open after mutation."""

        items = ModelBatchItem.by_batch(batch_id)
        touched = {
            int(item.group_id)
            for item in items
            if item.status
            in (
                "success",
                "scan_pending",
            )
        }
        for group_id in touched:
            group = ModelDuplicateGroup.get(group_id)
            if group is not None and group.resolution_status == "open":
                group.safe_to_delete = False
                group.resolution_status = "rescan_required"
                group.safety_flags_json = _json(["rescan_required_after_delete"])

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
        self._close_partially_processed_groups(batch_id)
        F.db.session.commit()
        try:
            if release_deletion_lease and deletion_lease_token:
                self.lease_service.release(deletion_lease_token)
        finally:
            self._wake_post_delete_scans()

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

                    _expire_session()
                    queued_item = ModelBatchItem.get(item_id)
                    if queued_item is None or queued_item.status != "planned":
                        continue

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
                        backend = self._planned_backend(batch)
                        if backend == "legacy_direct":
                            raise RuntimeError(
                                "이전 직접 삭제 계획은 실행할 수 없습니다. 다시 사전확인하세요."
                            )
                        if _delete_backend() != backend:
                            raise RuntimeError(
                                "사전확인 이후 파일 처리 방식이 변경되어 실행을 차단했습니다."
                            )
                        plan_digest = ""
                        confirmation = "DELETE %s" % item.delete_media_id
                        if backend == "quarantine":
                            preview_journal = self._preview_journal(
                                batch.id, item.delete_candidate_id
                            )
                            fresh_preview = self._fresh_quarantine_preview(
                                item, preview_journal
                            )
                            plan_digest = str(preview_journal.plan_digest)
                            confirmation = str(fresh_preview.get("confirmation") or "")
                        elif backend == "direct":
                            preview_journal = self._direct_preview_journal(
                                batch.id, item.delete_candidate_id
                            )
                            fresh_preview = self._rebind_direct_preview(
                                item, preview_journal
                            )
                            plan_digest = str(preview_journal.plan_digest)
                            confirmation = str(fresh_preview.get("confirmation") or "")
                        result = self.delete_service.delete(
                            group_id=item.group_id,
                            candidate_id=item.delete_candidate_id,
                            keep_candidate_id=item.keep_candidate_id,
                            confirmation=confirmation,
                            plan_digest=plan_digest,
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
                        for remaining in ModelBatchItem.by_batch(batch_id):
                            if (
                                remaining.group_id == item.group_id
                                and remaining.status == "planned"
                            ):
                                remaining.status = "skipped"
                                remaining.message = (
                                    "같은 그룹의 이전 항목 실패로 자동 정리하지 않음"
                                )
                                remaining.finished_at = datetime.now()
                        F.db.session.commit()
                        continue

                    _expire_session()
                    item = ModelBatchItem.get(item_id)
                    batch = ModelBatchRun.get(batch_id)
                    if item is None or batch is None:
                        raise RuntimeError("삭제 후 작업 정보를 다시 읽을 수 없습니다.")
                    pending_scan = result.get("verification") in (
                        "quarantined_pending_scan",
                        "deleted_pending_scan",
                    )
                    item.status = "scan_pending" if pending_scan else "success"
                    item.message = (
                        "파일 처리 완료 · Plex 부분 스캔 검증 대기"
                        if pending_scan
                        else "삭제 후 Plex 재검증 완료"
                    )
                    item.action_log_id = result.get("action_id")
                    item.finished_at = None if pending_scan else datetime.now()
                    self._advance_group_after_success(batch_id, item)
                    items = ModelBatchItem.by_batch(batch_id)
                    self._refresh_counts(batch, items)
                    batch.current_message = "Group #%s 완료" % item.group_id
                    F.db.session.commit()

                items = ModelBatchItem.by_batch(batch_id)
                if any(item.status == "scan_pending" for item in items):
                    self._finish_batch(
                        batch_id,
                        "scan_pending",
                        "파일 처리 완료 · Plex 부분 스캔 검증 대기",
                    )
                else:
                    failed = any(
                        item.status in _FAILURE_ITEM_STATUSES for item in items
                    )
                    skipped = any(
                        item.status in _SKIPPED_ITEM_STATUSES for item in items
                    )
                    self._finish_batch(
                        batch_id,
                        "completed_with_errors"
                        if failed
                        else ("completed_with_warnings" if skipped else "completed"),
                        "자동 정리 완료 · 일부 항목은 확인 필요"
                        if failed or skipped
                        else "자동 정리 완료",
                    )
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
        payload["backend"] = self._planned_backend(batch)
        payload["items"] = [item.as_api() for item in ModelBatchItem.by_batch(batch.id)]
        payload["excluded_groups"] = [
            item.as_api() for item in ModelBatchExclusion.by_batch(batch.id)
        ]
        run = ModelScanRun.get(batch.scan_run_id)
        if run is not None:
            payload["delete_budget"] = delete_attempt_budget(run)
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
                self._wake_post_delete_scans()
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
                # A process can stop after the filesystem transaction's final
                # journal commit but before enqueue_confirmed persists its scan
                # outbox row.  The ActionLog is then intentionally no longer
                # "interrupted" and no batch need exist, so both filesystem
                # journals must participate in the recovery fast-path gate.
                if (
                    not ModelBatchRun.unfinished()
                    and not ModelActionLog.interrupted()
                    and not ModelQuarantineJournal.unfinished()
                    and not ModelDirectDeleteJournal.unfinished()
                ):
                    return 0
        recovery_claim = self.lease_service.acquire_for_recovery()
        if recovery_claim is None:
            # A valid manual or batch owner belongs to another live web worker.
            return 0
        try:
            recovery_owner_ref = (
                "%s:%s"
                % (
                    getattr(recovery_claim, "previous_kind", ""),
                    getattr(recovery_claim, "previous_ref", ""),
                )
                if getattr(recovery_claim, "previous_kind", "")
                or getattr(recovery_claim, "previous_ref", "")
                else "plugin_load"
            )
            self.last_delete_recovery_counts = self.delete_service.recover_interrupted(
                recovery_lease_token=recovery_claim.token,
                recovery_lease_owner_ref=recovery_owner_ref,
            )
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
                                "quarantined_pending_scan",
                                "deleted_pending_scan",
                                "scan_running",
                            ):
                                item.status = (
                                    "scan_pending"
                                    if log.status
                                    in (
                                        "quarantined_pending_scan",
                                        "deleted_pending_scan",
                                        "scan_running",
                                    )
                                    else log.status
                                )
                                item.action_log_id = log.id
                                item.message = log.message or "삭제 감사 이력에서 복구됨"
                                item.finished_at = (
                                    None if item.status == "scan_pending" else now
                                )
                            else:
                                item.status = "unknown"
                                item.message = "FlaskFarm 재시작으로 삭제 결과를 확정할 수 없음"
                                item.finished_at = now
                    scan_pending = any(
                        item.status == "scan_pending"
                        for item in ModelBatchItem.by_batch(batch.id)
                    )
                    batch.status = "scan_pending" if scan_pending else "interrupted"
                    batch.finished_at = None if scan_pending else now
                    batch.current_message = (
                        "파일 처리 완료 · Plex 부분 스캔 검증 대기"
                        if scan_pending
                        else "재시작으로 보수적 중단 · 자동 재개하지 않음"
                    )
                    batch.nonce_hash = ""
                    batch.lease_key = None
                    batch.deletion_lease_token = ""
                    self._refresh_counts(batch, ModelBatchItem.by_batch(batch.id))
                F.db.session.commit()
                return len(batches)
        finally:
            try:
                self.lease_service.release(recovery_claim.token)
            finally:
                self._wake_post_delete_scans()

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
