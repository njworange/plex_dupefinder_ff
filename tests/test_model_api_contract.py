from __future__ import annotations

import json
import sys
import types
import unittest
from unittest import mock

from test_flaskfarm_compat import FlaskFarmImportHarness, PACKAGE_NAME


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

            with mock.patch.dict(
                harness.setup_module.P.ModelSetting._data,
                {"setting_max_delete_per_run": "2"},
                clear=True,
            ):
                payload = item.as_api()
            self.assertIsInstance(payload, dict)
            self.assertEqual(payload["id"], 7)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["section_ids"], ["1"])
            self.assertEqual(
                payload["delete_budget"],
                {
                    "unlimited": True,
                    "attempted": 0,
                    "limit": None,
                    "remaining": None,
                    "exhausted": False,
                },
            )

    def test_group_detail_api_returns_the_same_live_delete_budget(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = sys.modules[PACKAGE_NAME + ".mod_scan"]
            run = types.SimpleNamespace(id=7, deletion_attempts=1)
            group = types.SimpleNamespace(
                id=3,
                run_id=7,
                as_api=lambda: {"id": 3, "run_id": 7},
            )
            candidate = types.SimpleNamespace(as_api=lambda: {"id": 4})

            class Groups:
                @classmethod
                def get(cls, group_id):
                    return group if int(group_id) == group.id else None

            class Candidates:
                @classmethod
                def by_group(cls, group_id, include_deleted=True):
                    return [candidate]

            class Runs:
                @classmethod
                def get(cls, run_id):
                    return run if int(run_id) == run.id else None

            module.ModelDuplicateGroup = Groups
            module.ModelMediaCandidate = Candidates
            module.ModelScanRun = Runs
            request = types.SimpleNamespace(values={"group_id": "3"})
            with mock.patch.dict(
                harness.setup_module.P.ModelSetting._data,
                {
                    "setting_delete_enabled": "True",
                    "setting_max_delete_per_run": "2",
                },
                clear=True,
            ):
                response = harness.setup_module.P.module_list[1].process_ajax(
                    "group_detail", request
                )

            self.assertEqual(response["ret"], "success")
            self.assertEqual(
                response["data"]["delete_budget"],
                {
                    "unlimited": True,
                    "attempted": 1,
                    "limit": None,
                    "remaining": None,
                    "exhausted": False,
                },
            )

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

    def test_quarantine_serializer_hides_internal_snapshots_and_secret_like_fields(self) -> None:
        with FlaskFarmImportHarness() as harness:
            model = harness.setup_module.P.ModelQuarantineJournal
            item = model()
            item.id = 9
            item.created_at = None
            item.updated_at = None
            item.finished_at = None
            item.action_log_id = 1
            item.batch_run_id = None
            item.run_id = 2
            item.group_id = 3
            item.candidate_id = 4
            item.keep_candidate_id = 5
            item.operation_key = "public-operation-id"
            item.status = "quarantined_pending_scan"
            item.plan_digest = "a" * 64
            item.operation_path = "/quarantine/<img src=x onerror=alert(1)>"
            item.eligible_count = 1
            item.excluded_count = 1
            item.protected_count = 1
            item.quarantined_count = 1
            item.last_error = ""
            item.moved_json = json.dumps(
                [
                    {
                        "source_path": "/media/<script>alert(1)</script>.ko.srt",
                        "destination_path": "/quarantine/item.ko.srt",
                        "kind": "subtitle",
                        "inode": 999,
                        "sha256": "hidden-moved-hash",
                    }
                ]
            )
            item.manifest_json = json.dumps(
                {
                    "eligible": [
                        {
                            "path": "/media/<script>alert(1)</script>.ko.srt",
                            "reason": "exclusive_to_deleted_video",
                            "snapshot": {
                                "inode": 123,
                                "device": 456,
                                "sha256": "private-file-hash",
                                "plex_token": "must-never-escape",
                            },
                        }
                    ],
                    "excluded": [
                        {
                            "path": "/media/keep.ko.srt",
                            "reason": "survivor_owned",
                            "snapshot": {"inode": 789},
                        }
                    ],
                }
            )

            detail = item.cleanup_api(include_paths=True)
            summary = item.cleanup_api(include_paths=False)
            serialized = json.dumps(detail, ensure_ascii=False).lower()

            self.assertEqual(detail["counts"]["quarantined"], 1)
            self.assertEqual(
                detail["eligible"][0]["path"],
                "/media/<script>alert(1)</script>.ko.srt",
            )
            self.assertEqual(
                detail["eligible"][0]["quarantine_path"],
                "/quarantine/item.ko.srt",
            )
            self.assertEqual(summary["eligible"], [])
            self.assertEqual(summary["excluded"], [])
            for forbidden in (
                "snapshot",
                "inode",
                "device",
                "sha256",
                "plex_token",
                "must-never-escape",
                "private-file-hash",
                "hidden-moved-hash",
            ):
                self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
