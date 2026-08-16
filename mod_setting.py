from __future__ import annotations

import traceback
from typing import Any, Dict

from flask import jsonify, render_template
from plugin import PluginModuleBase

from .services.plex_gateway import PlexGateway
from .services.plex_mate_provider import PlexMateProvider
from .setup import P


name = "setting"


def _mask_machine(value: str) -> str:
    value = value or ""
    if len(value) <= 8:
        return value
    return "%s…%s" % (value[:4], value[-4:])


def _request_timeout() -> int:
    try:
        return max(5, min(120, int(P.ModelSetting.get("setting_request_timeout") or "20")))
    except (TypeError, ValueError):
        return 20


class ModuleSetting(PluginModuleBase):
    db_default = {
        "setting_db_version": "1",
        "setting_delete_enabled": "False",
        "setting_allowed_roots": "",
        "setting_max_delete_per_run": "1",
        "setting_request_timeout": "20",
        "setting_require_guid": "True",
        "setting_block_multipart": "True",
        "setting_video_codec_scores": "av1=5000\nhevc=4000\nh265=4000\nvp9=3000\nh264=2000\nmpeg4=1000\nmpeg2video=500",
        "setting_audio_codec_scores": "truehd=5000\ndca=4000\ndts=4000\neac3=3000\nac3=2000\naac=1000\nmp3=500",
        "setting_resolution_scores": "4k=40000\n2160=40000\n1080=20000\n720=10000\n576=6000\n480=5000\nsd=1000",
        "setting_filename_rules": "*remux*=10000\n*bluray*=4000\n*web-dl*=2500\n*webrip*=1500",
        "setting_bitrate_weight": "2",
        "setting_duration_weight": "0.0033333333",
        "setting_dimension_weight": "2",
        "setting_audio_channel_weight": "1000",
        "setting_use_filesize": "False",
        "setting_filesize_weight": "0.00001",
    }

    def __init__(self, plugin: Any) -> None:
        super(ModuleSetting, self).__init__(plugin, name=name, first_menu="setting")

    def process_menu(self, sub: str, req: Any) -> Any:
        arg: Dict[str, Any] = P.ModelSetting.to_dict()
        arg["package_name"] = P.package_name
        arg["module_name"] = self.name
        arg["sub"] = sub
        return render_template("%s_%s_setting.html" % (P.package_name, self.name), arg=arg)

    def _connection_payload(self, include_sections: bool = False) -> Dict[str, Any]:
        connection = PlexMateProvider().resolve(require_machine_id=False)
        timeout = _request_timeout()
        gateway = PlexGateway(connection, timeout=(5, timeout))
        identity = gateway.validate_identity(connection.machine_id, require_match=False)
        if connection.machine_id and identity.machine_id != connection.machine_id:
            raise RuntimeError("Plex Machine ID가 plex_mate 설정과 일치하지 않습니다.")

        payload: Dict[str, Any] = {
            "base_url": connection.base_url,
            "configured_machine": _mask_machine(connection.machine_id),
            "server_machine": _mask_machine(identity.machine_id),
            "machine_match": bool(connection.machine_id and connection.machine_id == identity.machine_id),
            "server_version": identity.version,
        }
        if include_sections:
            payload["sections"] = [section.as_dict() for section in gateway.list_sections()]
        return payload

    def process_ajax(self, sub: str, req: Any) -> Any:
        try:
            if sub == "connection_status":
                return jsonify({"ret": "success", "data": self._connection_payload(False)})
            if sub == "libraries":
                return jsonify({"ret": "success", "data": self._connection_payload(True)})
            return jsonify({"ret": "danger", "msg": "지원하지 않는 요청입니다."}), 400
        except Exception as exc:
            P.logger.warning("PlexMate connection check failed: %s", exc.__class__.__name__)
            P.logger.debug(traceback.format_exc())
            return jsonify({"ret": "danger", "msg": str(exc)}), 400
