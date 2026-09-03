from __future__ import annotations

import json
import math
import re
import traceback
from typing import Any, Dict, List

from flask import jsonify, render_template
from plugin import PluginModuleBase

from .services.score_engine import (
    DEFAULT_AUDIO_CODEC_SCORES,
    DEFAULT_FILENAME_SCORES,
    DEFAULT_RESOLUTION_SCORES,
    DEFAULT_VIDEO_CODEC_SCORES,
)
from .setup import P


name = "setting"
SETTINGS_SCHEMA_VERSION = "3"

ORIGINAL_SCORE_SETTINGS = {
    "audio_codec_scores": DEFAULT_AUDIO_CODEC_SCORES,
    "video_codec_scores": DEFAULT_VIDEO_CODEC_SCORES,
    "resolution_scores": DEFAULT_RESOLUTION_SCORES,
    "bitrate_weight": 2,
    "duration_divisor": 300,
    "dimensions_weight": 2,
    "audio_channels_weight": 1000,
    "size_divisor": 100000,
}
DEFAULT_SCORE_JSON = json.dumps(
    ORIGINAL_SCORE_SETTINGS, ensure_ascii=False, indent=2
)
DEFAULT_FILENAME_SCORE_JSON = json.dumps(
    DEFAULT_FILENAME_SCORES, ensure_ascii=False, indent=2
)


def parse_library_ids(value: Any) -> List[str]:
    """Accept the UI's single value, CSV, semicolon, or multiline input."""

    values: List[str] = []
    for raw in re.split(r"[,;\r\n]+", str(value or "")):
        item = raw.strip()
        if item and item not in values:
            values.append(item)
    return values


def _setting_bool(key: str, default: bool = False, strict: bool = False) -> bool:
    value = P.ModelSetting.get(key)
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    if strict:
        raise ValueError("%s 값은 True 또는 False여야 합니다." % key)
    return default


def _json_object_setting(key: str) -> Dict[str, Any]:
    raw = P.ModelSetting.get(key) or "{}"
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s 값은 올바른 JSON 객체여야 합니다." % key) from exc
    if not isinstance(value, dict):
        raise ValueError("%s 값은 JSON 객체여야 합니다." % key)
    return value


