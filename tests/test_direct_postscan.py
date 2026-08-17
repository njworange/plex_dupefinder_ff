from __future__ import annotations

import json
import sys
import types
import unittest

from services.domain import MediaPart, MediaVersion, MetadataItem, PlexIdentity
from test_flaskfarm_compat import FlaskFarmImportHarness


def _version(media_id: str, path: str) -> MediaVersion:
    return MediaVersion(
        media_id=media_id,
        duration=7_200_000,
        bitrate=2_000,
        width=1920,
        height=1080,
        video_resolution="1080",
        video_codec="h264",
        audio_codec="aac",
        audio_channels=2,
        container="mkv",
        parts=(
            MediaPart(
                part_id=media_id + "-part",
                file=path,
                size=1_000,
                duration=7_200_000,
                container="mkv",
                exists=True,
            ),
        ),
    )


def _item(rating_key: str, guid: str, title: str, *versions: MediaVersion) -> MetadataItem:
    return MetadataItem(
        rating_key=rating_key,
        guid=guid,
        media_type="movie",
        title=title,
        year=2024,
        media=tuple(versions),
    )


class _Record:
    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class DirectDeletePostScanSafetyTest(unittest.TestCase):
    def test_coalesced_direct_actions_are_each_verified_without_pms_delete(self) -> None:
        delete_one = _version("10", "/media/one-delete.mkv")
        keep_one = _version("20", "/media/one-keep.mkv")
        delete_two = _version("30", "/media/two-delete.mkv")
        keep_two = _version("40", "/media/two-keep.mkv")
        before = {
            1: _item("100", "plex://movie/one", "One", delete_one, keep_one),
            2: _item("200", "plex://movie/two", "Two", delete_two, keep_two),
        }
        after = {
            "100": _item("100", "plex://movie/one", "One", keep_one),
            "200": _item("200", "plex://movie/two", "Two", keep_two),
        }
        actions = {
            action_id: _Record(
                id=action_id,
                status="deleted_pending_scan",
                message="",
                before_json=json.dumps(item.as_dict()),
                after_json="{}",
            )
            for action_id, item in before.items()
        }
        journals = {
            1: _Record(
                id=11,
                action_log_id=1,
                batch_run_id=None,
                run_id=31,
                group_id=41,
                candidate_id=51,
                status="deleted_pending_scan",
                last_error="",
                updated_at=None,
                finished_at=None,
            ),
            2: _Record(
                id=12,
                action_log_id=2,
                batch_run_id=None,
                run_id=32,
                group_id=42,
                candidate_id=52,
                status="deleted_pending_scan",
                last_error="",
                updated_at=None,
                finished_at=None,
            ),
        }
        groups = {
            41: _Record(
                id=41,
                rating_key="100",
                safe_to_delete=False,
                resolution_status="delete_in_progress",
                safety_flags_json="[]",
            ),
            42: _Record(
                id=42,
                rating_key="200",
                safe_to_delete=False,
                resolution_status="delete_in_progress",
                safety_flags_json="[]",
            ),
        }
        candidates = {
            51: _Record(id=51, media_id="10", deleted=False, deleted_at=None),
            52: _Record(id=52, media_id="30", deleted=False, deleted_at=None),
        }
        runs = {
            31: _Record(id=31, successful_deletions=0),
            32: _Record(id=32, successful_deletions=0),
        }
        verified = []
        cleaned = []
        metadata_reads = []
        delete_calls = []
        heartbeats = []

        class Gateway:
            def __init__(self, connection, timeout=None):
                self.connection = connection

            def validate_identity(self, machine_id, require_match=True):
                return PlexIdentity(machine_id="machine-1", version="1.0")

            def get_metadata(self, rating_key):
                metadata_reads.append(str(rating_key))
                return after[str(rating_key)]

            def delete_media(self, *args, **kwargs):
                delete_calls.append((args, kwargs))
                raise AssertionError("direct post-scan must never call PMS DELETE")

        class Provider:
            def resolve(self, require_machine_id=True):
                return types.SimpleNamespace(machine_id="machine-1")

        with FlaskFarmImportHarness() as harness:
            module = sys.modules["plex_dupefinder_ff.delete_service"]
            module.F.db.session = _Session()
            module.PlexMateProvider = Provider
            module.PlexGateway = Gateway
            module.ModelActionLog = types.SimpleNamespace(
                get=lambda action_id: actions.get(int(action_id))
            )
            module.ModelDirectDeleteJournal = types.SimpleNamespace(
                for_action=lambda action_id: journals.get(int(action_id))
            )
            module.ModelQuarantineJournal = types.SimpleNamespace(
                for_action=lambda action_id: None
            )
            module.ModelDuplicateGroup = types.SimpleNamespace(
                get=lambda group_id: groups.get(int(group_id))
            )
            module.ModelMediaCandidate = types.SimpleNamespace(
                get=lambda candidate_id: candidates.get(int(candidate_id))
            )
            module.ModelScanRun = types.SimpleNamespace(
                get=lambda run_id: runs.get(int(run_id))
            )
            service = module.DeleteService()
            def verify_deleted(journal, heartbeat=None):
                if callable(heartbeat):
                    heartbeat()
                verified.append(int(journal.id))
                return {"verified": 2, "videos": 1, "restored": 0}

            def cleanup_backups(journal, heartbeat=None):
                # Cleanup must run only after final success is durable.
                cleaned.append((int(journal.id), str(journal.status)))
                return {"removed": 2}

            service.direct_delete_manager = types.SimpleNamespace(
                verify_deleted=verify_deleted,
                cleanup_backups=cleanup_backups,
            )
            job = _Record(
                action_ids_json="[1, 2]",
                action_log_id=1,
                group_id=41,
                server_machine_id="machine-1",
                _pdff_heartbeat=lambda: heartbeats.append(True),
            )

            service.finalize_quarantine_scan(job)

            self.assertEqual(verified, [11, 12])
            self.assertEqual(cleaned, [(11, "verified"), (12, "verified")])
            self.assertEqual(metadata_reads, ["100", "200"])
            self.assertEqual(delete_calls, [])
            self.assertGreaterEqual(len(heartbeats), 6)
            self.assertEqual({journal.status for journal in journals.values()}, {"verified"})
            self.assertEqual({action.status for action in actions.values()}, {"success"})
            self.assertTrue(all(candidate.deleted for candidate in candidates.values()))
            self.assertEqual([runs[31].successful_deletions, runs[32].successful_deletions], [1, 1])
            self.assertTrue(
                all(group.resolution_status == "rescan_required" for group in groups.values())
            )

    def test_mixed_quarantine_and_direct_coalescing_fails_closed(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = sys.modules["plex_dupefinder_ff.delete_service"]
            module.ModelDirectDeleteJournal = types.SimpleNamespace(
                for_action=lambda action_id: _Record(id=1)
                if int(action_id) == 1
                else None
            )
            module.ModelQuarantineJournal = types.SimpleNamespace(
                for_action=lambda action_id: _Record(id=2)
                if int(action_id) == 2
                else None
            )
            service = module.DeleteService()
            job = _Record(
                action_ids_json="[1, 2]",
                action_log_id=1,
                group_id=1,
                server_machine_id="machine-1",
            )
            blocked = sys.modules[
                "plex_dupefinder_ff.post_delete_scan"
            ].PostDeleteScanBlocked

            with self.assertRaisesRegex(blocked, "서로 다른 파일 처리 방식"):
                service.finalize_quarantine_scan(job)

    def test_mixed_failure_marks_both_backend_journals_manual_check(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = sys.modules["plex_dupefinder_ff.delete_service"]
            module.F.db.session = _Session()
            actions = {
                1: _Record(id=1, status="deleted_pending_scan", message=""),
                2: _Record(id=2, status="quarantined_pending_scan", message=""),
            }
            direct = _Record(
                action_log_id=1,
                batch_run_id=None,
                group_id=11,
                status="deleted_pending_scan",
                last_error="",
                updated_at=None,
            )
            quarantine = _Record(
                action_log_id=2,
                batch_run_id=None,
                group_id=12,
                status="quarantined_pending_scan",
                last_error="",
                updated_at=None,
            )
            groups = {
                11: _Record(
                    safe_to_delete=False,
                    resolution_status="delete_in_progress",
                    safety_flags_json="[]",
                ),
                12: _Record(
                    safe_to_delete=False,
                    resolution_status="delete_in_progress",
                    safety_flags_json="[]",
                ),
            }
            module.ModelActionLog = types.SimpleNamespace(
                get=lambda action_id: actions.get(int(action_id))
            )
            module.ModelDirectDeleteJournal = types.SimpleNamespace(
                for_action=lambda action_id: direct if int(action_id) == 1 else None
            )
            module.ModelQuarantineJournal = types.SimpleNamespace(
                for_action=lambda action_id: quarantine
                if int(action_id) == 2
                else None
            )
            module.ModelDuplicateGroup = types.SimpleNamespace(
                get=lambda group_id: groups.get(int(group_id))
            )
            service = module.DeleteService()
            service._batch_item_for_journal = lambda journal: None
            service._sync_batch_after_scan = lambda batch_id: None
            job = _Record(
                action_ids_json="[1, 2]",
                action_log_id=1,
                group_id=11,
                server_machine_id="machine-1",
            )

            service.fail_quarantine_scan(job, "failed", "부분 스캔 최종 실패")

            self.assertEqual(direct.status, "recovery_required")
            self.assertEqual(quarantine.status, "recovery_required")
            self.assertEqual({value.status for value in actions.values()}, {"unknown"})
            self.assertTrue(
                all(
                    value.resolution_status == "manual_check_required"
                    for value in groups.values()
                )
            )
            self.assertIn("direct_delete_scan_failed", groups[11].safety_flags_json)
            self.assertIn("quarantine_scan_failed", groups[12].safety_flags_json)


if __name__ == "__main__":
    unittest.main()
