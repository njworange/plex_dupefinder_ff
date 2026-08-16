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

    def test_library_select_buttons_start_disabled_and_update_scan_state(self) -> None:
        for element_id in ("select_all_libraries_btn", "clear_all_libraries_btn"):
            tag = re.search(r'<button[^>]+id="%s"[^>]*>' % element_id, self.scan_list)
            self.assertIsNotNone(tag)
            self.assertIn("disabled", tag.group(0))
        self.assertIn("function updateLibraryButtons()", self.scan_list)
        self.assertIn("$('.pdff-section').prop('checked', true)", self.scan_list)
        self.assertIn("$('.pdff-section').prop('checked', false)", self.scan_list)
        self.assertIn("selected === 0", self.scan_list)

    def test_batch_settings_are_opt_in_and_bounded(self) -> None:
        self.assertIn("setting_batch_delete_enabled", self.setting)
        self.assertIn("setting_batch_max_items", self.setting)
        self.assertRegex(self.setting, r"setting_batch_max_items[^\n]+min=1, max=100")
        self.assertIn("batchEnabled && !deleteEnabled", self.setting)

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

    def test_batch_requires_exact_confirmation_and_escapes_plan_fields(self) -> None:
        self.assertIn("confirmation !== expected", self.results)
        self.assertIn("batch_confirmation_phrase').text(confirmation)", self.results)
        for expression in ("title", "keep.mediaId", "del.mediaId", "message"):
            self.assertIn("PDFF.esc(%s)" % expression, self.results)
        self.assertIn("PDFF.esc(keep.paths.join", self.results)
        self.assertIn("PDFF.esc(del.paths.join", self.results)

    def test_safe_result_rows_offer_individual_delete_entry(self) -> None:
        self.assertIn("group.safe_to_delete && group.resolution_status === 'open'", self.results)
        self.assertIn("class=\"btn btn-sm btn-outline-danger group-delete\"", self.results)
        self.assertIn("openGroup(Number($(this).data('id')), true)", self.results)

    def test_run_and_group_rows_show_delete_diagnostics(self) -> None:
        self.assertIn("run.deletion_attempts", self.scan_list)
        self.assertIn("run.successful_deletions", self.scan_list)
        self.assertIn("PDFF.badge(group.resolution_status || 'open')", self.results)

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
