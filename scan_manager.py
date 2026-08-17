from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from framework import F

from .models import ModelDuplicateGroup, ModelMediaCandidate, ModelScanRun
from .services.domain import SafetyResult
from .services.plex_gateway import PlexGateway
from .services.plex_mate_provider import PlexMateProvider, redact_secret
from .services.safety import SafetyPolicy, assess_group
from .services.score_engine import (
    DEFAULT_AUDIO_CODEC_SCORES,
    DEFAULT_RESOLUTION_SCORES,
    DEFAULT_VIDEO_CODEC_SCORES,
    ScoreConfig,
    ScoreEngine,
    parse_filename_rules,
    parse_score_map,
)
from .setup import P


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _setting_bool(key: str, default: bool = False) -> bool:
    value = P.ModelSetting.get(key)
    if value is None:
        return default
    return value == "True"


def _setting_int(key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(P.ModelSetting.get(key) or default)))
    except (TypeError, ValueError):
        return default


def _setting_float(key: str, default: float) -> float:
    try:
        return float(P.ModelSetting.get(key) or default)
    except (TypeError, ValueError):
        return default


def current_score_config() -> ScoreConfig:
    return ScoreConfig(
        video_codec_scores=parse_score_map(
            P.ModelSetting.get("setting_video_codec_scores") or "", DEFAULT_VIDEO_CODEC_SCORES
        ),
        audio_codec_scores=parse_score_map(
            P.ModelSetting.get("setting_audio_codec_scores") or "", DEFAULT_AUDIO_CODEC_SCORES
        ),
        resolution_scores=parse_score_map(
            P.ModelSetting.get("setting_resolution_scores") or "", DEFAULT_RESOLUTION_SCORES
        ),
        filename_rules=parse_filename_rules(P.ModelSetting.get("setting_filename_rules") or ""),
        bitrate_weight=_setting_float("setting_bitrate_weight", 2.0),
        duration_weight=_setting_float("setting_duration_weight", 1.0 / 300.0),
        dimension_weight=_setting_float("setting_dimension_weight", 2.0),
        audio_channel_weight=_setting_float("setting_audio_channel_weight", 1000.0),
        use_filesize=_setting_bool("setting_use_filesize", False),
        filesize_weight=_setting_float("setting_filesize_weight", 1.0 / 100000.0),
    )


