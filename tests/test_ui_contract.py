from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"


class MinimalUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.setting = (
            TEMPLATES / "plex_dupefinder_ff_setting_setting.html"
        ).read_text(encoding="utf-8")
        cls.cleanup = (
            TEMPLATES / "plex_dupefinder_ff_cleanup_run.html"
        ).read_text(encoding="utf-8")
        cls.history = (
            TEMPLATES / "plex_dupefinder_ff_history_list.html"
        ).read_text(encoding="utf-8")
        cls.javascript = (STATIC / "pdff.js").read_text(encoding="utf-8")
        cls.stylesheet = (STATIC / "pdff.css").read_text(encoding="utf-8")

    def test_setting_page_exposes_v21_defaults_and_library_picker(self) -> None:
        for setting_id in (
            "setting_library_id",
            "setting_score_json",
            "setting_filename_score",
            "setting_size_score",
            "setting_subtitle_extensions",
            "setting_subs_search",
            "setting_timeout",
            "setting_scheduler_mode",
            "setting_scheduler_interval",
        ):
            self.assertIn("'%s'" % setting_id, self.setting)
        self.assertIn("globalSettingSaveBtn", self.setting)
        self.assertIn("['off', '사용 안 함']", self.setting)
        self.assertIn("['dry_run', 'Dry Run']", self.setting)
        self.assertIn("['live', '즉시 자동 정리']", self.setting)
        self.assertIn(".smi,.sup", self.setting)
        self.assertIn("Subs 및 Subtitles", self.setting)
        self.assertIn("setting_input_text_and_buttons", self.setting)
        self.assertIn("'load_libraries_btn', '라이브러리 조회'", self.setting)
        self.assertIn('id="library_lookup_result"', self.setting)
        self.assertIn("globalSendCommand('libraries'", self.setting)
        self.assertIn("toggleLibraryId", self.setting)
        score_field = self.setting.split("'setting_score_json'", 1)[1].split(
            ") }}", 1
        )[0]
        self.assertIn("value=arg['setting_score_json']", score_field)
        self.assertIn("row='28'", score_field)
        filename_field = self.setting.split("'setting_filename_score'", 1)[1].split(
            ") }}", 1
        )[0]
        self.assertIn("value=arg['setting_filename_score']", filename_field)
        self.assertIn("row='20'", filename_field)
        size_field = self.setting.split("'setting_size_score'", 1)[1].split(
            ") }}", 1
        )[0]
        self.assertIn("value=arg.get('setting_size_score', 'True')", size_field)

    def test_cleanup_commands_match_backend_contract(self) -> None:
        for command in ("dry_run", "start_live", "stop", "status"):
            self.assertIn("'%s'" % command, self.cleanup)
        self.assertIn("Dry Run 시작", self.cleanup)
        self.assertIn("즉시 자동 정리 시작", self.cleanup)
        self.assertIn('id="recent_actions"', self.cleanup)
        self.assertIn("PDFF.renderActions", self.cleanup)

    def test_cleanup_page_shows_status_and_summary(self) -> None:
        for element_id in (
            "run_status",
            "run_mode",
            "run_started_at",
            "run_current",
            "run_message",
            "run_progress_text",
            "summary_groups",
            "summary_deleted",
            "summary_bytes",
            "summary_would_delete",
            "summary_would_delete_bytes",
            "summary_partial",
            "summary_errors",
        ):
            self.assertIn('id="%s"' % element_id, self.cleanup)
        self.assertIn("window.setInterval(refreshStatus, 2000)", self.cleanup)
        self.assertIn("stopping: '중지 요청됨'", self.cleanup)
        self.assertIn("stopped: '중지됨'", self.cleanup)
        self.assertIn("data.running && data.stop_requested", self.cleanup)
        self.assertIn("!data.running && data.stop_requested", self.cleanup)
        self.assertIn("PDFF.bytes(summary.bytes)", self.cleanup)
        self.assertIn("PDFF.bytes(summary.would_delete_bytes)", self.cleanup)
        self.assertIn("hasOwnProperty.call(responseData, 'running')", self.cleanup)

    def test_history_uses_history_command_and_renders_runs_and_actions(self) -> None:
        self.assertIn("globalSendCommand('history'", self.history)
        self.assertIn("PDFF.renderRuns('history_runs', data.runs)", self.history)
        self.assertIn("PDFF.renderActions('history_actions', data.items)", self.history)
        self.assertIn("action.file_size !== undefined", self.javascript)
        self.assertIn("bytes(action.file_size)", self.javascript)

    def test_untrusted_output_is_rendered_as_text(self) -> None:
        self.assertIn("node.textContent = valueText(value)", self.javascript)
        self.assertIn("target.textContent", self.javascript)
        self.assertNotIn(".innerHTML", self.javascript)
        self.assertNotIn("insertAdjacentHTML", self.javascript)
        for template in (self.setting, self.cleanup, self.history):
            self.assertIn("arg['package_name']|tojson", template)
        self.assertNotIn(".innerHTML", self.setting)

    def test_removed_workflows_do_not_reappear(self) -> None:
        combined = "\n".join((self.setting, self.cleanup, self.history, self.javascript)).lower()
        for forbidden in (
            "window.confirm",
            "confirm(",
            "approval",
            "quarantine",
            "batch",
            "승인",
            "격리",
        ):
            self.assertNotIn(forbidden, combined)

    def test_static_assets_do_not_reference_plex_credentials(self) -> None:
        combined = self.javascript + "\n" + self.stylesheet
        self.assertNotIn("X-Plex-Token", combined)
        self.assertNotIn("base_token", combined)

    def test_jinja_delimiters_are_balanced(self) -> None:
        for path in TEMPLATES.glob("*.html"):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("{{"), text.count("}}"), path.name)
            self.assertEqual(text.count("{%"), text.count("%}"), path.name)


if __name__ == "__main__":
    unittest.main()
