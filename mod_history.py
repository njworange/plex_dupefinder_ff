from __future__ import annotations

import traceback
from typing import Any, Dict

from flask import jsonify, render_template
from plugin import PluginModuleBase

from .models import ModelActionLog
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
        return render_template("%s_%s_list.html" % (P.package_name, self.name), arg=arg)

    def process_ajax(self, sub: str, req: Any) -> Any:
        try:
            if sub == "actions":
                page = max(1, int(req.values.get("page", 1)))
                page_size = max(10, min(100, int(req.values.get("page_size", 50))))
                run_value = req.values.get("run_id", "")
                run_id = int(run_value) if str(run_value).strip() else None
                status = str(req.values.get("status", "")).strip()
                subtitle_filter = str(req.values.get("subtitle_filter", "")).strip()
                if subtitle_filter not in ("", "excluded", "quarantined"):
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
            return jsonify({"ret": "danger", "msg": "지원하지 않는 요청입니다."}), 400
        except Exception as exc:
            P.logger.warning("History request failed: %s", exc.__class__.__name__)
            P.logger.debug(traceback.format_exc())
            return jsonify({"ret": "danger", "msg": str(exc)}), 400
