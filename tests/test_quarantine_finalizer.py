from __future__ import annotations

import json
import sys
import types
import unittest
from dataclasses import replace
from unittest import mock

from services.domain import MediaPart, MediaVersion, MetadataItem

from test_flaskfarm_compat import FlaskFarmImportHarness, PACKAGE_NAME


class _Record:
    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)


class _Session:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _version(media_id: str, path: str, bitrate: int = 1000) -> MediaVersion:
    return MediaVersion(
        media_id=media_id,
        duration=7_200_000,
        bitrate=bitrate,
        width=1920,
        height=1080,
        video_resolution="1080",
        video_codec="h264",
        audio_codec="aac",
        audio_channels=2,
        container="mkv",
        parts=(
            MediaPart(
                media_id + "1",
                path,
                size=1000,
                duration=7_200_000,
                container="mkv",
                exists=True,
            ),
        ),
    )


def _item(*media: MediaVersion) -> MetadataItem:
    return MetadataItem(
        rating_key="100",
        guid="plex://movie/quarantine-finalizer",
        media_type="movie",
        title="Finalizer",
        year=2026,
        media=tuple(media),
    )


class QuarantineFinalizerTest(unittest.TestCase):
    def _run_finalizer(self, current: MetadataItem, protected=None):
        harness = FlaskFarmImportHarness()
        harness.__enter__()
        module = sys.modules[PACKAGE_NAME + ".delete_service"]
        post_module = sys.modules[PACKAGE_NAME + ".post_delete_scan"]
        before = _item(
            _version("10", "/media/movies/Finalizer/delete.mkv"),
            _version("20", "/media/movies/Finalizer/keep.mkv", bitrate=2000),
        )
        action = _Record(
            id=41,
            status="quarantined_pending_scan",
            message="",
            before_json=json.dumps(before.as_dict()),
            after_json="",
        )
        journal = _Record(
            id=42,
            action_log_id=41,
            batch_run_id=None,
            run_id=43,
            group_id=44,
            candidate_id=45,
            status="quarantined_pending_scan",
            last_error="",
            updated_at=None,
            finished_at=None,
        )
        group = _Record(
            id=44,
            rating_key="100",
            safe_to_delete=False,
            resolution_status="delete_in_progress",
            safety_flags_json="[]",
        )
        candidate = _Record(
            id=45,
            media_id="10",
            deleted=False,
            deleted_at=None,
        )
        run = _Record(id=43, successful_deletions=0)
        session = _Session()
        module.F.db.session = session

        module.ModelActionLog = types.SimpleNamespace(
            get=lambda action_id: action if int(action_id) == 41 else None
        )
        module.ModelQuarantineJournal = types.SimpleNamespace(
            for_action=lambda action_id: journal if int(action_id) == 41 else None
        )
        module.ModelDuplicateGroup = types.SimpleNamespace(
            get=lambda group_id: group if int(group_id) == 44 else None
        )
        module.ModelMediaCandidate = types.SimpleNamespace(
            get=lambda candidate_id: candidate if int(candidate_id) == 45 else None
        )
        module.ModelScanRun = types.SimpleNamespace(
            get=lambda run_id: run if int(run_id) == 43 else None
        )

        connection = types.SimpleNamespace(
            base_url="http://plex.local:32400",
            machine_id="machine-1",
            token="secret",
        )

        class Provider:
            def resolve(self, require_machine_id=False):
                return connection

        class Gateway:
            def __init__(self, *args, **kwargs):
                pass

            def validate_identity(self, machine_id, require_match=True):
                return types.SimpleNamespace(machine_id="machine-1")

            def get_metadata(self, rating_key):
                return current

        module.PlexMateProvider = Provider
        module.PlexGateway = Gateway
        service = object.__new__(module.DeleteService)
        service.quarantine_manager = types.SimpleNamespace(
            verify_or_restore_protected=(
                protected or (lambda value: {"verified": 1, "restored": 0})
            )
        )
        service._batch_item_for_journal = lambda value: None
        service._sync_batch_after_scan = lambda batch_id: None
        job = _Record(
            action_log_id=41,
            action_ids_json="[41]",
            batch_run_id=None,
            run_id=43,
            group_id=44,
            candidate_id=45,
            server_machine_id="machine-1",
        )
        return (
            harness,
            module,
            post_module,
            service,
            job,
            before,
            action,
            journal,
            group,
            candidate,
            run,
            session,
        )

    def test_verified_finalizer_marks_candidate_action_group_and_run(self) -> None:
        before = _item(
            _version("10", "/media/movies/Finalizer/delete.mkv"),
            _version("20", "/media/movies/Finalizer/keep.mkv", bitrate=2000),
        )
        values = self._run_finalizer(_item(before.media[1]))
        harness, _module, _post, service, job = values[:5]
        action, journal, group, candidate, run, session = values[6:12]
        try:
            service.finalize_quarantine_scan(job)
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(journal.status, "verified")
        self.assertEqual(action.status, "success")
        self.assertTrue(candidate.deleted)
        self.assertIsNotNone(candidate.deleted_at)
        self.assertEqual(run.successful_deletions, 1)
        self.assertEqual(group.resolution_status, "rescan_required")
        self.assertIn("rescan_required_after_quarantine", group.safety_flags_json)
        self.assertGreaterEqual(session.commits, 2)

    def test_trash_pending_is_success_but_remains_explicit_in_audit(self) -> None:
        before = _item(
            _version("10", "/media/movies/Finalizer/delete.mkv"),
            _version("20", "/media/movies/Finalizer/keep.mkv", bitrate=2000),
        )
        missing = replace(
            before.media[0],
            parts=tuple(replace(part, exists=False) for part in before.media[0].parts),
        )
        values = self._run_finalizer(_item(missing, before.media[1]))
        harness, _module, _post, service, job = values[:5]
        action, journal, group, candidate = values[6:10]
        try:
            service.finalize_quarantine_scan(job)
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(journal.status, "trash_pending")
        self.assertEqual(action.status, "success")
        self.assertTrue(candidate.deleted)
        self.assertIn("휴지통", action.message)
        self.assertIn("plex_trash_pending_after_quarantine", group.safety_flags_json)

    def test_survivor_drift_is_critical_and_never_marks_candidate_deleted(self) -> None:
        changed = _item(
            _version("20", "/media/movies/Finalizer/keep.mkv", bitrate=2001)
        )
        values = self._run_finalizer(changed)
        harness, _module, post_module, service, job = values[:5]
        action, journal, group, candidate, run = values[6:11]
        try:
            with self.assertRaises(post_module.PostDeleteScanBlocked):
                service.finalize_quarantine_scan(job)
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(journal.status, "critical")
        self.assertEqual(action.status, "critical")
        self.assertFalse(candidate.deleted)
        self.assertEqual(run.successful_deletions, 0)
        self.assertEqual(group.resolution_status, "manual_check_required")
        self.assertIn("quarantine_postscan_critical", group.safety_flags_json)

    def test_restored_protected_subtitle_requires_another_scan_before_success(self) -> None:
        before = _item(
            _version("10", "/media/movies/Finalizer/delete.mkv"),
            _version("20", "/media/movies/Finalizer/keep.mkv", bitrate=2000),
        )
        calls = []

        def protected(_journal):
            calls.append(True)
            return (
                {"verified": 0, "restored": 1}
                if len(calls) == 1
                else {"verified": 1, "restored": 0}
            )

        values = self._run_finalizer(_item(before.media[1]), protected=protected)
        harness, _module, post_module, service, job = values[:5]
        action, journal, group, candidate, run = values[6:11]
        try:
            with self.assertRaises(post_module.PostDeleteScanRefreshRequired):
                service.finalize_quarantine_scan(job)
            self.assertFalse(candidate.deleted)
            self.assertEqual(run.successful_deletions, 0)
            self.assertEqual(journal.status, "scan_running")
            self.assertIn("복구", action.message)

            service.finalize_quarantine_scan(job)
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(candidate.deleted)
        self.assertEqual(run.successful_deletions, 1)
        self.assertEqual(journal.status, "verified")
        self.assertEqual(group.resolution_status, "rescan_required")

    def test_batch_sync_waits_for_every_coalesced_quarantine_item(self) -> None:
        with FlaskFarmImportHarness():
            module = sys.modules[PACKAGE_NAME + ".delete_service"]
            models = sys.modules[PACKAGE_NAME + ".models"]
            batch = _Record(
                id=71,
                succeeded_items=0,
                failed_items=0,
                skipped_items=0,
                processed_items=0,
                status="scan_pending",
                current_message="",
                finished_at=None,
            )
            items = [
                _Record(status="success"),
                _Record(status="scan_pending"),
            ]
            batches = types.SimpleNamespace(get=lambda batch_id: batch)
            batch_items = types.SimpleNamespace(by_batch=lambda batch_id: items)
            with mock.patch.object(models, "ModelBatchRun", batches), mock.patch.object(
                models, "ModelBatchItem", batch_items
            ):
                module.DeleteService._sync_batch_after_scan(71)
                self.assertEqual(batch.status, "scan_pending")
                self.assertIsNone(batch.finished_at)
                self.assertEqual(batch.succeeded_items, 1)
                self.assertEqual(batch.processed_items, 1)

                items[1].status = "success"
                module.DeleteService._sync_batch_after_scan(71)
                self.assertEqual(batch.status, "completed")
                self.assertIsNotNone(batch.finished_at)
                self.assertEqual(batch.succeeded_items, 2)
                self.assertEqual(batch.processed_items, 2)

    def test_missing_coalesced_action_never_returns_success(self) -> None:
        before = _item(
            _version("10", "/media/movies/Finalizer/delete.mkv"),
            _version("20", "/media/movies/Finalizer/keep.mkv", bitrate=2000),
        )
        values = self._run_finalizer(_item(before.media[1]))
        harness, module, post_module, service, job = values[:5]
        candidate, run = values[9:11]
        job.action_log_id = 999
        job.action_ids_json = "[999]"
        module.ModelActionLog = types.SimpleNamespace(get=lambda action_id: None)
        try:
            with self.assertRaises(post_module.PostDeleteScanBlocked):
                service.finalize_quarantine_scan(job)
        finally:
            harness.__exit__(None, None, None)

        self.assertFalse(candidate.deleted)
        self.assertEqual(run.successful_deletions, 0)

    def test_missing_coalesced_journal_never_returns_success(self) -> None:
        before = _item(
            _version("10", "/media/movies/Finalizer/delete.mkv"),
            _version("20", "/media/movies/Finalizer/keep.mkv", bitrate=2000),
        )
        values = self._run_finalizer(_item(before.media[1]))
        harness, module, post_module, service, job = values[:5]
        candidate, run = values[9:11]
        module.ModelQuarantineJournal = types.SimpleNamespace(
            for_action=lambda action_id: None
        )
        try:
            with self.assertRaises(post_module.PostDeleteScanBlocked):
                service.finalize_quarantine_scan(job)
        finally:
            harness.__exit__(None, None, None)

        self.assertFalse(candidate.deleted)
        self.assertEqual(run.successful_deletions, 0)


if __name__ == "__main__":
    unittest.main()
