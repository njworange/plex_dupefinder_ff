from __future__ import annotations

import secrets
import traceback
from typing import Any, Dict

from flask import jsonify, render_template, session
from plugin import PluginModuleBase

from .models import ModelActionLog, ModelPostDeleteScanJob
from .setup import P


name = "history"


class ModuleHistory(PluginModuleBase):
    def __init__(self, plugin: Any) -> None:
        super(ModuleHistory, self).__init__(plugin, name=name, first_menu="list")

    def process_menu(self, sub: str, req: Any) -> Any:
        arg: Dict[str, Any] = P.ModelSetting.to_dict()
        arg["package_name"] = P.package_name
        arg["module_name"] = self.name
        arg["sub"] = sub
        if "plex_dupefinder_ff_csrf" not in session:
            session["plex_dupefinder_ff_csrf"] = secrets.token_urlsafe(32)
        arg["csrf_token"] = session["plex_dupefinder_ff_csrf"]
        return render_template("%s_%s_list.html" % (P.package_name, self.name), arg=arg)

    @staticmethod
    def _csrf(req: Any) -> None:
        expected = session.get("plex_dupefinder_ff_csrf", "")
        actual = req.form.get("csrf_token", "")
        if not expected or not actual or not secrets.compare_digest(expected, actual):
            raise ValueError("보안 토큰이 만료되었습니다. 페이지를 새로고침하세요.")

    def process_ajax(self, sub: str, req: Any) -> Any:
        try:
            if sub == "actions":
                page = max(1, int(req.values.get("page", 1)))
                page_size = max(10, min(100, int(req.values.get("page_size", 50))))
                run_value = req.values.get("run_id", "")
                run_id = int(run_value) if str(run_value).strip() else None
                status = str(req.values.get("status", "")).strip()
                subtitle_filter = str(req.values.get("subtitle_filter", "")).strip()
                if subtitle_filter not in ("", "excluded", "quarantined", "deleted"):
                    raise ValueError("외부 자막 필터가 올바르지 않습니다.")
                result = ModelActionLog.search(
                    page=page,
                    page_size=page_size,
                    run_id=run_id,
                    status=status,
                    subtitle_filter=subtitle_filter,
                )
                total = result["total"]
                return jsonify(
                    {
                        "ret": "success",
                        "data": {
                            "items": [
                                item.as_api(include_snapshots=False) for item in result["items"]
                            ],
                            "total": total,
                            "page": page,
                            "page_size": page_size,
                            "pages": max(1, (total + page_size - 1) // page_size),
                        },
                    }
                )
            if sub == "action_detail":
                action_id = int(req.values.get("action_id", 0))
                if action_id <= 0:
                    raise ValueError("action_id가 올바르지 않습니다.")
                item = ModelActionLog.get(action_id)
                if item is None:
                    raise ValueError("감사 이력을 찾을 수 없습니다.")
                return jsonify({"ret": "success", "data": item.as_api(include_snapshots=True)})
            if sub == "force_delete":
                if req.method != "POST":
                    raise ValueError("작업 이력 강제 삭제는 POST만 허용합니다.")
                self._csrf(req)
                item_type = str(req.form.get("item_type", "")).strip()
                if item_type not in ("action", "post_scan"):
                    raise ValueError("작업 이력 종류가 올바르지 않습니다.")
                try:
                    item_id = int(req.form.get("item_id", 0))
                except (TypeError, ValueError):
                    item_id = 0
                if item_id <= 0:
                    raise ValueError("작업 이력 ID가 올바르지 않습니다.")
                expected_confirmation = "FORCE DELETE %s %s" % (
                    "ACTION" if item_type == "action" else "POST_SCAN",
                    item_id,
                )
                confirmation = str(req.form.get("confirmation", ""))
                if not secrets.compare_digest(expected_confirmation, confirmation):
                    raise ValueError("작업 이력 강제 삭제 확인값이 일치하지 않습니다.")
                model = (
                    ModelActionLog
                    if item_type == "action"
                    else ModelPostDeleteScanJob
                )
                result = model.force_delete_history(item_id)
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "DB 작업 이력을 강제로 삭제했습니다. 파일과 Plex에는 명령을 보내지 않았습니다.",
                        "data": result,
                    }
                )
            return jsonify({"ret": "danger", "msg": "지원하지 않는 요청입니다."}), 400
        except Exception as exc:
            P.logger.warning("History request failed: %s", exc.__class__.__name__)
            P.logger.debug(traceback.format_exc())
            return jsonify({"ret": "danger", "msg": str(exc)}), 400
