from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BatchUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scan_list = (ROOT / "templates" / "plex_dupefinder_ff_scan_list.html").read_text(
            encoding="utf-8"
        )
        self.results = (
            ROOT / "templates" / "plex_dupefinder_ff_scan_results.html"
        ).read_text(encoding="utf-8")
        self.setting = (
            ROOT / "templates" / "plex_dupefinder_ff_setting_setting.html"
        ).read_text(encoding="utf-8")
        self.history = (
            ROOT / "templates" / "plex_dupefinder_ff_history_list.html"
        ).read_text(encoding="utf-8")
        self.scan_module = (ROOT / "mod_scan.py").read_text(encoding="utf-8")
        self.batch_module = (ROOT / "batch_delete_manager.py").read_text(
            encoding="utf-8"
        )

    def test_library_select_buttons_start_disabled_and_update_scan_state(self) -> None:
        for element_id in ("select_all_libraries_btn", "clear_all_libraries_btn"):
            tag = re.search(r'<button[^>]+id="%s"[^>]*>' % element_id, self.scan_list)
            self.assertIsNotNone(tag)
            self.assertIn("disabled", tag.group(0))
        self.assertIn("function updateLibraryButtons()", self.scan_list)
        self.assertIn("$('.pdff-section').prop('checked', true)", self.scan_list)
        self.assertIn("$('.pdff-section').prop('checked', false)", self.scan_list)
        self.assertIn("selected === 0", self.scan_list)

    def test_auto_cleanup_setting_is_opt_in_without_item_cap(self) -> None:
        self.assertIn("setting_batch_delete_enabled", self.setting)
        self.assertNotIn("setting_batch_max_items", self.setting)
        self.assertIn("항목 수 상한은 없습니다", self.setting)
        self.assertIn("batchEnabled && !deleteEnabled", self.setting)

    def test_post_delete_scan_setting_is_opt_in_and_enum_validated(self) -> None:
        self.assertIn("setting_post_delete_scan_mode", self.setting)
        for value in ("none", "binary", "web"):
            self.assertRegex(self.setting, r"['\"]%s['\"]" % value)
        self.assertIn("postDeleteScanModes.indexOf(scanMode) < 0", self.setting)
        self.assertIn("scan.selected_supported === true", self.setting)
        self.assertIn("scan.binary_helper_exported === true", self.setting)
        self.assertIn("scan.binary_scanner_configured === true", self.setting)
        self.assertIn("scan.web_connection_validated === true", self.setting)
        self.assertIn("파일 처리 전에 필수 검증", self.setting)
        self.assertIn("안전 격리와 Plex Media DELETE + 외부 자막 정리", self.setting)

    def test_capability_diagnostics_do_not_execute_a_scan(self) -> None:
        setting_module = (ROOT / "mod_setting.py").read_text(encoding="utf-8")
        self.assertIn('getattr(binary_scanner, "scan_refresh", None)', setting_module)
        self.assertNotIn("PlexWebHandle", setting_module)
        self.assertNotRegex(setting_module, r"\.scan_refresh\s*\(")

    def test_history_shows_post_delete_scan_status_with_escaped_fields(self) -> None:
        self.assertIn('id="post_scan_refresh_btn"', self.history)
        self.assertRegex(
            self.history,
            r"PDFF\.request\(packageName, 'scan', 'post_delete_scan_status', \{page: postScanPage, page_size: postScanPageSize\}, 'GET'",
        )
        self.assertIn("PDFF.badge(item.status || 'unknown')", self.history)
        for field in (
            "item.last_error",
            "item.section_key || '-'",
            "item.target_path || '-'",
            "attempts",
            "links",
        ):
            self.assertIn("PDFF.esc(%s)" % field, self.history)
        self.assertIn("Array.isArray(result.items)", self.history)
        for element_id in (
            "post_scan_prev_btn",
            "post_scan_next_btn",
            "post_scan_page_label",
            "post_scan_summary",
        ):
            self.assertIn('id="%s"' % element_id, self.history)
        self.assertIn("postScanPage = Math.max", self.history)
        self.assertIn("postScanTotalPages = Math.max", self.history)
        self.assertIn("loadPostDeleteScans(false)", self.history)
        self.assertIn("전체 작업을 최신순으로 페이지 조회", self.history)

    def test_post_delete_scan_retry_statuses_have_warning_badges(self) -> None:
        static = (ROOT / "static" / "pdff.js").read_text(encoding="utf-8")
        self.assertIn("'retry_wait'", static)
        self.assertIn("'unverified'", static)

    def test_batch_mutations_use_post_and_csrf(self) -> None:
        functions = {
            "batch_preview": "previewBatch",
            "batch_approve": "approveBatch",
            "batch_cancel": "cancelBatch",
        }
        for action, function_name in functions.items():
            body = self.results.split("function %s()" % function_name, 1)[1].split(
                "\nfunction ", 1
            )[0]
            self.assertIn("'%s'" % action, body)
            self.assertIn("'POST'", body, action)
            self.assertIn("csrf_token", body, action)
        self.assertRegex(
            self.results,
            r"PDFF\.request\(packageName, 'scan', 'batch_status'.*?'GET'",
        )

    def test_auto_cleanup_has_no_typed_or_popup_confirmation_and_escapes_fields(self) -> None:
        approve = self.results.split("function approveBatch()", 1)[1].split(
            "\nfunction ", 1
        )[0]
        self.assertNotIn("window.confirm", approve)
        self.assertNotIn("confirmation_input", self.results)
        self.assertIn("confirmation: confirmation", approve)
        self.assertIn("nonce:", approve)
        self.assertIn("csrf_token: csrfToken", approve)
        for expression in ("title", "keep.mediaId", "del.mediaId", "message"):
            self.assertIn("PDFF.esc(%s)" % expression, self.results)
        self.assertIn("PDFF.esc(keep.paths.join", self.results)
        self.assertIn("PDFF.esc(del.paths.join", self.results)

    def test_auto_cleanup_is_one_click_preview_then_approve(self) -> None:
        self.assertIn(
            '>최고 점수만 남기고 자동 정리 시작</button>', self.results
        )
        self.assertNotIn('id="batch_approve_btn"', self.results)
        preview = self.results.split("function previewBatch()", 1)[1].split(
            "\nfunction ", 1
        )[0]
        self.assertIn("'batch_preview'", preview)
        self.assertIn("var canApprove = renderBatchPlan(previewData)", preview)
        self.assertIn("if (canApprove)", preview)
        self.assertIn("approveBatch();", preview)
        self.assertIn("previewExclusions", preview)
        self.assertIn(
            "!batchPlanId(previewData) && !previewExclusions.length", preview
        )
        self.assertNotIn("window.confirm", preview)
        self.assertIn("$('#batch_preview_btn').on('click', previewBatch)", self.results)
        self.assertNotIn("$('#batch_approve_btn').on", self.results)

    def test_excluded_only_preview_is_visible_but_never_approvable(self) -> None:
        self.assertIn("_persist_exclusion_review", self.batch_module)
        self.assertIn("ModelBatchExclusion", self.batch_module)
        self.assertIn('status="completed_with_warnings"', self.batch_module)
        self.assertIn('"executable": False', self.batch_module)
        self.assertIn('payload["excluded_groups"]', self.batch_module)
        self.assertIn(
            'session.pop("plex_dupefinder_ff_batch_preview", None)',
            self.scan_module,
        )
        self.assertIn(
            'if data.get("plan_id") and data.get("nonce")', self.scan_module
        )
        self.assertIn("previewExclusions.length", self.results)
        self.assertIn("canApprove = approvalState && hasNonce", self.results)

    def test_initial_and_approval_previews_use_shared_plex_context(self) -> None:
        preview = self.batch_module.split("def preview(self, run_id", 1)[1].split(
            "\n    def _validate_plan_unchanged", 1
        )[0]
        validation = self.batch_module.split(
            "def _validate_plan_unchanged", 1
        )[1].split("\n    def approve", 1)[0]
        self.assertIn("self._preview_pairs(pairs)", preview)
        self.assertIn("self._preview_pairs(expected_pairs)", validation)
        self.assertNotIn("self.delete_service.preview(", validation)

    def test_safe_result_rows_offer_individual_delete_entry(self) -> None:
        self.assertIn("group.safe_to_delete && group.resolution_status === 'open'", self.results)
        self.assertIn("class=\"btn btn-sm btn-outline-danger group-delete\"", self.results)
        self.assertIn("openGroup(Number($(this).data('id')), true)", self.results)

    def test_run_and_group_rows_show_delete_diagnostics(self) -> None:
        self.assertIn("PDFF.deleteBudget(run)", self.scan_list)
        self.assertIn("PDFF.esc(budget.attempted)", self.scan_list)
        self.assertIn("한도 없음", self.scan_list)
        self.assertIn("run.successful_deletions", self.scan_list)
        self.assertIn("PDFF.badge(group.resolution_status || 'open')", self.results)

    def test_terminal_scan_rows_offer_fail_closed_database_delete(self) -> None:
        self.assertIn(
            "var scanDeleteStatuses = ['completed', 'completed_with_warnings', 'cancelled', 'failed', 'interrupted']",
            self.scan_list,
        )
        self.assertIn("function canDeleteScan(run)", self.scan_list)
        self.assertIn("if (!current && canDeleteScan(run))", self.scan_list)
        self.assertIn('class="btn btn-sm btn-danger force-delete-scan"', self.scan_list)
        self.assertIn("isScanDeletePending(run.id)", self.scan_list)
        self.assertIn("deletePending ? ' disabled'", self.scan_list)
        self.assertNotRegex(
            self.scan_list,
            r"scanDeleteStatuses\s*=\s*\[[^\]]*['\"](?:queued|running|cancelling)['\"]",
        )

    def test_scan_database_force_delete_is_explicit_post_csrf_and_reload(self) -> None:
        self.assertNotIn('class="btn btn-sm btn-outline-danger delete-scan"', self.scan_list)
        handler = self.scan_list.split(
            "$(document).on('click', '.force-delete-scan'", 1
        )[1].split("$('#refresh_runs_btn')", 1)[0]
        self.assertIn("window.confirm", handler)
        self.assertIn("recovery_required", handler)
        self.assertIn("미디어·자막 파일과 Plex에는 아무 명령도 보내지 않으며", handler)
        self.assertIn("'delete_scan'", handler)
        self.assertIn("force: '1'", handler)
        self.assertIn("confirmation: 'FORCE DELETE SCAN ' + String(runId)", handler)
        self.assertIn("csrf_token: csrfToken", handler)
        self.assertIn("'POST'", handler)
        self.assertIn("scanDeletePending[String(runId)] = true", handler)
        self.assertIn("delete scanDeletePending[String(runId)]", handler)
        self.assertIn("button.closest('.pdff-row').remove()", handler)
        self.assertIn("loadRuns()", handler)
        self.assertIn("loadStatus()", handler)

    def test_delete_attempt_counter_is_visible_but_never_disables_mutations(self) -> None:
        static = (ROOT / "static" / "pdff.js").read_text(encoding="utf-8")
        self.assertIn('id="delete_budget_status"', self.results)
        self.assertNotIn('id="delete_budget_warning"', self.results)
        self.assertIn("function deleteBudget(value)", static)
        self.assertIn("value.deletion_attempts", static)
        self.assertIn("unlimited: true", static)
        self.assertIn("limit: null", static)
        self.assertIn("remaining: null", static)
        self.assertIn("exhausted: false", static)
        self.assertIn("function selectedDeleteBudget()", self.results)
        self.assertNotIn("budget.exhausted", self.results)
        self.assertNotIn("selectedDeleteBudget().exhausted", self.results)
        self.assertNotIn("삭제 시도 한도 소진", self.results)
        self.assertIn("refreshSelectedRunBudget", self.results)
        render_batch = self.results.split("function renderBatchPlan(data)", 1)[1].split(
            "\nfunction ", 1
        )[0]
        self.assertIn("data.delete_budget", render_batch)
        self.assertIn("applyDeleteBudget(data.delete_budget)", render_batch)
        self.assertIn("한도</strong> 없음", self.results)

    def test_per_scan_and_batch_item_limits_are_removed(self) -> None:
        self.assertNotIn("setting_max_delete_per_run", self.setting)
        self.assertNotIn("setting_batch_max_items", self.setting)
        self.assertIn("항목 수 상한은 없습니다", self.setting)

    def test_direct_backend_requires_exact_review_and_hides_quarantine_root(self) -> None:
        self.assertIn("['direct', 'Plex Media DELETE + 외부 자막 정리']", self.setting)
        self.assertIn("deleteBackends = ['plex', 'quarantine', 'direct']", self.setting)
        self.assertIn("$('#quarantine_root_setting').toggle(backend === 'quarantine')", self.setting)
        self.assertIn("deleteBackend === 'direct'", self.setting)
        self.assertIn("영상은 PMS의 Media DELETE로 삭제", self.setting)
        self.assertIn("보호본을 위한 FlaskFarm data 쓰기 권한", self.setting)
        self.assertNotIn("confirmation !== expected", self.results)
        self.assertIn("cleanup.backend === 'direct'", self.results)

    def test_legacy_direct_batch_preview_requires_a_fresh_plan(self) -> None:
        self.assertIn('id="batch_legacy_direct_warning"', self.results)
        self.assertIn("confirmation.indexOf('BATCH DELETE MEDIA ') === 0", self.results)
        self.assertIn("legacyDirectPlan = confirmation.indexOf('BATCH DELETE FILES ') === 0", self.results)
        self.assertIn("!missingSubtitlePlan && !legacyDirectPlan", self.results)
        self.assertIn("다시 자동 정리를 시작하세요", self.results)

    def test_latest_plan_is_restored_by_run_id(self) -> None:
        self.assertIn("function restoreBatchForRun()", self.results)
        self.assertIn("{run_id: runId}", self.results)
        self.assertIn("approvalState && !hasNonce", self.results)
        self.assertIn("canApprove = approvalState && hasNonce", self.results)

    def test_backend_terminal_and_failure_statuses_are_rendered(self) -> None:
        static = (ROOT / "static" / "pdff.js").read_text(encoding="utf-8")
        for status in ("stopped", "interrupted", "expired"):
            self.assertIn("'%s'" % status, self.results)
            self.assertIn("'%s'" % status, static)
        for status in ("unknown", "verification_failed", "critical"):
            self.assertIn("'%s'" % status, static)
        self.assertIn("succeeded + failed + skipped", self.results)


if __name__ == "__main__":
    unittest.main()
