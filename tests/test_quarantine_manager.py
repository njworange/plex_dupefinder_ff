from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from services.quarantine_delete import build_quarantine_plan

from test_flaskfarm_compat import FlaskFarmImportHarness


class _Record:
    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)


class _Journal(_Record):
    pass


class _Session:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, value) -> None:
        if getattr(value, "id", None) is None:
            value.id = len(self.added) + 1
        self.added.append(value)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class QuarantineManagerFilesystemTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.quarantine = self.root / "quarantine"
        self.folder = self.media / "Movie"
        self.media.mkdir()
        self.quarantine.mkdir()
        self.delete_video = _write(self.folder / "Film.1080p.mkv", b"delete-video")
        self.keep_video = _write(self.folder / "Film.2160p.mkv", b"keep-video")
        self.delete_subtitle = _write(self.folder / "Film.1080p.ko.srt", b"delete-subtitle")
        self.keep_subtitle = _write(self.folder / "Film.2160p.ko.srt", b"keep-subtitle")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_plan(self):
        return build_quarantine_plan(
            (str(self.delete_video),),
            (str(self.keep_video),),
            (str(self.media),),
            (str(self.media),),
            str(self.quarantine),
        )

    def manager_context(self):
        harness = FlaskFarmImportHarness()
        harness.__enter__()
        module = sys.modules["plex_dupefinder_ff.quarantine_manager"]
        session = _Session()
        module.F.db.session = session
        module.ModelQuarantineJournal = _Journal
        return harness, module, session, module.QuarantineManager()

    @staticmethod
    def records():
        return {
            "run": _Record(id=1),
            "group": _Record(
                id=2,
                safe_to_delete=False,
                resolution_status="delete_in_progress",
                safety_flags_json="[]",
            ),
            "candidate": _Record(id=3),
            "keep": _Record(id=4),
            "action_log": _Record(id=5, status="validating", message=""),
        }

    def test_success_moves_only_video_and_exclusive_subtitle_and_backs_up_keep_subtitle(self) -> None:
        plan = self.make_plan()
        harness, _module, session, manager = self.manager_context()
        try:
            journal = manager.stage(plan, plan.plan_digest, **self.records())
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(journal.status, "quarantined_pending_scan")
        self.assertFalse(self.delete_video.exists())
        self.assertFalse(self.delete_subtitle.exists())
        self.assertTrue(self.keep_video.exists())
        self.assertTrue(self.keep_subtitle.exists())
        moved = json.loads(journal.moved_json)
        self.assertEqual({item["kind"] for item in moved}, {"video", "subtitle"})
        self.assertTrue(all(Path(item["destination_path"]).exists() for item in moved))
        backups = json.loads(journal.backups_json)
        self.assertTrue(backups)
        self.assertTrue(all(Path(item["backup_path"]).exists() for item in backups))
        self.assertGreaterEqual(session.commits, 5)

    def test_digest_mismatch_is_read_only(self) -> None:
        plan = self.make_plan()
        harness, _module, session, manager = self.manager_context()
        try:
            with self.assertRaises(Exception):
                manager.stage(plan, "0" * 64, **self.records())
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertEqual(session.added, [])

    def test_stat_drift_before_stage_moves_nothing(self) -> None:
        plan = self.make_plan()
        self.delete_subtitle.write_bytes(b"changed-after-preview")
        harness, _module, session, manager = self.manager_context()
        try:
            with self.assertRaises(Exception):
                manager.stage(plan, plan.plan_digest, **self.records())
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertEqual(session.added, [])

    def test_new_same_stem_sibling_video_after_preview_moves_nothing(self) -> None:
        plan = self.make_plan()
        sibling = _write(self.folder / "Film.1080p.mp4", b"new-sibling")
        harness, _module, session, manager = self.manager_context()
        try:
            with self.assertRaisesRegex(Exception, "폴더 내용"):
                manager.stage(plan, plan.plan_digest, **self.records())
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(sibling.exists())
        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertEqual(session.added, [])

    def test_recreated_quarantine_root_after_preview_moves_nothing(self) -> None:
        plan = self.make_plan()
        Path(plan.quarantine_marker.path).unlink()
        self.quarantine.rmdir()
        self.quarantine.mkdir()
        harness, _module, session, manager = self.manager_context()
        try:
            with self.assertRaisesRegex(Exception, "격리 루트"):
                manager.stage(plan, plan.plan_digest, **self.records())
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertEqual(session.added, [])

    def test_recreated_watched_subtitle_directory_moves_nothing(self) -> None:
        subtitles = self.folder / "Subs"
        subtitles.mkdir()
        plan = self.make_plan()
        watched = next(
            value
            for value in plan.watched_directories
            if Path(value.path) == subtitles
        )
        subtitles.rmdir()
        subtitles.mkdir()
        # Make the timestamp distinction deterministic even on filesystems
        # that immediately reuse the same directory inode.
        os.utime(
            str(subtitles),
            ns=(watched.mtime_ns + 2_000_000_000, watched.mtime_ns + 2_000_000_000),
        )
        harness, _module, session, manager = self.manager_context()
        try:
            with self.assertRaisesRegex(Exception, "폴더 내용"):
                manager.stage(plan, plan.plan_digest, **self.records())
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertEqual(session.added, [])

    def test_plan_manifest_binds_marker_and_watched_directory_times(self) -> None:
        plan = self.make_plan()
        manifest = plan.manifest_dict()
        self.assertNotIn("quarantine_mtime_ns", manifest)
        self.assertNotIn("quarantine_ctime_ns", manifest)
        self.assertEqual(
            manifest["quarantine_marker"], plan.quarantine_marker.as_dict()
        )
        self.assertTrue(manifest["quarantine_marker"]["sha256"])
        self.assertTrue(manifest["watched_directories"])
        self.assertTrue(
            all(
                "mtime_ns" in value and "ctime_ns" in value
                for value in manifest["watched_directories"]
            )
        )

    def test_marker_tamper_before_stage_moves_nothing(self) -> None:
        plan = self.make_plan()
        Path(plan.quarantine_marker.path).write_bytes(b"x" * 32)
        harness, _module, session, manager = self.manager_context()
        try:
            with self.assertRaisesRegex(Exception, "격리 루트"):
                manager.stage(plan, plan.plan_digest, **self.records())
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertEqual(session.added, [])

    def test_symlink_marker_is_rejected_by_preview(self) -> None:
        target = _write(self.root / "marker-target", b"x" * 32)
        marker = self.quarantine / ".plex_dupefinder_ff-root-id"
        try:
            os.symlink(str(target), str(marker))
        except (NotImplementedError, OSError):
            self.skipTest("symlinks are unavailable")
        with self.assertRaisesRegex(Exception, "identity marker"):
            self.make_plan()

    def test_marker_hardlink_before_stage_moves_nothing(self) -> None:
        plan = self.make_plan()
        try:
            os.link(plan.quarantine_marker.path, str(self.quarantine / "marker-copy"))
        except (NotImplementedError, OSError):
            self.skipTest("hard links are unavailable")
        with self.assertRaisesRegex(Exception, "identity marker"):
            self.make_plan()
        harness, _module, session, manager = self.manager_context()
        try:
            with self.assertRaisesRegex(Exception, "격리 루트"):
                manager.stage(plan, plan.plan_digest, **self.records())
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertEqual(session.added, [])

    def test_concurrent_first_plans_share_one_complete_marker(self) -> None:
        workers = 8
        barrier = threading.Barrier(workers)

        def build():
            barrier.wait(timeout=5)
            return self.make_plan()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            plans = list(executor.map(lambda _value: build(), range(workers)))

        marker_paths = {plan.quarantine_marker.path for plan in plans}
        marker_hashes = {plan.quarantine_marker.sha256 for plan in plans}
        marker_inodes = {plan.quarantine_marker.inode for plan in plans}
        plan_digests = {plan.plan_digest for plan in plans}
        self.assertEqual(len(marker_paths), 1)
        self.assertEqual(len(marker_hashes), 1)
        self.assertEqual(len(marker_inodes), 1)
        self.assertEqual(len(plan_digests), 1)
        self.assertEqual(Path(next(iter(marker_paths))).stat().st_size, 32)

    def test_first_operation_does_not_change_second_plan_digest(self) -> None:
        second_folder = self.media / "Movie2"
        second_delete = _write(second_folder / "Other.1080p.mkv", b"delete-two")
        second_keep = _write(second_folder / "Other.2160p.mkv", b"keep-two")
        _write(second_folder / "Other.1080p.ko.srt", b"subtitle-two")
        first_plan = self.make_plan()
        second_plan = build_quarantine_plan(
            (str(second_delete),),
            (str(second_keep),),
            (str(self.media),),
            (str(self.media),),
            str(self.quarantine),
        )
        harness, _module, _session, manager = self.manager_context()
        try:
            manager.stage(first_plan, first_plan.plan_digest, **self.records())
        finally:
            harness.__exit__(None, None, None)

        fresh_second = build_quarantine_plan(
            (str(second_delete),),
            (str(second_keep),),
            (str(self.media),),
            (str(self.media),),
            str(self.quarantine),
        )
        self.assertEqual(second_plan.plan_digest, fresh_second.plan_digest)

    def test_heartbeat_loss_before_stage_moves_nothing(self) -> None:
        plan = self.make_plan()
        harness, _module, _session, manager = self.manager_context()
        heartbeat = mock.Mock(side_effect=RuntimeError("lease lost"))
        try:
            with self.assertRaises(Exception):
                manager.stage(
                    plan,
                    plan.plan_digest,
                    heartbeat=heartbeat,
                    **self.records(),
                )
        finally:
            harness.__exit__(None, None, None)

        heartbeat.assert_called()
        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())

    def test_quarantined_video_mtime_drift_is_not_finalized(self) -> None:
        plan = self.make_plan()
        harness, _module, _session, manager = self.manager_context()
        try:
            journal = manager.stage(plan, plan.plan_digest, **self.records())
            moved = json.loads(journal.moved_json)
            video = next(item for item in moved if item["kind"] == "video")
            destination = Path(video["destination_path"])
            before = destination.stat()
            os.utime(
                str(destination),
                ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000),
            )

            with self.assertRaisesRegex(Exception, "identity"):
                manager.verify_quarantined(journal)
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(destination.exists())

    def test_partial_rename_is_durably_marked_recovery_required_without_rollback(self) -> None:
        plan = self.make_plan()
        harness, module, session, manager = self.manager_context()
        records = self.records()
        real_replace = os.replace
        calls = []
        planned_sources = {plan.video.path} | {
            decision.path for decision in plan.eligible
        }

        def fail_second(source, destination):
            if str(source) not in planned_sources:
                return real_replace(source, destination)
            calls.append((source, destination))
            if len(calls) == 2:
                raise OSError("injected second rename failure")
            return real_replace(source, destination)

        try:
            with mock.patch.object(module.os, "replace", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "완결되지"):
                    manager.stage(plan, plan.plan_digest, **records)
            journal = session.added[0]
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(journal.status, "recovery_required")
        moved = json.loads(journal.moved_json)
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["kind"], "video")
        self.assertFalse(Path(moved[0]["source_path"]).exists())
        self.assertTrue(Path(moved[0]["destination_path"]).exists())
        self.assertFalse(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertFalse(records["group"].safe_to_delete)
        self.assertEqual(
            records["group"].resolution_status,
            "manual_check_required",
        )
        self.assertIn(
            "quarantine_recovery_required",
            json.loads(records["group"].safety_flags_json),
        )
        self.assertIn("injected second rename failure", journal.last_error)
        self.assertGreaterEqual(session.commits, 1)

    def test_restart_marks_incomplete_filesystem_transaction_for_manual_recovery(self) -> None:
        harness, module, session, manager = self.manager_context()
        journal = _Record(
            id=11,
            status="quarantining",
            action_log_id=12,
            group_id=13,
            last_error="",
            updated_at=None,
        )
        action = _Record(id=12, status="quarantining", message="")
        group = _Record(
            id=13,
            safe_to_delete=False,
            resolution_status="delete_in_progress",
            safety_flags_json="[]",
        )

        module.ModelQuarantineJournal = types.SimpleNamespace(
            unfinished=lambda: [journal]
        )
        module.ModelActionLog = types.SimpleNamespace(
            get=lambda action_id: action if int(action_id) == 12 else None
        )
        module.ModelDuplicateGroup = types.SimpleNamespace(
            get=lambda group_id: group if int(group_id) == 13 else None
        )
        module.ModelPostDeleteScanJob = types.SimpleNamespace(
            active_for_action=lambda action_id: None
        )
        try:
            count = manager.recover_interrupted()
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(count, 1)
        self.assertEqual(journal.status, "recovery_required")
        self.assertEqual(action.status, "unknown")
        self.assertEqual(group.resolution_status, "manual_check_required")
        self.assertIn(
            "quarantine_recovery_required",
            json.loads(group.safety_flags_json),
        )
        self.assertGreaterEqual(session.commits, 1)

    def test_restart_preserves_completed_quarantine_owned_by_durable_scan_job(self) -> None:
        harness, module, session, manager = self.manager_context()
        journal = _Record(
            id=21,
            status="quarantined_pending_scan",
            action_log_id=22,
            group_id=23,
            last_error="",
            updated_at=None,
        )
        module.ModelQuarantineJournal = types.SimpleNamespace(
            unfinished=lambda: [journal]
        )
        module.ModelPostDeleteScanJob = types.SimpleNamespace(
            active_for_action=lambda action_id: _Record(id=24, status="queued")
        )
        module.ModelActionLog = types.SimpleNamespace(
            get=lambda action_id: (_ for _ in ()).throw(
                AssertionError("active scan-owned journal must not be rewritten")
            )
        )
        module.ModelDuplicateGroup = types.SimpleNamespace(
            get=lambda group_id: (_ for _ in ()).throw(
                AssertionError("active scan-owned group must not be relocked")
            )
        )
        try:
            count = manager.recover_interrupted()
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(count, 0)
        self.assertEqual(journal.status, "quarantined_pending_scan")
        self.assertEqual(session.commits, 0)

    def test_protected_subtitle_change_is_never_overwritten_from_backup(self) -> None:
        plan = self.make_plan()
        harness, _module, _session, manager = self.manager_context()
        try:
            journal = manager.stage(plan, plan.plan_digest, **self.records())
            changed = b"user-changed-keep-subtitle"
            self.keep_subtitle.write_bytes(changed)

            with self.assertRaisesRegex(Exception, "덮어쓰지"):
                manager.verify_or_restore_protected(journal)
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(self.keep_subtitle.read_bytes(), changed)

    def test_missing_protected_subtitle_is_hash_restored_and_next_verify_succeeds(self) -> None:
        plan = self.make_plan()
        harness, _module, _session, manager = self.manager_context()
        try:
            journal = manager.stage(plan, plan.plan_digest, **self.records())
            approved_manifest = journal.manifest_json
            self.keep_subtitle.unlink()

            first = manager.verify_or_restore_protected(journal)
            second = manager.verify_or_restore_protected(journal)
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(first, {"verified": 0, "restored": 1})
        self.assertEqual(second, {"verified": 1, "restored": 0})
        self.assertEqual(self.keep_subtitle.read_bytes(), b"keep-subtitle")
        self.assertEqual(journal.manifest_json, approved_manifest)
        restored = json.loads(journal.backups_json)[0].get("restored_snapshot")
        self.assertIsInstance(restored, dict)
        self.assertEqual(restored.get("path"), str(self.keep_subtitle))

    def test_tampered_protection_backup_never_publishes_a_restore(self) -> None:
        plan = self.make_plan()
        harness, _module, _session, manager = self.manager_context()
        try:
            journal = manager.stage(plan, plan.plan_digest, **self.records())
            backup = json.loads(journal.backups_json)[0]
            Path(backup["backup_path"]).write_bytes(b"tampered-backup")
            self.keep_subtitle.unlink()

            with self.assertRaisesRegex(Exception, "해시|identity"):
                manager.verify_or_restore_protected(journal)
        finally:
            harness.__exit__(None, None, None)

        self.assertFalse(self.keep_subtitle.exists())


if __name__ == "__main__":
    unittest.main()
