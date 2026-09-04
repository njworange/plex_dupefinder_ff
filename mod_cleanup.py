from __future__ import annotations

import copy
import importlib
import json
import math
import os
import threading
import traceback
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from flask import jsonify, render_template
from framework import F
from plugin import PluginModuleBase

from .mod_setting import runtime_config, sync_scheduler_settings
from .models import ModelCleanupAction, ModelCleanupRun
from .setup import P


name = "cleanup"
RUN_MODES = frozenset(("dry_run", "live"))


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _candidate_id(candidate: Any) -> str:
    value = _value(candidate, "media_id", _value(candidate, "id", ""))
    return str(value or "")


def _candidate_paths(candidate: Any) -> Tuple[str, ...]:
    direct = _value(candidate, "paths", None)
    if direct is not None:
        return tuple(str(path) for path in direct if path)
    result: List[str] = []
    for part in _value(candidate, "parts", ()) or ():
        path = _value(part, "path", _value(part, "file", ""))
        if path:
            result.append(str(path))
    return tuple(result)


def _candidate_size(candidate: Any) -> int:
    value = _value(candidate, "total_size", None)
    if value is not None:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            pass
    total = 0
    for part in _value(candidate, "parts", ()) or ():
        try:
            total += max(0, int(_value(part, "size", 0) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _canonical_path(path: Any) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _canonical_paths(candidate: Any) -> frozenset[str]:
    return frozenset(_canonical_path(path) for path in _candidate_paths(candidate))


def _candidate_map(group: Any) -> Dict[str, frozenset[str]]:
    return {
        _candidate_id(candidate): _canonical_paths(candidate)
        for candidate in (_value(group, "candidates", _value(group, "media", ())) or ())
        if _candidate_id(candidate)
    }


def _candidate_objects(group: Any) -> Dict[str, Any]:
    return {
        _candidate_id(candidate): candidate
        for candidate in (_value(group, "candidates", _value(group, "media", ())) or ())
        if _candidate_id(candidate)
    }


def _has_shared_video_path(path_map: Mapping[str, frozenset[str]]) -> bool:
    seen: set[str] = set()
    for paths in path_map.values():
        if seen.intersection(paths):
            return True
        seen.update(paths)
    return False


def _candidate_snapshot(candidate: Any) -> Dict[str, Any]:
    return {
        "media_id": _candidate_id(candidate),
        "paths": list(_candidate_paths(candidate)),
        "size": _candidate_size(candidate),
        "duration": int(_value(candidate, "duration", 0) or 0),
        "bitrate": int(_value(candidate, "bitrate", 0) or 0),
        "width": int(_value(candidate, "width", 0) or 0),
        "height": int(_value(candidate, "height", 0) or 0),
        "video_resolution": str(_value(candidate, "video_resolution", "") or ""),
        "video_codec": str(_value(candidate, "video_codec", "") or ""),
        "audio_codec": str(_value(candidate, "audio_codec", "") or ""),
    }


def _response_status(response: Any) -> Optional[int]:
    value = getattr(response, "status_code", response if isinstance(response, int) else None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _score_value(decision: Any, candidate: Any) -> float:
    media_id = _candidate_id(candidate)
    ranked = _value(decision, "ranked", ()) or ()
    for item in ranked:
        ranked_candidate = _value(item, "candidate", None)
        if _candidate_id(ranked_candidate) != media_id:
            continue
        score = _value(item, "score", 0)
        return float(_value(score, "total", score) or 0)

    scores = _value(decision, "scores", {}) or {}
    if isinstance(scores, Mapping):
        score = scores.get(media_id, scores.get(candidate, 0))
        return float(_value(score, "total", score) or 0)
    for score in scores:
        if str(_value(score, "media_id", "")) == media_id:
            return float(_value(score, "total", 0) or 0)
    return float(_value(candidate, "score", 0) or 0)


def _score_config(config: Dict[str, Any]) -> Any:
    module = importlib.import_module(".services.score_engine", __package__)
    ScoreConfig = getattr(module, "ScoreConfig")
    raw = dict(config.get("score") or {})
    aliases = {
        "audio": "audio_codec_scores",
        "video": "video_codec_scores",
        "resolution": "resolution_scores",
    }
    for old, new in aliases.items():
        if old in raw and new not in raw:
            raw[new] = raw.pop(old)

    allowed = set(getattr(ScoreConfig, "__dataclass_fields__", {}))
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError("setting_score_json의 알 수 없는 키: %s" % ", ".join(unknown))

    for key in ("audio_codec_scores", "video_codec_scores", "resolution_scores"):
        if key not in raw:
            continue
        if not isinstance(raw[key], dict):
            raise ValueError("setting_score_json.%s 값은 JSON 객체여야 합니다." % key)
        converted: Dict[str, float] = {}
        for score_key, score_value in raw[key].items():
            try:
                number = float(score_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("setting_score_json.%s score는 숫자여야 합니다." % key) from exc
            if not math.isfinite(number):
                raise ValueError("setting_score_json.%s score는 유한해야 합니다." % key)
            converted[str(score_key)] = number
        raw[key] = converted

    for key in (
        "bitrate_weight",
        "duration_divisor",
        "dimensions_weight",
        "audio_channels_weight",
        "size_divisor",
    ):
        if key in raw:
            try:
                raw[key] = float(raw[key])
            except (TypeError, ValueError) as exc:
                raise ValueError("setting_score_json.%s 값은 숫자여야 합니다." % key) from exc
            if not math.isfinite(raw[key]):
                raise ValueError("setting_score_json.%s 값은 유한해야 합니다." % key)

    # ScoreEngine owns glob translation; passing regular expressions here would
    # double-translate the UI rules and make them stop matching filenames.
    filename_scores = dict(config.get("filename_scores") or {})
    if filename_scores:
        raw["filename_scores"] = filename_scores
    raw["include_size"] = bool(config.get("include_size"))
    return ScoreConfig(**raw)


def _resolve_plex_connection() -> Any:
    try:
        provider_module = importlib.import_module(
            ".services.plex_mate_provider", __package__
        )
        provider = getattr(provider_module, "PlexMateProvider")()
        return provider.resolve()
    except ModuleNotFoundError:
        pass

    plex_mate = F.PluginManager.get_plugin_instance("plex_mate")
    if plex_mate is None:
        raise RuntimeError("plex_mate 플러그인을 찾을 수 없습니다.")
    settings = getattr(plex_mate, "ModelSetting", None)
    if settings is None:
        raise RuntimeError("plex_mate 연결 설정을 읽을 수 없습니다.")
    base_url = str(settings.get("base_url") or "").strip().rstrip("/")
    token = str(settings.get("base_token") or "").strip()
    machine_id = str(settings.get("base_machine") or "").strip()
    if not base_url or not token:
        raise RuntimeError("plex_mate에 Plex URL과 Token을 설정해야 합니다.")
    domain = importlib.import_module(".services.domain", __package__)
    return getattr(domain, "PlexConnection")(base_url, token, machine_id)


def _create_gateway(connection: Any, timeout: int) -> Any:
    module = importlib.import_module(".services.plex_gateway", __package__)
    gateway_type = getattr(module, "PlexGateway")
    return gateway_type(connection, timeout=(5, timeout))


class CleanupServiceAdapter:
    """Small seam between the Flask worker and independently tested services."""

    def __init__(self, config: Dict[str, Any]) -> None:
        connection = _resolve_plex_connection()
        self.gateway = _create_gateway(connection, int(config["timeout"]))
        score_module = importlib.import_module(".services.score_engine", __package__)
        self.score_engine = getattr(score_module, "ScoreEngine")(_score_config(config))
        subtitle_module = importlib.import_module(".services.subtitle_finder", __package__)
        subtitle_dirs = (
            getattr(subtitle_module, "DEFAULT_SUBTITLE_DIRS", ("Subs", "Subtitles"))
            if bool(config.get("subs_search"))
            else ()
        )
        self.subtitle_finder = getattr(subtitle_module, "SubtitleFinder")(
            config.get("subtitle_extensions") or (),
            subtitle_dirs=subtitle_dirs,
        )

    def iter_duplicate_groups(
        self, section_id: str, cancel_check: Optional[Any] = None
    ) -> Iterable[Any]:
        method = getattr(self.gateway, "iter_duplicate_groups", None)
        if method is None:
            method = getattr(self.gateway, "duplicate_groups", None)
        if method is None:
            raise RuntimeError("PlexGateway duplicate group API를 찾을 수 없습니다.")
        return method(section_id, cancel_check=cancel_check)

    def rank(self, group: Any) -> Any:
        return self.score_engine.select_keep(group)

    def get_group(self, rating_key: str) -> Any:
        method = getattr(self.gateway, "get_metadata", None)
        if method is None:
            method = getattr(self.gateway, "metadata", None)
        if method is None:
            raise RuntimeError("PlexGateway metadata API를 찾을 수 없습니다.")
        return method(rating_key)

    def delete_media(self, rating_key: str, media_id: str) -> Any:
        return self.gateway.delete_media(rating_key, media_id)

    def media_exists(self, rating_key: str, media_id: str) -> bool:
        method = getattr(self.gateway, "media_exists", None)
        if callable(method):
            return bool(method(rating_key, media_id))
        return _candidate_id(
            _candidate_objects(self.get_group(rating_key)).get(str(media_id))
        ) == str(media_id)

    def find_sidecars(self, candidate: Any) -> Tuple[str, ...]:
        return tuple(self.subtitle_finder.find_for_candidate(candidate))

    def delete_sidecars(self, paths: Sequence[str]) -> Any:
        # The worker invokes this only after Plex metadata proves video absence.
        return self.subtitle_finder.delete(paths, dry_run=False)


def build_cleanup_adapter(config: Dict[str, Any]) -> CleanupServiceAdapter:
    return CleanupServiceAdapter(config)


def _empty_status() -> Dict[str, Any]:
    return {
        "running": False,
        "status": "idle",
        "mode": "",
        "stop_requested": False,
        "started_at": None,
        "current": {},
        "progress": {"processed": 0, "total": 0},
        "summary": {
            "groups": 0,
            "would_delete": 0,
            "would_delete_bytes": 0,
            "deleted": 0,
            "bytes": 0,
            "partial": 0,
            "errors": 0,
            "skipped": 0,
        },
        "recent_actions": [],
        "message": "",
    }


def _rollback_session() -> None:
    try:
        with F.app.app_context():
            F.db.session.rollback()
    except Exception:
        pass


class ModuleCleanup(PluginModuleBase):
    db_default = {
        "cleanup_db_version": "2",
        "cleanup_auto_start": "False",
        # FlaskFarm requires this conventional key before registering a job.
        # The public UI value remains setting_scheduler_interval.
        "cleanup_interval": "60",
    }

    def __init__(self, plugin: Any) -> None:
        super(ModuleCleanup, self).__init__(
            plugin,
            name=name,
            first_menu="run",
            scheduler_desc="Plex duplicate immediate cleanup",
        )
        self.worker_thread: Optional[threading.Thread] = None
        self.worker_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.current_run_id: Optional[int] = None
        self.adapter_factory = build_cleanup_adapter
        self._status = _empty_status()

    def process_menu(self, sub: str, req: Any) -> Any:
        arg = P.ModelSetting.to_dict()
        arg["package_name"] = P.package_name
        arg["module_name"] = self.name
        arg["sub"] = sub
        return render_template(
            "%s_%s_run.html" % (P.package_name, self.name), arg=arg
        )

    def get_scheduler_interval(self) -> str:
        return str(P.ModelSetting.get("cleanup_interval") or "60")

    def setting_save_after(self, change_list: Any) -> None:
        del change_list
        sync_scheduler_settings()

    def scheduler_function(self) -> bool:
        mode = str(P.ModelSetting.get("setting_scheduler_mode") or "off").strip().lower()
        if mode == "off":
            return False
        if mode not in RUN_MODES:
            P.logger.warning("지원하지 않는 scheduler mode: %s", mode)
            return False
        result = self._start(mode)
        return result.get("ret") == "success"

    def plugin_load(self) -> None:
        try:
            with F.app.app_context():
                actions = ModelCleanupAction.recover_interrupted()
                runs = ModelCleanupRun.recover_interrupted()
                F.db.session.commit()
                if actions or runs:
                    P.logger.warning(
                        "Interrupted cleanup recovered: runs=%s actions=%s",
                        runs,
                        actions,
                    )
        except Exception as exc:
            _rollback_session()
            P.logger.warning("Cleanup recovery failed: %s", exc.__class__.__name__)

    def plugin_unload(self) -> None:
        self.stop_event.set()
        with self.worker_lock:
            worker = self.worker_thread
        if worker and worker is not threading.current_thread():
            worker.join(timeout=10)
            if worker.is_alive():
                P.logger.warning("Cleanup worker did not stop before unload timeout")

    def process_command(
        self, command: str, arg1: str, arg2: str, arg3: str, req: Any
    ) -> Any:
        del arg1, arg2, arg3, req
        if command == "dry_run":
            return jsonify(self._start("dry_run"))
        if command == "start_live":
            return jsonify(self._start("live"))
        if command == "stop":
            return jsonify(self._stop())
        if command == "status":
            return jsonify({"ret": "success", "data": self.status_payload()})
        return jsonify({"ret": "warning", "msg": "지원하지 않는 명령입니다."})

    def process_ajax(self, sub: str, req: Any) -> Any:
        return self.process_command(sub, "", "", "", req)

    def _start(self, mode: str) -> Dict[str, Any]:
        if mode not in RUN_MODES:
            return {"ret": "warning", "msg": "지원하지 않는 실행 모드입니다."}
        try:
            config = runtime_config()
            if not config["library_ids"]:
                raise ValueError("대상 Plex 라이브러리를 하나 이상 설정하세요.")
            # Validate all score keys and divisors before a Run row or thread
            # exists.  Constructing the engine compiles the glob rules too.
            score_module = importlib.import_module(
                ".services.score_engine", __package__
            )
            getattr(score_module, "ScoreEngine")(_score_config(config))
        except Exception as exc:
            return {"ret": "warning", "msg": str(exc)}

        with self.worker_lock:
            if self.worker_thread and self.worker_thread.is_alive():
                return {"ret": "warning", "msg": "이미 정리 작업이 실행 중입니다."}
            self.stop_event.clear()
            try:
                with F.app.app_context():
                    active = ModelCleanupRun.active()
                    if active is not None:
                        return {
                            "ret": "warning",
                            "msg": "DB에 실행 중인 정리 작업이 남아 있습니다.",
                        }
                    run = ModelCleanupRun.create(mode, config)
                    run_id = int(run.id)
            except Exception as exc:
                _rollback_session()
                P.logger.warning("Cleanup run create failed: %s", exc.__class__.__name__)
                return {"ret": "warning", "msg": "실행 이력을 만들지 못했습니다."}

            self.current_run_id = run_id
            self._status = _empty_status()
            self._status.update(
                {
                    "running": True,
                    "status": "queued",
                    "mode": mode,
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "message": "대기",
                }
            )
            self.worker_thread = threading.Thread(
                target=self._worker,
                args=(run_id, mode, config),
                name="plex-dupefinder-cleanup",
                daemon=True,
            )
            try:
                self.worker_thread.start()
            except Exception as exc:
                self.worker_thread = None
                self.current_run_id = None
                with F.app.app_context():
                    failed = ModelCleanupRun.get(run_id)
                    if failed is not None:
                        failed.status = "failed"
                        failed.finished_at = datetime.now()
                        failed.error_message = "worker_start_failed"
                        F.db.session.commit()
                return {"ret": "warning", "msg": "worker를 시작하지 못했습니다."}
        return {"ret": "success", "msg": "작업을 시작했습니다.", "data": self.status_payload()}

    def _stop(self) -> Dict[str, Any]:
        with self.worker_lock:
            worker = self.worker_thread
            run_id = self.current_run_id
            active_statuses = ("queued", "running", "stopping")
            if (
                not worker
                or not worker.is_alive()
                or self._status.get("status") not in active_statuses
            ):
                return {"ret": "warning", "msg": "실행 중인 작업이 없습니다."}
            self.stop_event.set()
            self._status["status"] = "stopping"
            self._status["stop_requested"] = True
            self._status["message"] = "중지 요청됨"
        if run_id is not None:
            try:
                with F.app.app_context():
                    updated = ModelCleanupRun.request_stop(run_id)
                    if not updated:
                        run = ModelCleanupRun.get(run_id)
                        if run is not None:
                            self._set_status_from_run(run)
            except Exception:
                _rollback_session()
        return {
            "ret": "success",
            "msg": "중지 요청을 접수했습니다. 이미 시작된 삭제 건을 마친 뒤 중지합니다.",
            "data": self.status_payload(),
        }

    def status_payload(self) -> Dict[str, Any]:
        with self.worker_lock:
            payload = copy.deepcopy(self._status)
            status = str(payload.get("status") or "")
            active_statuses = ("queued", "running", "stopping")
            payload["running"] = bool(
                self.worker_thread
                and self.worker_thread.is_alive()
                and (not status or status in active_statuses)
            )
        return payload

    def _set_status_from_run(self, run: ModelCleanupRun) -> None:
        with self.worker_lock:
            actions = list(self._status.get("recent_actions", []))
            payload = run.as_api()
            stop_requested = bool(run.stop_requested) or self.stop_event.is_set()
            status = str(payload["status"])
            if stop_requested and status in ("queued", "running"):
                status = "stopping"
            message = "중지 요청됨" if status == "stopping" else payload["message"]
            self._status.update(
                {
                    "running": status in ("queued", "running", "stopping"),
                    "status": status,
                    "mode": run.mode,
                    "stop_requested": stop_requested,
                    "started_at": payload["started_at"],
                    "current": payload["current"],
                    "progress": payload["progress"],
                    "summary": dict(payload["summary"], skipped=self._status["summary"].get("skipped", 0)),
                    "recent_actions": actions[-20:],
                    "message": message,
                }
            )

    def _record_action_status(self, action: ModelCleanupAction) -> None:
        with self.worker_lock:
            items = self._status.setdefault("recent_actions", [])
            action_api = action.as_api()
            items[:] = [item for item in items if item.get("id") != action.id]
            items.append(action_api)
            del items[:-20]

    @staticmethod
    def _save_action(action: ModelCleanupAction) -> None:
        F.db.session.add(action)
        F.db.session.commit()

    def _create_action(
        self,
        run: ModelCleanupRun,
        section_id: str,
        group: Any,
        keep: Any,
        candidate: Any,
        decision: Any,
        status: str,
        sidecars: Sequence[str] = (),
        message: str = "",
    ) -> ModelCleanupAction:
        paths = _candidate_paths(candidate)
        return ModelCleanupAction.create(
            run_id=run.id,
            mode=run.mode,
            section_id=section_id,
            rating_key=str(_value(group, "rating_key", "") or ""),
            media_type=str(_value(group, "media_type", "") or ""),
            title=str(_value(group, "title", "") or ""),
            keep_media_id=_candidate_id(keep),
            delete_media_id=_candidate_id(candidate),
            keep_score=_score_value(decision, keep),
            delete_score=_score_value(decision, candidate),
            file_path=paths[0] if paths else "",
            file_size=_candidate_size(candidate),
            sidecars=list(sidecars),
            candidate_snapshot=_candidate_snapshot(candidate),
            status=status,
            message=message,
        )

    def _shared_sidecars(
        self,
        adapter: CleanupServiceAdapter,
        current_candidates: Mapping[str, Any],
        delete_media_id: str,
    ) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        target = current_candidates[delete_media_id]
        target_paths = tuple(adapter.find_sidecars(target))
        retained: set[str] = set()
        for media_id, candidate in current_candidates.items():
            if media_id == delete_media_id:
                continue
            retained.update(_canonical_path(path) for path in adapter.find_sidecars(candidate))
        exclusive = tuple(
            path for path in target_paths if _canonical_path(path) not in retained
        )
        shared = tuple(
            path for path in target_paths if _canonical_path(path) in retained
        )
        return exclusive, shared

    def _record_skipped_group(
        self,
        run: ModelCleanupRun,
        section_id: str,
        group: Any,
        decision: Any,
        reason: str,
    ) -> None:
        keep = _value(decision, "keep")
        duplicates = tuple(
            _value(decision, "delete_candidates", _value(decision, "duplicates", ()))
            or ()
        )
        for candidate in duplicates:
            action = self._create_action(
                run, section_id, group, keep, candidate, decision, "skipped", message=reason
            )
            action.finished_at = datetime.now()
            self._save_action(action)
            self._record_action_status(action)
            with self.worker_lock:
                self._status["summary"]["skipped"] += 1

    def _worker(self, run_id: int, mode: str, config: Dict[str, Any]) -> None:
        run: Optional[ModelCleanupRun] = None
        try:
            with F.app.app_context():
                run = ModelCleanupRun.get(run_id)
                if run is None:
                    raise RuntimeError("cleanup run disappeared")
                run.status = "running"
                run.started_at = datetime.now()
                run.status_message = "Plex 연결 중"
                F.db.session.commit()
                self._set_status_from_run(run)

                adapter = self.adapter_factory(config)
                for section_id in config["library_ids"]:
                    if self.stop_event.is_set():
                        break
                    for group in adapter.iter_duplicate_groups(
                        section_id, cancel_check=self.stop_event.is_set
                    ):
                        if self.stop_event.is_set():
                            break
                        # The iterator is deliberately consumed one group at a
                        # time: no library-wide preview/batch is accumulated.
                        run.total_groups += 1
                        run.groups_found += 1
                        rating_key = str(_value(group, "rating_key", "") or "")
                        run.current_json = json.dumps(
                            {
                                "section_id": section_id,
                                "rating_key": rating_key,
                                "title": str(_value(group, "title", "") or ""),
                            },
                            ensure_ascii=False,
                        )
                        run.status_message = "중복 그룹 처리 중"
                        F.db.session.commit()
                        self._set_status_from_run(run)

                        decision = adapter.rank(group)
                        keep = _value(decision, "keep")
                        duplicates = tuple(
                            _value(
                                decision,
                                "delete_candidates",
                                _value(decision, "duplicates", ()),
                            )
                            or ()
                        )
                        expected = _candidate_map(group)
                        expected_objects = _candidate_objects(group)
                        if _has_shared_video_path(expected):
                            self._record_skipped_group(
                                run,
                                section_id,
                                group,
                                decision,
                                "shared_video_path",
                            )
                            run.processed_groups += 1
                            F.db.session.commit()
                            self._set_status_from_run(run)
                            continue

                        group_interrupted = False
                        for original_candidate in duplicates:
                            if self.stop_event.is_set():
                                group_interrupted = True
                                break
                            media_id = _candidate_id(original_candidate)
                            current_objects = dict(expected_objects)
                            current_candidate = original_candidate

                            # Snapshot sidecars while every retained candidate
                            # is still known. Shared paths are never unlinked.
                            exclusive_sidecars, shared_sidecars = self._shared_sidecars(
                                adapter, current_objects, media_id
                            )
                            if self.stop_event.is_set():
                                group_interrupted = True
                                break

                            if mode == "live":
                                try:
                                    current_group = adapter.get_group(rating_key)
                                    current_map = _candidate_map(current_group)
                                    current_objects = _candidate_objects(current_group)
                                except Exception:
                                    if self.stop_event.is_set():
                                        group_interrupted = True
                                        break
                                    action = self._create_action(
                                        run,
                                        section_id,
                                        group,
                                        keep,
                                        original_candidate,
                                        decision,
                                        "skipped",
                                        message="metadata_reread_failed",
                                    )
                                    action.finished_at = datetime.now()
                                    self._save_action(action)
                                    self._record_action_status(action)
                                    with self.worker_lock:
                                        self._status["summary"]["skipped"] += 1
                                    continue

                                # A stop received while the fresh metadata was
                                # loading must prevent a new irreversible DELETE.
                                if self.stop_event.is_set():
                                    group_interrupted = True
                                    break
                                if current_map != expected or media_id not in current_objects:
                                    action = self._create_action(
                                        run,
                                        section_id,
                                        group,
                                        keep,
                                        original_candidate,
                                        decision,
                                        "skipped",
                                        message="metadata_media_or_path_changed",
                                    )
                                    action.finished_at = datetime.now()
                                    self._save_action(action)
                                    self._record_action_status(action)
                                    with self.worker_lock:
                                        self._status["summary"]["skipped"] += 1
                                    continue
                                current_candidate = current_objects[media_id]

                            if self.stop_event.is_set():
                                group_interrupted = True
                                break
                            sidecar_note = (
                                "shared_sidecars_preserved=%s" % len(shared_sidecars)
                                if shared_sidecars
                                else ""
                            )

                            if mode == "dry_run":
                                action = self._create_action(
                                    run,
                                    section_id,
                                    group,
                                    keep,
                                    current_candidate,
                                    decision,
                                    "would_delete",
                                    sidecars=exclusive_sidecars,
                                    message=sidecar_note,
                                )
                                action.finished_at = datetime.now()
                                self._save_action(action)
                                run.would_delete_count += 1
                                run.would_delete_bytes += action.file_size or 0
                                F.db.session.commit()
                                self._record_action_status(action)
                                self._set_status_from_run(run)
                                continue

                            # Persist the in-flight state before the only DELETE call.
                            action = self._create_action(
                                run,
                                section_id,
                                group,
                                keep,
                                current_candidate,
                                decision,
                                "deleting",
                                sidecars=exclusive_sidecars,
                                message=sidecar_note,
                            )
                            self._record_action_status(action)
                            response = None
                            delete_exception: Optional[Exception] = None
                            # Action creation commits before the DELETE call.
                            # Recheck immediately after that bookkeeping so a
                            # stop received in this final window cannot start a
                            # new irreversible request.
                            if self.stop_event.is_set():
                                action.status = "skipped"
                                action.finished_at = datetime.now()
                                action.message = ";".join(
                                    item
                                    for item in (
                                        sidecar_note,
                                        "stop_requested_before_delete",
                                    )
                                    if item
                                )
                                self._save_action(action)
                                self._record_action_status(action)
                                with self.worker_lock:
                                    self._status["summary"]["skipped"] += 1
                                group_interrupted = True
                                break
                            try:
                                response = adapter.delete_media(rating_key, media_id)
                                action.response_status = _response_status(response)
                            except Exception as exc:
                                # DELETE is never retried. A transport failure
                                # has an uncertain outcome, so reconcile once
                                # through fresh metadata below.
                                delete_exception = exc

                            try:
                                post_delete_group = adapter.get_group(rating_key)
                                post_delete_map = _candidate_map(post_delete_group)
                            except Exception as exc:
                                action.status = "unknown"
                                action.finished_at = datetime.now()
                                prefix = (
                                    "plex_delete_exception:%s;" % delete_exception.__class__.__name__
                                    if delete_exception is not None
                                    else ""
                                )
                                action.message = "%splex_verify_exception:%s" % (
                                    prefix,
                                    exc.__class__.__name__,
                                )
                                run.error_count += 1
                                self._save_action(action)
                                F.db.session.commit()
                                self._record_action_status(action)
                                self._set_status_from_run(run)
                                # The keep/remaining candidates cannot be
                                # proven intact, so this group must stop here.
                                break

                            if media_id in post_delete_map:
                                action.status = (
                                    "unknown" if delete_exception is not None else "failed"
                                )
                                action.finished_at = datetime.now()
                                action.message = (
                                    "plex_delete_exception:%s;media_still_present"
                                    % delete_exception.__class__.__name__
                                    if delete_exception is not None
                                    else "media_still_present_after_delete"
                                )
                                run.error_count += 1
                                self._save_action(action)
                                F.db.session.commit()
                                self._record_action_status(action)
                                self._set_status_from_run(run)
                                break

                            remaining_expected = dict(expected)
                            remaining_expected.pop(media_id, None)
                            if post_delete_map != remaining_expected:
                                # The target is gone, but Plex also removed or
                                # mutated something that was supposed to stay.
                                # Never unlink sidecars or attempt another
                                # candidate from this now-uncertain group.
                                action.status = "partial"
                                action.finished_at = datetime.now()
                                action.message = "post_delete_remaining_media_changed"
                                run.partial_count += 1
                                self._save_action(action)
                                F.db.session.commit()
                                self._record_action_status(action)
                                self._set_status_from_run(run)
                                break

                            # Plex metadata absence is proven. Keep the expected
                            # map in sync before handling another duplicate.
                            expected = post_delete_map
                            expected_objects = _candidate_objects(post_delete_group)

                            present_video_paths = tuple(
                                path
                                for path in _candidate_paths(current_candidate)
                                if os.path.lexists(path)
                            )
                            if present_video_paths:
                                action.status = "partial"
                                action.finished_at = datetime.now()
                                action.message = "video_file_still_present"
                                run.partial_count += 1
                                self._save_action(action)
                                F.db.session.commit()
                                self._record_action_status(action)
                                self._set_status_from_run(run)
                                continue

                            # Both metadata and original video paths are absent.
                            # Only now may exclusive sidecars be unlinked.
                            try:
                                sidecar_result = adapter.delete_sidecars(exclusive_sidecars)
                                failed = tuple(_value(sidecar_result, "failed", ()) or ())
                            except Exception as exc:
                                failed = (("", exc.__class__.__name__),)

                            action.finished_at = datetime.now()
                            run.deleted_count += 1
                            run.deleted_bytes += action.file_size or 0
                            if failed:
                                action.status = "partial"
                                action.message = "video_deleted_sidecar_failures=%s" % len(failed)
                                run.partial_count += 1
                            else:
                                action.status = "deleted"
                                messages = []
                                if delete_exception is not None:
                                    messages.append(
                                        "plex_delete_exception_reconciled:%s"
                                        % delete_exception.__class__.__name__
                                    )
                                if sidecar_note:
                                    messages.append(sidecar_note)
                                messages.append("video_absent_sidecars_processed")
                                action.message = ";".join(messages)
                            self._save_action(action)
                            F.db.session.commit()
                            self._record_action_status(action)
                            self._set_status_from_run(run)

                        if not group_interrupted:
                            run.processed_groups += 1
                        F.db.session.commit()
                        self._set_status_from_run(run)
                        if group_interrupted:
                            break

                run.stop_requested = self.stop_event.is_set()
                run.finished_at = datetime.now()
                run.current_json = "{}"
                if run.stop_requested:
                    run.status = "stopped"
                    run.status_message = "사용자 요청으로 중지됨"
                elif run.error_count or run.partial_count:
                    run.status = "completed_with_errors"
                    run.status_message = "오류 또는 부분 실패와 함께 완료"
                else:
                    run.status = "completed"
                    run.status_message = "완료"
                F.db.session.commit()
                self._set_status_from_run(run)
        except Exception as exc:
            P.logger.error("Cleanup worker failed: %s", exc.__class__.__name__)
            P.logger.debug(traceback.format_exc())
            try:
                with F.app.app_context():
                    # The triggering error may have left SQLAlchemy's session
                    # in a failed transaction; recovery queries require a
                    # rollback inside a fresh application context.
                    F.db.session.rollback()
                    run = ModelCleanupRun.get(run_id)
                    if run is not None:
                        run.status = "failed"
                        run.finished_at = datetime.now()
                        run.error_message = "worker_failed:%s" % exc.__class__.__name__
                        run.error_count += 1
                        F.db.session.commit()
                        self._set_status_from_run(run)
            except Exception:
                _rollback_session()
        finally:
            try:
                remove = getattr(F.db.session, "remove", None)
                if callable(remove):
                    remove()
            except Exception:
                pass
            with self.worker_lock:
                self._status["running"] = False
                self.current_run_id = None
                if self.worker_thread is threading.current_thread():
                    self.worker_thread = None


__all__ = [
    "CleanupServiceAdapter",
    "ModuleCleanup",
    "build_cleanup_adapter",
]
