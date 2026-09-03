from __future__ import annotations

from typing import Any

from flask import jsonify, render_template
from plugin import PluginModuleBase

from .models import ModelCleanupAction, ModelCleanupRun
from .setup import P


name = "history"


class ModuleHistory(PluginModuleBase):
    def __init__(self, plugin: Any) -> None:
        super(ModuleHistory, self).__init__(plugin, name=name, first_menu="list")
        self.web_list_model = ModelCleanupAction

    def process_menu(self, sub: str, req: Any) -> Any:
        arg = P.ModelSetting.to_dict()
        arg["package_name"] = P.package_name
        arg["module_name"] = self.name
        arg["sub"] = sub
        return render_template(
            "%s_%s_list.html" % (P.package_name, self.name), arg=arg
        )

    def process_command(
        self, command: str, arg1: str, arg2: str, arg3: str, req: Any
    ) -> Any:
        del arg1, arg2, arg3, req
        if command != "history":
            return jsonify({"ret": "warning", "msg": "지원하지 않는 명령입니다."})
        try:
            actions = [item.as_api() for item in ModelCleanupAction.recent(100)]
            runs = [item.as_api() for item in ModelCleanupRun.recent(30)]
            return jsonify(
                {"ret": "success", "data": {"items": actions, "runs": runs}}
            )
        except Exception as exc:
            P.logger.warning("Cleanup history query failed: %s", exc.__class__.__name__)
            return jsonify({"ret": "warning", "msg": "작업 이력을 읽지 못했습니다."})

    def process_ajax(self, sub: str, req: Any) -> Any:
        return self.process_command(sub, "", "", "", req)


__all__ = ["ModuleHistory"]
