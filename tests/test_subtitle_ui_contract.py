from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SubtitleUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.setting_module = (ROOT / "mod_setting.py").read_text(encoding="utf-8")
        cls.setting = (
            ROOT / "templates" / "plex_dupefinder_ff_setting_setting.html"
        ).read_text(encoding="utf-8")
        cls.results = (
            ROOT / "templates" / "plex_dupefinder_ff_scan_results.html"
        ).read_text(encoding="utf-8")
        cls.history = (
            ROOT / "templates" / "plex_dupefinder_ff_history_list.html"
        ).read_text(encoding="utf-8")
        cls.static = (ROOT / "static" / "pdff.js").read_text(encoding="utf-8")
        cls.history_module = (ROOT / "mod_history.py").read_text(encoding="utf-8")
        cls.models = (ROOT / "models.py").read_text(encoding="utf-8")

    def test_quarantine_subtitle_cleanup_is_opt_in(self) -> None:
        self.assertIn('"setting_delete_backend": "plex"', self.setting_module)
        self.assertIn('"setting_quarantine_root": ""', self.setting_module)
        self.assertIn("setting_delete_backend", self.setting)
        self.assertIn("['plex', 'Plex Media DELETE (기존 방식)']", self.setting)
        self.assertIn("['quarantine', '안전 격리 (영상 + 전용 외부 자막)']", self.setting)
        self.assertIn("['direct', 'Plex Media DELETE + 외부 자막 정리']", self.setting)

    def test_quarantine_save_requires_root_and_partial_scan(self) -> None:
        self.assertIn("deleteBackend === 'quarantine' && !quarantineRoot", self.setting)
        self.assertIn("deleteBackend === 'quarantine' || deleteBackend === 'direct'", self.setting)
        self.assertIn("deleteBackend === 'plex' && wasDeleteBackend === 'quarantine'", self.setting)
        self.assertIn("PMS DB 반영과 유지본 사후 검증", self.setting)
        self.assertIn("격리는 영구삭제가 아닙니다", self.setting)

    def test_manual_preview_requires_inline_review_before_execute(self) -> None:
        for element_id in (
            "delete_preview_panel",
            "delete_subtitle_preview",
            "delete_confirmation_phrase",
            "delete_confirmation_input",
            "delete_execute_btn",
            "delete_result_panel",
        ):
            self.assertIn('id="%s"' % element_id, self.results)
        self.assertIn("PDFF.subtitleCleanupHtml(preview, 'preview')", self.results)
        self.assertIn("PDFF.subtitleCleanupHtml(result, 'result')", self.results)
        self.assertIn("confirmation !== expected", self.results)
        self.assertIn("plan_digest: cleanup.planDigest", self.results)
        self.assertNotIn("window.prompt", self.results)

    def test_individual_delete_has_no_second_popup_but_keeps_exact_handshake(self) -> None:
        execute_delete = self.results.split("function executeDelete()", 1)[1].split(
            "\nfunction ", 1
        )[0]
        self.assertNotIn("window.confirm", execute_delete)
        self.assertIn("confirmation !== expected", execute_delete)
        self.assertIn("nonce: pendingDeletePreview.nonce", execute_delete)
        self.assertIn("confirmation: confirmation", execute_delete)
        self.assertIn("plan_digest: cleanup.planDigest", execute_delete)
        self.assertIn("Object.assign({}, pendingDeletePayload", execute_delete)

        preview_delete = self.results.split("function previewDelete()", 1)[1].split(
            "\nfunction ", 1
        )[0]
        self.assertIn("csrf_token: csrfToken", preview_delete)
        self.assertIn("pendingDeletePayload = payload", preview_delete)

        approve_batch = self.results.split("function approveBatch()", 1)[1].split(
            "\nfunction ", 1
        )[0]
        self.assertIn("window.confirm", approve_batch)

    def test_subtitle_paths_and_reasons_are_html_escaped(self) -> None:
        self.assertIn("esc(subtitlePath(entry, true))", self.static)
        self.assertIn("esc(subtitlePath(entry, false))", self.static)
        self.assertIn("cleanup.protected.forEach", self.static)
        self.assertIn("protected_subtitles", self.static)
        self.assertIn("esc(destination)", self.static)
        self.assertIn("esc(subtitleReason(entry", self.static)
        self.assertIn("위험·모호하여 제외", self.static)
        self.assertIn("영구삭제가 아니라 격리 이동", self.static)
        self.assertIn("영상 삭제는 PMS에 한 번만 요청", self.static)
        self.assertIn("기타·차단 검토 대상", self.static)
        self.assertIn("PMS 처리 뒤 삭제 대상 전용 자막만", self.static)

    def test_direct_failure_diagnostics_preserve_message_and_escape_rendering(self) -> None:
        self.assertIn("(backup|protection)", self.static)
        self.assertIn("message: String(cleanup.message || value.message || '')", self.static)
        self.assertIn("operationId: String(cleanup.operation_id || value.operation_id || '')", self.static)
        self.assertIn("recoveryDiagnostics: recoveryDiagnostics", self.static)
        self.assertIn("cleanup.recovery_diagnostics !== undefined", self.static)
        self.assertIn("esc(cleanup.message)", self.static)
        self.assertIn("esc(cleanup.operationId)", self.static)
        self.assertIn("esc(label)", self.static)
        self.assertNotIn("+ cleanup.message +", self.static)
        self.assertNotIn("+ cleanup.operationId +", self.static)
        for state, label in (
            ("source_only", "원본 존재"),
            ("tombstone_only", "임시파일 존재·수동 복구 필요"),
            ("both_absent", "둘 다 없음·삭제 여부 수동 확인"),
            ("conflict", "충돌"),
            ("both_present", "충돌"),
            ("unreadable", "경로 상태 확인 불가·수동 확인"),
        ):
            self.assertIn("%s: '%s'" % (state, label), self.static)
        self.assertIn("PDFF.subtitleCleanupHtml(detail, 'result')", self.history)

    def test_direct_protected_paths_are_public_but_blockers_are_fail_closed(self) -> None:
        self.assertIn("protectedDetailsPresent", self.static)
        self.assertIn("blockingCount", self.static)
        self.assertIn("required_backup_unavailable:", self.static)
        self.assertIn("PMS DELETE 전 SHA-256 보호·복원 대상", self.static)
        self.assertIn("cleanup.protected.length !== cleanup.protectedCount", self.results)
        self.assertIn("!cleanup.executable || cleanup.blockingCount > 0", self.results)
        self.assertIn('id="batch_direct_blocked_warning"', self.results)
        self.assertIn("!blockedDirectPlan", self.results)

    def test_batch_and_history_expose_subtitle_exceptions(self) -> None:
        self.assertIn("PDFF.subtitleCleanupHtml(item", self.results)
        self.assertIn("renderBatchItems(items, filesystemPlan)", self.results)
        self.assertIn(": (filesystemPlan", self.results)
        self.assertIn("confirmation.indexOf('BATCH QUARANTINE ') === 0", self.results)
        self.assertIn("confirmation.indexOf('BATCH DELETE MEDIA ') === 0", self.results)
        self.assertIn("legacyDirectPlan = confirmation.indexOf('BATCH DELETE FILES ') === 0", self.results)
        self.assertIn("Plex Media DELETE 방식은 외부 자막을 선별하지 않으며", self.results)
        self.assertIn('id="subtitle_filter"', self.history)
        self.assertIn('<option value="excluded">', self.history)
        self.assertIn('<option value="quarantined">', self.history)
        self.assertIn('<option value="deleted">', self.history)
        self.assertIn("subtitle_filter: $('#subtitle_filter').val()", self.history)
        self.assertIn("PDFF.subtitleCleanupHtml(detail, 'result')", self.history)
        self.assertIn("자막 보호·예외 확인", self.history)

    def test_history_filter_is_enforced_by_the_backend_query(self) -> None:
        self.assertIn('subtitle_filter not in ("", "excluded", "quarantined", "deleted")', self.history_module)
        self.assertIn("subtitle_filter=subtitle_filter", self.history_module)
        self.assertIn('subtitle_filter == "excluded"', self.models)
        self.assertIn('subtitle_filter == "quarantined"', self.models)
        self.assertIn("ModelQuarantineJournal.excluded_count > 0", self.models)
        self.assertIn("ModelQuarantineJournal.quarantined_count > 0", self.models)
        self.assertIn('subtitle_filter == "deleted"', self.models)
        self.assertIn("ModelDirectDeleteJournal.deleted_count > 0", self.models)


if __name__ == "__main__":
    unittest.main()
