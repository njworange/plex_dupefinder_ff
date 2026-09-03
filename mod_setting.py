from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List

from flask import jsonify, render_template
from plugin import PluginModuleBase

from .setup import P


name = "setting"


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
        "include_size": _setting_bool("setting_size_score", False, strict=True),
        "subtitle_extensions": extensions,
        "subs_search": _setting_bool("setting_subs_search", True),
        "timeout": timeout,
        "score_profile": "v2.0.0",
    }


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
        "setting_db_version": "2",
        "setting_library_id": "",
        "setting_score_json": "{}",
        "setting_filename_score": "{}",
        "setting_size_score": "False",
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
        return jsonify({"ret": "warning", "msg": "지원하지 않는 명령입니다."})

    def setting_save_after(self, change_list: Any) -> None:
        del change_list
        sync_scheduler_settings()


__all__ = [
    "ModuleSetting",
    "parse_library_ids",
    "runtime_config",
    "sync_scheduler_settings",
]
