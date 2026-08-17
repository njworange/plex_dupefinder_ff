from __future__ import annotations

import json
import sys
import types
import unittest

from services.domain import (
    LibrarySection,
    MediaPart,
    MediaVersion,
    MetadataItem,
    PlexIdentity,
)
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
    def test_bulk_preview_reuses_plex_context_and_aborts_on_transport_error(self) -> None:
        keep_one = _version("10", "/media/one-keep.mkv")
        delete_one = _version("20", "/media/one-delete-a.mkv")
        delete_two = _version("30", "/media/one-delete-b.mkv")
        keep_two = _version("40", "/media/two-keep.mkv")
        delete_three = _version("50", "/media/two-delete.mkv")
        current = {
            "100": _item(
                "100",
                "plex://movie/one",
                "One",
                keep_one,
                delete_one,
                delete_two,
            ),
            "200": _item(
                "200", "plex://movie/two", "Two", keep_two, delete_three
            ),
        }
        run = _Record(
            id=31,
            status="completed",
            server_machine_id="machine-1",
            deletion_attempts=0,
        )
        groups = {
            1: _Record(
                id=1,
                run_id=31,
                rating_key="100",
                media_type="movie",
                section_key="15",
                safe_to_delete=True,
                resolution_status="open",
                identity_fingerprint="one",
            ),
            2: _Record(
                id=2,
                run_id=31,
                rating_key="200",
                media_type="movie",
                section_key="15",
                safe_to_delete=True,
                resolution_status="open",
                identity_fingerprint="two",
            ),
        }
        candidates = {
            10: _Record(
                id=10, group_id=1, media_id="10", fingerprint="keep-one", deleted=False
            ),
            11: _Record(
                id=11, group_id=1, media_id="20", fingerprint="delete-one", deleted=False
            ),
            12: _Record(
                id=12, group_id=1, media_id="30", fingerprint="delete-two", deleted=False
            ),
            20: _Record(
                id=20, group_id=2, media_id="40", fingerprint="keep-two", deleted=False
            ),
            21: _Record(
                id=21, group_id=2, media_id="50", fingerprint="delete-three", deleted=False
            ),
        }
        active = {
            1: [candidates[10], candidates[11], candidates[12]],
            2: [candidates[20], candidates[21]],
        }
        provider_calls = []
        identity_calls = []
        section_calls = []
        metadata_calls = []
        filesystem_calls = []

        class Gateway:
            fail = False
            missing_first = False

            def __init__(self, connection, timeout=None):
                pass

            def validate_identity(self, machine_id, require_match=True):
                identity_calls.append(machine_id)
                return PlexIdentity(machine_id="machine-1", version="1.0")

            def list_sections(self):
                section_calls.append(True)
                return [
                    LibrarySection(
                        key="15",
                        title="Movies",
                        section_type="movie",
                        locations=("/media",),
                    )
                ]

            def get_metadata(self, rating_key):
                metadata_calls.append(str(rating_key))
                if self.missing_first and str(rating_key) == "100":
                    raise module.PlexMetadataNotFound("metadata removed")
                if self.fail:
                    raise module.PlexGatewayError("transport failed")
                return current[str(rating_key)]

        class Provider:
            def resolve(self, require_machine_id=True):
                provider_calls.append(True)
                return types.SimpleNamespace(machine_id="machine-1")

        class Plan:
            def __init__(self, media_id):
                self.plan_digest = str(media_id).zfill(64)
                self.eligible = ()

            def public_dict(self):
                return {
                    "backend": "direct",
                    "executable": True,
                    "eligible": [],
                    "excluded": [],
                    "protected": [],
                    "counts": {"eligible": 0, "excluded": 0, "protected": 0},
                }

        with FlaskFarmImportHarness() as harness:
            module = sys.modules["plex_dupefinder_ff.delete_service"]
            harness.setup_module.P.ModelSetting._data.update(
                {
                    "setting_delete_enabled": "True",
                    "setting_delete_backend": "direct",
                }
            )
            module.PlexMateProvider = Provider
            module.PlexGateway = Gateway
            module.group_has_cross_path_conflict = lambda run_id, group_id: False
            module.require_delete_attempt_available = lambda value: {
                "unlimited": True,
                "attempted": 0,
            }
            module.current_safety_policy = lambda: types.SimpleNamespace(
                allowed_roots=("/media",)
            )
            module.validate_fresh_snapshot = lambda *args, **kwargs: types.SimpleNamespace(
                safe=True, flags=()
            )
            module.assess_group = lambda *args, **kwargs: types.SimpleNamespace(
                safe=True, flags=()
            )
            module._single_surviving_scan_target = lambda *args, **kwargs: "/media"
            module.ModelMediaCandidate = types.SimpleNamespace(
                by_group=lambda group_id, include_deleted=False: active[int(group_id)]
            )
            service = module.DeleteService()
            service._load = lambda group_id, candidate_id, keep_id: (
                run,
                groups[int(group_id)],
                candidates[int(candidate_id)],
                candidates[int(keep_id)],
            )

            def direct_preview(item, media_id, allowed_roots, section_locations):
                filesystem_calls.append(str(media_id))
                return Plan(media_id)

            service.direct_delete_manager = types.SimpleNamespace(
                preview=direct_preview
            )
            requests = ((1, 11, 10), (1, 12, 10), (2, 21, 20))
            previews, errors = service.preview_many(requests)

            self.assertEqual(errors, {})
            self.assertEqual(set(previews), set(requests))
            self.assertEqual(len(provider_calls), 1)
            self.assertEqual(len(identity_calls), 1)
            self.assertEqual(len(section_calls), 1)
            self.assertEqual(metadata_calls, ["100", "200"])
            self.assertEqual(filesystem_calls, ["20", "30", "50"])

            Gateway.missing_first = True
            metadata_calls[:] = []
            filesystem_calls[:] = []
            previews, errors = service.preview_many(requests)
            self.assertEqual(errors, {1: "metadata removed"})
            self.assertEqual(set(previews), {(2, 21, 20)})
            self.assertEqual(metadata_calls, ["100", "200"])
            self.assertEqual(filesystem_calls, ["50"])

            Gateway.missing_first = False
            Gateway.fail = True
            metadata_calls[:] = []
            with self.assertRaisesRegex(module.PlexGatewayError, "transport failed"):
                service.preview_many(requests)
            self.assertEqual(metadata_calls, ["100"])
            self.assertEqual(filesystem_calls, ["50"])

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
            def verify_deleted(
                journal, heartbeat=None, intentionally_deleted_paths=()
            ):
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

    def test_three_version_auto_group_uses_action_union_and_counts_once(self) -> None:
        keep = _version("10", "/media/keep.mkv")
        first_delete = _version("20", "/media/first.mkv")
        second_delete = _version("30", "/media/second.mkv")
        first_before = _item(
            "100", "plex://movie/three", "Three", keep, first_delete, second_delete
        )
        second_before = _item(
            "100", "plex://movie/three", "Three", keep, second_delete
        )
        current = _item("100", "plex://movie/three", "Three", keep)
        actions = {
            1: _Record(
                id=1,
                status="deleted_pending_scan",
                message="",
                before_json=json.dumps(first_before.as_dict()),
                after_json="{}",
            ),
            2: _Record(
                id=2,
                status="deleted_pending_scan",
                message="",
                before_json=json.dumps(second_before.as_dict()),
                after_json="{}",
            ),
        }
        journals = {
            1: _Record(
                id=11,
                action_log_id=1,
                batch_run_id=71,
                run_id=31,
                group_id=41,
                candidate_id=51,
                status="deleted_pending_scan",
                manifest_json=json.dumps(
                    {
                        "video": {"path": "/media/first.mkv"},
                        "eligible": [
                            {"snapshot": {"path": "/media/first.ko.srt"}}
                        ],
                    }
                ),
                last_error="",
                updated_at=None,
                finished_at=None,
            ),
            2: _Record(
                id=12,
                action_log_id=2,
                batch_run_id=71,
                run_id=31,
                group_id=41,
                candidate_id=52,
                status="deleted_pending_scan",
                manifest_json=json.dumps(
                    {
                        "video": {"path": "/media/second.mkv"},
                        "eligible": [
                            {"snapshot": {"path": "/media/second.ko.srt"}}
                        ],
                    }
                ),
                last_error="",
                updated_at=None,
                finished_at=None,
            ),
        }
        group = _Record(
            id=41,
            rating_key="100",
            safe_to_delete=False,
            resolution_status="rescan_required",
            safety_flags_json="[]",
        )
        candidates = {
            51: _Record(id=51, media_id="20", deleted=True, deleted_at=None),
            52: _Record(id=52, media_id="30", deleted=True, deleted_at=None),
        }
        run = _Record(id=31, successful_deletions=0)
        observed_paths = []

        class Gateway:
            def __init__(self, connection, timeout=None):
                pass

            def validate_identity(self, machine_id, require_match=True):
                return PlexIdentity(machine_id="machine-1", version="1.0")

            def get_metadata(self, rating_key):
                return current

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
                get=lambda group_id: group if int(group_id) == 41 else None
            )
            module.ModelMediaCandidate = types.SimpleNamespace(
                get=lambda candidate_id: candidates.get(int(candidate_id))
            )
            module.ModelScanRun = types.SimpleNamespace(
                get=lambda run_id: run if int(run_id) == 31 else None
            )
            service = module.DeleteService()
            service._batch_item_for_journal = lambda journal: None
            service._sync_batch_after_scan = lambda batch_id: None

            def verify_deleted(
                journal, heartbeat=None, intentionally_deleted_paths=()
            ):
                observed_paths.append(set(intentionally_deleted_paths))
                return {"verified": 1, "videos": 1, "restored": 0}

            service.direct_delete_manager = types.SimpleNamespace(
                verify_deleted=verify_deleted,
                cleanup_backups=lambda journal, heartbeat=None: {"removed": 1},
            )
            job = _Record(
                action_ids_json="[1, 2]",
                action_log_id=1,
                group_id=41,
                server_machine_id="machine-1",
            )
            service.finalize_direct_scan(job)
            # Re-entry sees terminal journals and must not increment again.
            service.finalize_direct_scan(job)

        expected_paths = {
            "/media/first.mkv",
            "/media/first.ko.srt",
            "/media/second.mkv",
            "/media/second.ko.srt",
        }
        self.assertEqual(observed_paths, [expected_paths, expected_paths])
        self.assertEqual({value.status for value in journals.values()}, {"verified"})
        self.assertEqual({value.status for value in actions.values()}, {"success"})
        self.assertEqual(run.successful_deletions, 2)

    def test_later_success_does_not_downgrade_earlier_critical_group(self) -> None:
        keep = _version("10", "/media/keep.mkv")
        first_delete = _version("20", "/media/first.mkv")
        second_delete = _version("30", "/media/second.mkv")
        actions = {
            1: _Record(
                id=1,
                group_id=41,
                status="deleted_pending_scan",
                message="",
                before_json=json.dumps(
                    _item(
                        "100",
                        "plex://movie/three",
                        "Three",
                        keep,
                        first_delete,
                        second_delete,
                    ).as_dict()
                ),
                after_json="{}",
            ),
            2: _Record(
                id=2,
                group_id=41,
                status="deleted_pending_scan",
                message="",
                before_json=json.dumps(
                    _item(
                        "100", "plex://movie/three", "Three", keep, second_delete
                    ).as_dict()
                ),
                after_json="{}",
            ),
        }
        journals = {
            1: _Record(
                id=11,
                action_log_id=1,
                batch_run_id=71,
                run_id=31,
                group_id=41,
                candidate_id=51,
                status="deleted_pending_scan",
                manifest_json=json.dumps({"video": {"path": "/media/first.mkv"}}),
                last_error="",
                updated_at=None,
                finished_at=None,
            ),
            2: _Record(
                id=12,
                action_log_id=2,
                batch_run_id=71,
                run_id=31,
                group_id=41,
                candidate_id=52,
                status="deleted_pending_scan",
                manifest_json=json.dumps({"video": {"path": "/media/second.mkv"}}),
                last_error="",
                updated_at=None,
                finished_at=None,
            ),
        }
        group = _Record(
            id=41,
            rating_key="100",
            safe_to_delete=False,
            resolution_status="delete_in_progress",
            safety_flags_json="[]",
        )
        candidates = {
            51: _Record(id=51, media_id="20", deleted=True, deleted_at=None),
            52: _Record(id=52, media_id="30", deleted=True, deleted_at=None),
        }
        run = _Record(id=31, successful_deletions=0)
        current = _item("100", "plex://movie/three", "Three", keep)

        class Gateway:
            def __init__(self, connection, timeout=None):
                pass

            def validate_identity(self, machine_id, require_match=True):
                return PlexIdentity(machine_id="machine-1", version="1.0")

            def get_metadata(self, rating_key):
                return current

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
                get=lambda group_id: group if int(group_id) == 41 else None
            )
            module.ModelMediaCandidate = types.SimpleNamespace(
                get=lambda candidate_id: candidates.get(int(candidate_id))
            )
            module.ModelScanRun = types.SimpleNamespace(
                get=lambda run_id: run if int(run_id) == 31 else None
            )
            service = module.DeleteService()
            service._batch_item_for_journal = lambda journal: None
            service._sync_batch_after_scan = lambda batch_id: None

            def verify_deleted(
                journal, heartbeat=None, intentionally_deleted_paths=()
            ):
                if int(journal.id) == 11:
                    raise RuntimeError("first action verification failed")
                return {"verified": 1, "videos": 1, "restored": 0}

            service.direct_delete_manager = types.SimpleNamespace(
                verify_deleted=verify_deleted,
                cleanup_backups=lambda journal, heartbeat=None: {"removed": 1},
            )
            job = _Record(
                action_ids_json="[1, 2]",
                action_log_id=1,
                group_id=41,
                server_machine_id="machine-1",
            )
            blocked = sys.modules[
                "plex_dupefinder_ff.post_delete_scan"
            ].PostDeleteScanBlocked

            with self.assertRaisesRegex(blocked, "수동 확인"):
                service.finalize_direct_scan(job)

        self.assertEqual(actions[1].status, "critical")
        self.assertEqual(actions[2].status, "success")
        self.assertEqual(journals[1].status, "critical")
        self.assertEqual(journals[2].status, "verified")
        self.assertEqual(group.resolution_status, "manual_check_required")
        self.assertIn("direct_delete_postscan_critical", group.safety_flags_json)
        self.assertEqual(run.successful_deletions, 1)

    def test_direct_missing_journal_locks_its_action_group(self) -> None:
        actions = {
            1: _Record(id=1, group_id=11, status="success", message=""),
            2: _Record(
                id=2, group_id=12, status="deleted_pending_scan", message=""
            ),
        }
        journal = _Record(
            id=21,
            action_log_id=1,
            batch_run_id=None,
            run_id=31,
            group_id=11,
            candidate_id=51,
            status="verified",
            manifest_json="{}",
            last_error="",
            updated_at=None,
        )
        groups = {
            11: _Record(
                id=11,
                safe_to_delete=False,
                resolution_status="rescan_required",
                safety_flags_json='["existing"]',
            ),
            12: _Record(
                id=12,
                safe_to_delete=True,
                resolution_status="open",
                safety_flags_json="[]",
            ),
        }

        class Gateway:
            def __init__(self, connection, timeout=None):
                pass

            def validate_identity(self, machine_id, require_match=True):
                return PlexIdentity(machine_id="machine-1", version="1.0")

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
                for_action=lambda action_id: journal if int(action_id) == 1 else None
            )
            module.ModelDuplicateGroup = types.SimpleNamespace(
                get=lambda group_id: groups.get(int(group_id))
            )
            module.ModelMediaCandidate = types.SimpleNamespace(get=lambda candidate_id: None)
            service = module.DeleteService()
            blocked = sys.modules[
                "plex_dupefinder_ff.post_delete_scan"
            ].PostDeleteScanBlocked
            job = _Record(
                action_ids_json="[1, 2]",
                action_log_id=1,
                group_id=11,
                server_machine_id="machine-1",
            )

            with self.assertRaisesRegex(blocked, "수동 확인"):
                service.finalize_direct_scan(job)

        self.assertEqual(groups[11].resolution_status, "rescan_required")
        self.assertEqual(groups[11].safety_flags_json, '["existing"]')
        self.assertEqual(groups[12].resolution_status, "manual_check_required")
        self.assertIn("direct_delete_postscan_record_missing", groups[12].safety_flags_json)

    def test_quarantine_missing_journal_locks_its_action_group(self) -> None:
        actions = {
            1: _Record(id=1, group_id=11, status="success", message=""),
            2: _Record(
                id=2, group_id=12, status="quarantined_pending_scan", message=""
            ),
        }
        journal = _Record(
            id=21,
            action_log_id=1,
            batch_run_id=None,
            run_id=31,
            group_id=11,
            candidate_id=51,
            status="verified",
            last_error="",
            updated_at=None,
        )
        groups = {
            11: _Record(
                id=11,
                safe_to_delete=False,
                resolution_status="rescan_required",
                safety_flags_json='["existing"]',
            ),
            12: _Record(
                id=12,
                safe_to_delete=True,
                resolution_status="open",
                safety_flags_json="[]",
            ),
        }

        class Gateway:
            def __init__(self, connection, timeout=None):
                pass

            def validate_identity(self, machine_id, require_match=True):
                return PlexIdentity(machine_id="machine-1", version="1.0")

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
                for_action=lambda action_id: None
            )
            module.ModelQuarantineJournal = types.SimpleNamespace(
                for_action=lambda action_id: journal if int(action_id) == 1 else None
            )
            module.ModelDuplicateGroup = types.SimpleNamespace(
                get=lambda group_id: groups.get(int(group_id))
            )
            service = module.DeleteService()
            blocked = sys.modules[
                "plex_dupefinder_ff.post_delete_scan"
            ].PostDeleteScanBlocked
            job = _Record(
                action_ids_json="[1, 2]",
                action_log_id=1,
                group_id=11,
                server_machine_id="machine-1",
            )

            with self.assertRaisesRegex(blocked, "수동 확인"):
                service.finalize_quarantine_scan(job)

        self.assertEqual(groups[11].resolution_status, "rescan_required")
        self.assertEqual(groups[11].safety_flags_json, '["existing"]')
        self.assertEqual(groups[12].resolution_status, "manual_check_required")
        self.assertIn("quarantine_postscan_record_missing", groups[12].safety_flags_json)

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