def _is_empty_json_object(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    try:
        return json.loads(text) == {}
    except (TypeError, ValueError):
        return False


def _finite_score_mapping(key: str) -> Dict[str, float]:
    value = _json_object_setting(key)
    result: Dict[str, float] = {}
    for pattern, raw_score in value.items():
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("%s의 pattern은 비어 있지 않은 문자열이어야 합니다." % key)
        try:
            score = float(raw_score)
        except (TypeError, ValueError) as exc:
            raise ValueError("%s의 score는 숫자여야 합니다." % key) from exc
        if not math.isfinite(score):
            raise ValueError("%s의 score는 유한한 숫자여야 합니다." % key)
        result[pattern] = score
    return result


def runtime_config() -> Dict[str, Any]:
    score_json = _json_object_setting("setting_score_json")
    filename_scores = _finite_score_mapping("setting_filename_score")
    library_ids = parse_library_ids(P.ModelSetting.get("setting_library_id"))
    invalid_library_ids = [value for value in library_ids if not value.isdigit()]
    if invalid_library_ids:
        raise ValueError(
            "Plex Library ID는 숫자여야 합니다: %s"
            % ", ".join(invalid_library_ids)
        )

    try:
        timeout = int(P.ModelSetting.get("setting_timeout") or "20")
    except (TypeError, ValueError) as exc:
        raise ValueError("setting_timeout 값은 정수여야 합니다.") from exc
    timeout = max(5, min(timeout, 120))

    extensions = []
    for raw in re.split(
        r"[,;\s]+", P.ModelSetting.get("setting_subtitle_extensions") or ""
    ):
        ext = raw.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        if ext not in extensions:
            extensions.append(ext)
    if not extensions:
        raise ValueError("setting_subtitle_extensions에 확장자를 하나 이상 설정하세요.")

    return {
        "library_ids": library_ids,
        "score": score_json,
        "filename_scores": filename_scores,
        "include_size": _setting_bool("setting_size_score", True, strict=True),
        "subtitle_extensions": extensions,
        "subs_search": _setting_bool("setting_subs_search", True),
        "timeout": timeout,
        "score_profile": "v2.1.0-upstream-example",
    }


def library_sections() -> List[Dict[str, str]]:
    """Read video libraries like plex_mate's webhook settings picker."""

    from .services.plex_gateway import PlexGateway
    from .services.plex_mate_provider import PlexMateProvider

    try:
        timeout = int(P.ModelSetting.get("setting_timeout") or "20")
    except (TypeError, ValueError):
        timeout = 20
    timeout = max(5, min(timeout, 120))
    provider = PlexMateProvider()
    try:
        plex_mate = provider.get_plugin()
        db_handle = getattr(plex_mate, "PlexDBHandle", None)
        db_loader = getattr(db_handle, "library_sections", None)
        if callable(db_loader):
            db_rows = db_loader()
            if db_rows is not None:
                db_result: List[Dict[str, str]] = []
                type_names = {1: "movie", 2: "show"}
                for row in db_rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        section_type = int(row.get("section_type") or 0)
                    except (TypeError, ValueError):
                        continue
                    section_id = str(row.get("id") or "").strip()
                    if section_type not in type_names or not section_id.isdigit():
                        continue
                    db_result.append(
                        {
                            "id": section_id,
                            "name": str(
                                row.get("name") or "Library %s" % section_id
                            ),
                            "type": type_names[section_type],
                        }
                    )
                db_result.sort(
                    key=lambda item: (int(item["id"]), item["name"].casefold())
                )
                return db_result
    except Exception as exc:
        P.logger.debug(
            "plex_mate DB library lookup unavailable (%s); using Plex Web API",
            exc.__class__.__name__,
        )

    connection = provider.resolve(require_machine_id=False)
    gateway = PlexGateway(connection, timeout=(5, timeout))
    result: List[Dict[str, str]] = []
    for section in gateway.list_sections():
        if section.plex_item_type not in (1, 4) or not section.key.isdigit():
            continue
        result.append(
            {
                "id": section.key,
                "name": section.title,
                "type": section.section_type,
            }
        )
    result.sort(key=lambda item: (int(item["id"]), item["name"].casefold()))
    return result


def sync_scheduler_settings() -> bool:
    mode = str(
        P.ModelSetting.get("setting_scheduler_mode") or "off"
    ).strip().lower()
    raw_interval = str(
        P.ModelSetting.get("setting_scheduler_interval") or "60"
    ).strip()
    try:
        interval = int(raw_interval)
        if interval <= 0:
            raise ValueError
    except (TypeError, ValueError):
        interval = 60
        mode = "off"
        P.logger.warning("scheduler interval은 양의 정수(분)여야 합니다.")

    enabled = mode in ("dry_run", "live")
    setter = getattr(P.ModelSetting, "set", None)
    if callable(setter):
        setter("cleanup_interval", str(interval))
        setter("cleanup_auto_start", str(enabled))

    logic = getattr(P, "logic", None)
    if logic is not None:
        try:
            logic.scheduler_stop("cleanup")
        except Exception:
            pass
        if enabled:
            try:
                logic.scheduler_start("cleanup")
            except Exception as exc:
                P.logger.warning(
                    "Cleanup scheduler start failed: %s", exc.__class__.__name__
                )
    return enabled


class ModuleSetting(PluginModuleBase):
    db_default = {
        "setting_db_version": SETTINGS_SCHEMA_VERSION,
        "setting_library_id": "",
        "setting_score_json": DEFAULT_SCORE_JSON,
        "setting_filename_score": DEFAULT_FILENAME_SCORE_JSON,
        "setting_size_score": "True",
        "setting_subtitle_extensions": ".srt,.ass,.ssa,.sub,.idx,.vtt,.smi,.sup",
        "setting_subs_search": "True",
        "setting_timeout": "20",
        "setting_scheduler_mode": "off",
        "setting_scheduler_interval": "60",
    }

    def __init__(self, plugin: Any) -> None:
        super(ModuleSetting, self).__init__(plugin, name=name, first_menu="setting")

    def process_menu(self, sub: str, req: Any) -> Any:
        arg = P.ModelSetting.to_dict()
        if _is_empty_json_object(arg.get("setting_score_json")):
            arg["setting_score_json"] = DEFAULT_SCORE_JSON
        if _is_empty_json_object(arg.get("setting_filename_score")):
            arg["setting_filename_score"] = DEFAULT_FILENAME_SCORE_JSON
        arg["package_name"] = P.package_name
        arg["module_name"] = self.name
        arg["sub"] = sub
        return render_template(
            "%s_%s_setting.html" % (P.package_name, self.name), arg=arg
        )

    def process_command(
        self, command: str, arg1: str, arg2: str, arg3: str, req: Any
    ) -> Any:
        del arg1, arg2, arg3, req
        if command == "runtime_config":
            return jsonify({"ret": "success", "data": runtime_config()})
        if command == "libraries":
            try:
                return jsonify({"ret": "success", "data": library_sections()})
            except Exception as exc:
                P.logger.warning(
                    "Plex library lookup failed: %s", exc.__class__.__name__
                )
                P.logger.debug(traceback.format_exc())
                return jsonify(
                    {
                        "ret": "danger",
                        "msg": "plex_mate를 통한 라이브러리 조회에 실패했습니다: %s"
                        % str(exc),
                    }
                )
        return jsonify({"ret": "warning", "msg": "지원하지 않는 명령입니다."})

    def process_ajax(self, sub: str, req: Any) -> Any:
        return self.process_command(sub, "", "", "", req)

    def migration(self) -> None:
        try:
            current = int(P.ModelSetting.get("setting_db_version") or "0")
        except (TypeError, ValueError):
            current = 0
        if current >= int(SETTINGS_SCHEMA_VERSION):
            return

        score_was_empty = _is_empty_json_object(
            P.ModelSetting.get("setting_score_json")
        )
        filename_was_empty = _is_empty_json_object(
            P.ModelSetting.get("setting_filename_score")
        )
        if score_was_empty:
            P.ModelSetting.set("setting_score_json", DEFAULT_SCORE_JSON)
        if filename_was_empty:
            P.ModelSetting.set(
                "setting_filename_score", DEFAULT_FILENAME_SCORE_JSON
            )
        if score_was_empty and filename_was_empty:
            P.ModelSetting.set("setting_size_score", "True")
        P.ModelSetting.set("setting_db_version", SETTINGS_SCHEMA_VERSION)

    def setting_save_after(self, change_list: Any) -> None:
        del change_list
        sync_scheduler_settings()


__all__ = [
    "DEFAULT_FILENAME_SCORE_JSON",
    "DEFAULT_SCORE_JSON",
    "ModuleSetting",
    "ORIGINAL_SCORE_SETTINGS",
    "library_sections",
    "parse_library_ids",
    "runtime_config",
    "sync_scheduler_settings",
]
