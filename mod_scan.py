from __future__ import annotations

import json
import secrets
import time
import traceback
from typing import Any, Dict, List

from flask import jsonify, render_template, session
from framework import F
from plugin import PluginModuleBase

from .batch_delete_manager import BatchDeleteManager
from .delete_service import DeleteService
from .models import ModelDuplicateGroup, ModelMediaCandidate, ModelScanRun
from .post_delete_scan import PostDeleteScanManager
from .scan_manager import ScanManager
from .services.plex_gateway import PlexGateway
from .services.plex_mate_provider import PlexMateProvider
from .setup import P


name = "scan"


def _positive_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s 값이 올바르지 않습니다." % field) from exc
    if result <= 0:
        raise ValueError("%s 값이 올바르지 않습니다." % field)
    return result


def _section_ids(value: Any) -> List[str]:
    if isinstance(value, list):
        values = value
    else:
        raw = str(value or "").strip()
        try:
            decoded = json.loads(raw)
            values = decoded if isinstance(decoded, list) else []
        except (TypeError, ValueError):
            values = raw.split(",")
    return [str(item).strip() for item in values if str(item).strip()]


class ModuleScan(PluginModuleBase):
    db_default = {
        "scan_last_section_ids": "[]",
        "scan_last_run_id": "",
    }

    def __init__(self, plugin: Any) -> None:
        super(ModuleScan, self).__init__(plugin, name=name, first_menu="list")
        self.manager = ScanManager()
        self.post_delete_scan_manager = PostDeleteScanManager()
        self.delete_service = DeleteService(self.post_delete_scan_manager)
        # Share DeleteService so manual and batch work serialize inside this
        # process; DB claims provide the cross-process protection.
        self.batch_manager = BatchDeleteManager(self.delete_service)
        self.post_delete_scan_manager.deletion_recovery_callback = (
            self.batch_manager.recover_interrupted
        )

    def plugin_load(self) -> None:
        scan_count = self.manager.recover_interrupted()
        if scan_count:
            P.logger.warning("Interrupted duplicate scans recovered: %s", scan_count)
        batch_count = self.batch_manager.recover_interrupted()
        delete_counts = self.batch_manager.last_delete_recovery_counts
        if delete_counts["blocked"] or delete_counts["unknown"]:
            P.logger.warning(
                "Interrupted deletes recovered: blocked=%s unknown=%s",
                delete_counts["blocked"],
                delete_counts["unknown"],
            )
        if batch_count:
            P.logger.warning("Interrupted batch deletes recovered: %s", batch_count)
        post_scan_count = self.post_delete_scan_manager.plugin_load()
        if post_scan_count:
            P.logger.warning(
                "Interrupted post-delete scans recovered: %s", post_scan_count
            )

    def plugin_unload(self) -> None:
        self.manager.unload()
        self.batch_manager.unload()
        self.post_delete_scan_manager.unload()

    def process_menu(self, sub: str, req: Any) -> Any:
        arg: Dict[str, Any] = P.ModelSetting.to_dict()
        arg["package_name"] = P.package_name
        arg["module_name"] = self.name
        arg["sub"] = sub
        arg["requested_run_id"] = req.args.get("run_id", "")
        if "plex_dupefinder_ff_csrf" not in session:
            session["plex_dupefinder_ff_csrf"] = secrets.token_urlsafe(32)
        arg["csrf_token"] = session["plex_dupefinder_ff_csrf"]
        template = "%s_%s_%s.html" % (P.package_name, self.name, sub)
        return render_template(template, arg=arg)

    def _libraries(self) -> Dict[str, Any]:
        connection = PlexMateProvider().resolve(require_machine_id=False)
        try:
            timeout = max(5, min(120, int(P.ModelSetting.get("setting_request_timeout") or "20")))
        except (TypeError, ValueError):
            timeout = 20
        gateway = PlexGateway(connection, timeout=(5, timeout))
        identity = gateway.validate_identity(connection.machine_id, require_match=False)
        if connection.machine_id and identity.machine_id != connection.machine_id:
            raise RuntimeError("Plex Machine ID가 plex_mate 설정과 일치하지 않습니다.")
        return {
            "machine_configured": bool(connection.machine_id),
            "server_version": identity.version,
            "sections": [section.as_dict() for section in gateway.list_sections()],
        }

    @staticmethod
    def _csrf(req: Any) -> None:
        expected = session.get("plex_dupefinder_ff_csrf", "")
        actual = req.form.get("csrf_token", "")
        if not expected or not actual or not secrets.compare_digest(expected, actual):
            raise ValueError("보안 토큰이 만료되었습니다. 페이지를 새로고침하세요.")

    def process_ajax(self, sub: str, req: Any) -> Any:
        try:
            if sub in {
                "batch_preview",
                "batch_approve",
                "batch_status",
                "batch_cancel",
                "delete_preview",
                "delete_media",
            }:
                # Opportunistically recover an expired DB lease. A valid lease
                # always belongs to another live web worker and is untouched.
                self.batch_manager.recover_interrupted()
            if sub == "libraries":
                return jsonify({"ret": "success", "data": self._libraries()})

            if sub == "post_delete_scan_status":
                if req.method != "GET":
                    raise ValueError("삭제 후 스캔 상태 조회는 GET만 허용합니다.")
                action_raw = req.args.get("action_id", "")
                batch_raw = req.args.get("batch_id", "")
                action_id = _positive_int(action_raw, "action_id") if action_raw else None
                batch_id = _positive_int(batch_raw, "batch_id") if batch_raw else None
                return jsonify(
                    {
                        "ret": "success",
                        "data": {
                            "items": self.post_delete_scan_manager.status(
                                action_id=action_id,
                                batch_id=batch_id,
                            )
                        },
                    }
                )

            if sub == "start":
                if req.method != "POST":
                    raise ValueError("스캔 시작은 POST만 허용합니다.")
                self._csrf(req)
                ids = _section_ids(req.form.get("section_ids", ""))
                run = self.manager.start(ids)
                P.ModelSetting.set("scan_last_section_ids", json.dumps(ids))
                P.ModelSetting.set("scan_last_run_id", str(run.id))
                return jsonify({"ret": "success", "msg": "중복 스캔을 시작했습니다.", "data": run.as_api()})

            if sub == "cancel":
                if req.method != "POST":
                    raise ValueError("스캔 취소는 POST만 허용합니다.")
                self._csrf(req)
                run_id = _positive_int(req.form.get("run_id"), "run_id")
                run = self.manager.cancel(run_id)
                return jsonify({"ret": "success", "msg": "취소를 요청했습니다.", "data": run.as_api()})

            if sub == "status":
                run_id = req.values.get("run_id")
                run = ModelScanRun.get(run_id) if run_id else ModelScanRun.active()
                if run is None:
                    recent = ModelScanRun.recent(1)
                    run = recent[0] if recent else None
                return jsonify({"ret": "success", "data": run.as_api() if run else None})

            if sub == "runs":
                limit = min(100, max(1, int(req.values.get("limit", 30))))
                return jsonify(
                    {"ret": "success", "data": [run.as_api() for run in ModelScanRun.recent(limit)]}
                )

            if sub == "groups":
                run_id = _positive_int(req.values.get("run_id"), "run_id")
                page = max(1, int(req.values.get("page", 1)))
                page_size = max(10, min(100, int(req.values.get("page_size", 50))))
                result = ModelDuplicateGroup.search(
                    run_id=run_id,
                    page=page,
                    page_size=page_size,
                    media_type=req.values.get("media_type", ""),
                    safety=req.values.get("safety", ""),
                    keyword=req.values.get("keyword", ""),
                )
                total = result["total"]
                pages = max(1, (total + page_size - 1) // page_size)
                return jsonify(
                    {
                        "ret": "success",
                        "data": {
                            "items": [group.as_api() for group in result["items"]],
                            "total": total,
                            "page": page,
                            "page_size": page_size,
                            "pages": pages,
                        },
                    }
                )

            if sub == "batch_preview":
                if req.method != "POST":
                    raise ValueError("일괄 삭제 사전확인은 POST만 허용합니다.")
                self._csrf(req)
                run_id = _positive_int(req.form.get("run_id"), "run_id")
                data = self.batch_manager.preview(run_id)
                session["plex_dupefinder_ff_batch_preview"] = {
                    "plan_id": data["plan_id"],
                    "nonce": data["nonce"],
                    "expires_at": data["expires_at"],
                }
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "일괄 삭제 예정 목록을 생성했습니다.",
                        "data": data,
                    }
                )

            if sub == "batch_approve":
                if req.method != "POST":
                    raise ValueError("일괄 삭제 승인은 POST만 허용합니다.")
                self._csrf(req)
                preview = session.pop("plex_dupefinder_ff_batch_preview", None)
                if not preview or int(preview.get("expires_at", 0)) < int(time.time()):
                    raise ValueError("일괄 삭제 사전확인이 만료되었습니다. 다시 확인하세요.")
                plan_id = _positive_int(req.form.get("plan_id"), "plan_id")
                supplied_nonce = str(req.form.get("nonce", ""))
                if int(preview.get("plan_id", 0)) != plan_id or not secrets.compare_digest(
                    str(preview.get("nonce", "")), supplied_nonce
                ):
                    raise ValueError("일괄 삭제 사전확인 정보가 일치하지 않습니다.")
                data = self.batch_manager.approve(
                    batch_id=plan_id,
                    nonce=supplied_nonce,
                    confirmation=req.form.get("confirmation", ""),
                )
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "일괄 삭제를 승인했습니다.",
                        "data": data,
                    }
                )

            if sub == "batch_status":
                plan_id_raw = req.values.get("plan_id")
                run_id_raw = req.values.get("run_id")
                plan_id = _positive_int(plan_id_raw, "plan_id") if plan_id_raw else None
                run_id = _positive_int(run_id_raw, "run_id") if run_id_raw else None
                data = self.batch_manager.status(batch_id=plan_id, run_id=run_id)
                return jsonify({"ret": "success", "data": data})

            if sub == "batch_cancel":
                if req.method != "POST":
                    raise ValueError("일괄 삭제 취소는 POST만 허용합니다.")
                self._csrf(req)
                plan_id = _positive_int(req.form.get("plan_id"), "plan_id")
                data = self.batch_manager.cancel(plan_id)
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "일괄 삭제 취소를 요청했습니다.",
                        "data": data,
                    }
                )

            if sub == "group_detail":
                group_id = _positive_int(req.values.get("group_id"), "group_id")
                group = ModelDuplicateGroup.get(group_id)
                if group is None:
                    raise ValueError("중복 그룹을 찾을 수 없습니다.")
                candidates = ModelMediaCandidate.by_group(group.id, include_deleted=True)
                return jsonify(
                    {
                        "ret": "success",
                        "data": {
                            "group": group.as_api(),
                            "candidates": [candidate.as_api() for candidate in candidates],
                            "delete_enabled": P.ModelSetting.get("setting_delete_enabled") == "True",
                        },
                    }
                )

            if sub == "delete_preview":
                if req.method != "POST":
                    raise ValueError("삭제 사전확인은 POST만 허용합니다.")
                self._csrf(req)
                group_id = _positive_int(req.form.get("group_id"), "group_id")
                candidate_id = _positive_int(req.form.get("candidate_id"), "candidate_id")
                keep_candidate_id = _positive_int(
                    req.form.get("keep_candidate_id"), "keep_candidate_id"
                )
                group = ModelDuplicateGroup.get(group_id)
                candidate = ModelMediaCandidate.get(candidate_id)
                keep = ModelMediaCandidate.get(keep_candidate_id)
                if group is None or candidate is None or keep is None:
                    raise ValueError("삭제 대상 정보를 찾을 수 없습니다.")
                if candidate.group_id != group.id or keep.group_id != group.id:
                    raise ValueError("후보가 동일한 중복 그룹에 속하지 않습니다.")
                if candidate.id == keep.id or candidate.deleted or keep.deleted:
                    raise ValueError("유지 및 삭제 후보 선택이 올바르지 않습니다.")
                if not group.safe_to_delete or group.resolution_status != "open":
                    raise RuntimeError("안전 삭제가 허용되지 않은 그룹입니다.")
                if P.ModelSetting.get("setting_delete_enabled") != "True":
                    raise RuntimeError("설정에서 수동 삭제를 활성화해야 합니다.")

                nonce = secrets.token_urlsafe(24)
                expires_at = int(time.time()) + 120
                session["plex_dupefinder_ff_delete_preview"] = {
                    "nonce": nonce,
                    "group_id": group.id,
                    "candidate_id": candidate.id,
                    "keep_candidate_id": keep.id,
                    "expires_at": expires_at,
                }
                return jsonify(
                    {
                        "ret": "success",
                        "data": {
                            "nonce": nonce,
                            "confirmation": "DELETE %s" % candidate.media_id,
                            "expires_at": expires_at,
                            "delete_media_id": candidate.media_id,
                            "keep_media_id": keep.media_id,
                        },
                    }
                )

            if sub == "delete_media":
                if req.method != "POST":
                    raise ValueError("삭제 요청은 POST만 허용합니다.")
                self._csrf(req)
                preview = session.pop("plex_dupefinder_ff_delete_preview", None)
                if not preview or preview.get("expires_at", 0) < int(time.time()):
                    raise ValueError("삭제 사전확인이 만료되었습니다. 다시 확인하세요.")
                supplied = {
                    "group_id": _positive_int(req.form.get("group_id"), "group_id"),
                    "candidate_id": _positive_int(req.form.get("candidate_id"), "candidate_id"),
                    "keep_candidate_id": _positive_int(
                        req.form.get("keep_candidate_id"), "keep_candidate_id"
                    ),
                }
                if not secrets.compare_digest(
                    str(preview.get("nonce", "")), str(req.form.get("nonce", ""))
                ) or any(int(preview.get(key, 0)) != value for key, value in supplied.items()):
                    raise ValueError("삭제 사전확인 정보가 일치하지 않습니다.")
                result = self.delete_service.delete(
                    group_id=supplied["group_id"],
                    candidate_id=supplied["candidate_id"],
                    keep_candidate_id=supplied["keep_candidate_id"],
                    confirmation=req.form.get("confirmation", ""),
                )
                return jsonify({"ret": "success", "msg": "삭제와 사후 검증을 완료했습니다.", "data": result})

            return jsonify({"ret": "danger", "msg": "지원하지 않는 요청입니다."}), 400
        except Exception as exc:
            P.logger.warning("DupeFinder request blocked: action=%s error=%s", sub, exc.__class__.__name__)
            P.logger.debug(traceback.format_exc())
            return jsonify({"ret": "danger", "msg": str(exc)}), 400
