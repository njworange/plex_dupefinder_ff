from __future__ import annotations

import unittest

from test_flaskfarm_compat import FlaskFarmImportHarness


class ModelApiContractTest(unittest.TestCase):
    def test_scan_run_serializer_returns_the_status_payload(self) -> None:
        with FlaskFarmImportHarness() as harness:
            model = harness.setup_module.P.ModelScanRun
            item = model()
            item.id = 7
            item.created_at = None
            item.started_at = None
            item.finished_at = None
            item.status = "completed"
            item.progress = 100
            item.status_message = "done"
            item.section_ids_json = '["1"]'
            item.server_machine_id = "machine-1"
            item.server_version = "1.40"
            item.total_sections = 1
            item.completed_sections = 1
            item.total_groups = 2
            item.safe_groups = 1
            item.unsafe_groups = 1
            item.successful_deletions = 0
            item.deletion_attempts = 0
            item.cancellation_requested = False
            item.error_summary = ""

            payload = item.as_api()
            self.assertIsInstance(payload, dict)
            self.assertEqual(payload["id"], 7)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["section_ids"], ["1"])

    def test_action_log_serializer_honors_summary_and_detail_contract(self) -> None:
        with FlaskFarmImportHarness() as harness:
            model = harness.setup_module.P.ModelActionLog
            item = model()
            item.id = 1
            item.created_at = None
            item.run_id = 2
            item.group_id = 3
            item.candidate_id = 4
            item.keep_candidate_id = 5
            item.action = "delete_media"
            item.status = "success"
            item.message = "ok"
            item.response_status = 200
            item.before_json = '{"media":["10","20"]}'
            item.after_json = '{"media":["20"]}'

            summary = item.as_api(include_snapshots=False)
            detail = item.as_api(include_snapshots=True)
            self.assertNotIn("before", summary)
            self.assertNotIn("after", summary)
            self.assertEqual(detail["before"], {"media": ["10", "20"]})
            self.assertEqual(detail["after"], {"media": ["20"]})


if __name__ == "__main__":
    unittest.main()