def current_safety_policy() -> SafetyPolicy:
    roots = tuple(
        line.strip()
        for line in (P.ModelSetting.get("setting_allowed_roots") or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    return SafetyPolicy(
        allowed_roots=roots,
        require_guid=_setting_bool("setting_require_guid", True),
        block_multipart=_setting_bool("setting_block_multipart", True),
        require_allowed_roots=True,
    )


def _config_snapshot(score: ScoreConfig, policy: SafetyPolicy) -> Dict[str, Any]:
    return {
        "score": {
            "video_codec_scores": score.video_codec_scores,
            "audio_codec_scores": score.audio_codec_scores,
            "resolution_scores": score.resolution_scores,
            "filename_rules": list(score.filename_rules),
            "bitrate_weight": score.bitrate_weight,
            "duration_weight": score.duration_weight,
            "dimension_weight": score.dimension_weight,
            "audio_channel_weight": score.audio_channel_weight,
            "use_filesize": score.use_filesize,
            "filesize_weight": score.filesize_weight,
        },
        "safety": {
            "allowed_roots": list(policy.allowed_roots),
            "require_guid": policy.require_guid,
            "block_multipart": policy.block_multipart,
        },
    }


class ScanManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def recover_interrupted(self) -> int:
        with F.app.app_context():
            count = (
                F.db.session.query(ModelScanRun)
                .filter(ModelScanRun.status.in_(["queued", "running", "cancelling"]))
                .update(
                    {
                        "status": "interrupted",
                        "finished_at": datetime.now(),
                        "status_message": "FlaskFarm 재시작으로 중단됨",
                    },
                    synchronize_session=False,
                )
            )
            F.db.session.commit()
            return count

    def start(self, section_ids: Sequence[str]) -> ModelScanRun:
        clean_ids = list(dict.fromkeys(str(value).strip() for value in section_ids if str(value).strip()))
        if not clean_ids or any(not value.isdigit() for value in clean_ids):
            raise ValueError("하나 이상의 올바른 라이브러리 ID를 선택해야 합니다.")
        if len(clean_ids) > 100:
            raise ValueError("한 번에 선택할 수 있는 라이브러리는 100개 이하입니다.")

        with self._lock, F.app.app_context():
            active = ModelScanRun.active()
            if active is not None:
                raise RuntimeError("이미 실행 중인 스캔이 있습니다. (ID %s)" % active.id)

            score = current_score_config()
            policy = current_safety_policy()
            run = ModelScanRun(
                status="queued",
                progress=0,
                status_message="스캔 대기 중",
                section_ids_json=_json(clean_ids),
                settings_snapshot_json=_json(_config_snapshot(score, policy)),
                total_sections=len(clean_ids),
            )
            F.db.session.add(run)
            F.db.session.commit()
            run_id = run.id

            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._worker,
                args=(run_id, clean_ids, score, policy),
                name="plex-dupefinder-scan-%s" % run_id,
                daemon=True,
            )
            self._thread.start()
            return run

    def cancel(self, run_id: int) -> ModelScanRun:
        with F.app.app_context():
            run = ModelScanRun.get(run_id)
            if run is None:
                raise ValueError("스캔을 찾을 수 없습니다.")
            if run.status not in ("queued", "running", "cancelling"):
                return run
            run.cancellation_requested = True
            run.status = "cancelling"
            run.status_message = "취소 요청을 처리 중"
            F.db.session.commit()
        self._cancel.set()
        return run

    def delete_run(self, run_id: int) -> Dict[str, Any]:
        """Delete one terminal scan result without deleting audit evidence."""

        # This is a synchronous authenticated web operation.  Reuse the
        # request's app context/session so a nested Flask context cannot leave
        # the caller's identity map holding the pre-tombstone scan snapshot.
        with self._lock:
            return ModelScanRun.delete_results(run_id)

    def unload(self) -> None:
        self._cancel.set()

    def _is_cancelled(self, run_id: int) -> bool:
        if self._cancel.is_set():
            return True
        with F.app.app_context():
            run = ModelScanRun.get(run_id)
            return bool(run is None or run.cancellation_requested)

    def _set_run(self, run_id: int, **values: Any) -> None:
        with F.app.app_context():
            run = ModelScanRun.get(run_id)
            if run is None:
                return
            for key, value in values.items():
                setattr(run, key, value)
            F.db.session.commit()

    def _persist_group(
        self,
        run_id: int,
        section_key: str,
        section_title: str,
        item: Any,
        score_engine: ScoreEngine,
        policy: SafetyPolicy,
        machine_id_present: bool,
    ) -> None:
        safety = assess_group(item, policy)
        if not machine_id_present:
            flags = tuple(dict.fromkeys(list(safety.flags) + ["missing_machine_id"]))
            safety = SafetyResult(False, flags, dict(safety.details))

        recommended_media_id = score_engine.recommended_media_id(item.media)
        scored = [(version, score_engine.score(version)) for version in item.media]

        with F.app.app_context():
            group = ModelDuplicateGroup(
                run_id=run_id,
                section_key=section_key,
                section_title=section_title,
                rating_key=item.rating_key,
                guid=item.guid,
                media_type=item.media_type,
                title=item.title,
                year=item.year,
                grandparent_title=item.grandparent_title,
                grandparent_rating_key=item.grandparent_rating_key,
                parent_index=item.parent_index,
                media_index=item.index,
                identity_fingerprint=item.identity_fingerprint(),
                candidate_count=len(item.media),
                safe_to_delete=safety.safe,
                safety_flags_json=_json(list(safety.flags)),
                safety_details_json=_json(safety.details),
                resolution_status="open",
            )
            F.db.session.add(group)
            F.db.session.flush()

            recommended_candidate_id = None
            for version, score_result in scored:
                candidate = ModelMediaCandidate(
                    group_id=group.id,
                    media_id=version.media_id,
                    duration=version.duration,
                    bitrate=version.bitrate,
                    width=version.width,
                    height=version.height,
                    video_resolution=version.video_resolution,
                    video_codec=version.video_codec,
                    audio_codec=version.audio_codec,
                    audio_channels=max(
                        [version.audio_channels] + [track.channels for track in version.audio_tracks],
                        default=0,
                    ),
                    container=version.container,
                    total_size=version.total_size,
                    parts_json=_json([part.as_dict() for part in version.parts]),
                    audio_tracks_json=_json([track.as_dict() for track in version.audio_tracks]),
                    fingerprint=version.fingerprint(),
                    score=score_result.total,
                    score_breakdown_json=_json(score_result.breakdown),
                )
                F.db.session.add(candidate)
                F.db.session.flush()
                if version.media_id == recommended_media_id:
                    recommended_candidate_id = candidate.id

            group.recommended_candidate_id = recommended_candidate_id
            run = ModelScanRun.get(run_id)
            run.total_groups = (run.total_groups or 0) + 1
            if safety.safe:
                run.safe_groups = (run.safe_groups or 0) + 1
            else:
                run.unsafe_groups = (run.unsafe_groups or 0) + 1
            F.db.session.commit()

    def _worker(
        self,
        run_id: int,
        section_ids: Sequence[str],
        score: ScoreConfig,
        policy: SafetyPolicy,
    ) -> None:
        connection = None
        warnings: List[str] = []
        try:
            self._set_run(
                run_id,
                status="running",
                started_at=datetime.now(),
                status_message="Plex 연결 확인 중",
            )
            connection = PlexMateProvider().resolve(require_machine_id=False)
            timeout = _setting_int("setting_request_timeout", 20, 5, 120)
            gateway = PlexGateway(connection, timeout=(5, timeout))
            identity = gateway.validate_identity(connection.machine_id, require_match=False)
            if connection.machine_id and identity.machine_id != connection.machine_id:
                raise RuntimeError("Plex Machine ID가 plex_mate 설정과 일치하지 않습니다.")

            self._set_run(
                run_id,
                server_machine_id=identity.machine_id,
                server_version=identity.version,
                status_message="라이브러리 확인 중",
            )
            section_map = {section.key: section for section in gateway.list_sections()}
            missing = [section_id for section_id in section_ids if section_id not in section_map]
            if missing:
                raise RuntimeError("Plex에서 찾을 수 없는 라이브러리 ID: %s" % ", ".join(missing))

            score_engine = ScoreEngine(score)
            total_sections = len(section_ids)
            for section_index, section_id in enumerate(section_ids):
                if self._is_cancelled(run_id):
                    break
                section = section_map[section_id]
                self._set_run(run_id, status_message="%s 중복 검색 중" % section.title)
                rating_keys = gateway.duplicate_rating_keys(
                    section, cancel_check=lambda: self._is_cancelled(run_id)
                )
                item_count = max(1, len(rating_keys))

                for item_index, rating_key in enumerate(rating_keys):
                    if self._is_cancelled(run_id):
                        break
                    try:
                        item = gateway.get_metadata(rating_key)
                        self._persist_group(
                            run_id,
                            section.key,
                            section.title,
                            item,
                            score_engine,
                            policy,
                            bool(connection.machine_id),
                        )
                    except Exception as exc:
                        with F.app.app_context():
                            F.db.session.rollback()
                        warnings.append("ratingKey %s: %s" % (rating_key, exc.__class__.__name__))
                        P.logger.warning(
                            "Duplicate metadata skipped: section=%s ratingKey=%s error=%s",
                            section.key,
                            rating_key,
                            exc.__class__.__name__,
                        )
                    progress = int(
                        ((section_index + float(item_index + 1) / item_count) / total_sections) * 100
                    )
                    self._set_run(run_id, progress=min(progress, 99))

                self._set_run(
                    run_id,
                    completed_sections=section_index + 1,
                    progress=int(((section_index + 1) / total_sections) * 100),
                )

            if self._is_cancelled(run_id):
                self._set_run(
                    run_id,
                    status="cancelled",
                    finished_at=datetime.now(),
                    status_message="사용자 요청으로 취소됨",
                )
            else:
                status = "completed_with_warnings" if warnings else "completed"
                self._set_run(
                    run_id,
                    status=status,
                    finished_at=datetime.now(),
                    progress=100,
                    status_message="스캔 완료" if not warnings else "일부 항목을 건너뛰고 완료",
                    error_summary="\n".join(warnings[:100]),
                )
        except Exception as exc:
            secret = connection.token if connection is not None else ""
            safe_message = redact_secret(str(exc), secret)
            P.logger.error("Dupe scan failed: %s", exc.__class__.__name__)
            P.logger.debug(redact_secret(traceback.format_exc(), secret))
            self._set_run(
                run_id,
                status="failed",
                finished_at=datetime.now(),
                status_message="스캔 실패",
                error_summary=safe_message[:4000],
            )
        finally:
            try:
                with F.app.app_context():
                    F.db.session.remove()
            except Exception:
                pass
            with self._lock:
                self._thread = None
                self._cancel.clear()
