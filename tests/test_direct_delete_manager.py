from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from services.direct_delete import build_direct_delete_plan
from test_flaskfarm_compat import FlaskFarmImportHarness


def _write(path: Path, value: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


class _Record:
    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)


class _Journal(_Record):
    values = {}

    def __init__(self, **values):
        super().__init__(**values)
        self.id = getattr(self, "id", None)
        self.finished_at = getattr(self, "finished_at", None)
        self.last_error = getattr(self, "last_error", "")

    @classmethod
    def get(cls, journal_id):
        return cls.values.get(int(journal_id))


class _Session:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, value) -> None:
        if getattr(value, "id", None) is None:
            value.id = len(self.added) + 1
        self.added.append(value)
        _Journal.values[int(value.id)] = value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class DirectDeleteManagerFilesystemSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pdff-direct-manager-")
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.folder = self.media / "Movie"
        self.delete_video = _write(
            self.folder / "Film.1080p.mkv", b"delete-video"
        )
        self.keep_video = _write(self.folder / "Film.2160p.mkv", b"keep-video")
        self.delete_subtitle = _write(
            self.folder / "Film.1080p.ko.srt", b"delete-subtitle"
        )
        self.keep_subtitle = _write(
            self.folder / "Film.2160p.ko.srt", b"keep-subtitle"
        )
        _Journal.values = {}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_plan(self):
        return build_direct_delete_plan(
            (str(self.delete_video),),
            (str(self.keep_video),),
            (str(self.media),),
            (str(self.media),),
            "web",
        )

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

    def manager_context(self, records=None):
        records = records or self.records()
        harness = FlaskFarmImportHarness()
        harness.__enter__()
        module = sys.modules["plex_dupefinder_ff.direct_delete_manager"]
        session = _Session()
        module.F.db.session = session
        module.ModelDirectDeleteJournal = _Journal
        module.ModelActionLog = types.SimpleNamespace(
            get=lambda action_id: records["action_log"]
            if int(action_id) == records["action_log"].id
            else None
        )
        module.ModelDuplicateGroup = types.SimpleNamespace(
            get=lambda group_id: records["group"]
            if int(group_id) == records["group"].id
            else None
        )
        return harness, module, session, module.DirectDeleteManager(), records

    def test_success_deletes_video_and_only_exclusive_subtitle_after_durable_journal(self) -> None:
        plan = self.make_plan()
        harness, module, session, manager, records = self.manager_context()
        real_rename = os.rename
        commits_at_first_mutation = []
        fsynced = []

        def observed_rename(source, destination):
            commits_at_first_mutation.append(session.commits)
            return real_rename(source, destination)

        try:
            with mock.patch.object(module.os, "rename", side_effect=observed_rename), mock.patch.object(
                module, "_fsync_directory", side_effect=lambda path: fsynced.append(path)
            ):
                journal = manager.execute(
                    plan, plan.plan_digest, **records
                )
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(commits_at_first_mutation)
        self.assertGreaterEqual(commits_at_first_mutation[0], 2)
        self.assertEqual(journal.status, "deleted_pending_scan")
        self.assertFalse(self.delete_video.exists())
        self.assertFalse(self.delete_subtitle.exists())
        self.assertTrue(self.keep_video.exists())
        self.assertEqual(self.keep_subtitle.read_bytes(), b"keep-subtitle")
        self.assertFalse(any(self.folder.glob(".pdff-direct-*")))
        self.assertIn(str(self.folder), {str(Path(value)) for value in fsynced})
        self.assertEqual(records["action_log"].status, "deleted_pending_scan")
        operations = json.loads(journal.unlink_json)
        self.assertEqual({value["state"] for value in operations}, {"deleted"})
        self.assertEqual({value["kind"] for value in operations}, {"video", "subtitle"})

    def test_digest_mismatch_is_read_only_and_creates_no_journal(self) -> None:
        plan = self.make_plan()
        harness, _module, session, manager, records = self.manager_context()
        try:
            with self.assertRaises(Exception):
                manager.execute(plan, "0" * 64, **records)
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(session.added, [])
        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertTrue(self.keep_video.exists())
        self.assertTrue(self.keep_subtitle.exists())

    def test_journal_commit_failure_occurs_before_any_filesystem_mutation(self) -> None:
        plan = self.make_plan()
        harness, module, session, manager, records = self.manager_context()
        session.commit = mock.Mock(side_effect=RuntimeError("injected db failure"))
        try:
            with mock.patch.object(
                module.os,
                "rename",
                side_effect=AssertionError("rename must follow durable journal commit"),
            ) as rename:
                with self.assertRaisesRegex(RuntimeError, "작업 기록"):
                    manager.execute(plan, plan.plan_digest, **records)
            rename.assert_not_called()
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertTrue(self.keep_video.exists())
        self.assertTrue(self.keep_subtitle.exists())

    def test_directory_drift_before_execute_is_read_only(self) -> None:
        plan = self.make_plan()
        unrelated = _write(self.folder / "arrived-after-preview.txt", b"new")
        harness, _module, session, manager, records = self.manager_context()
        try:
            with self.assertRaisesRegex(Exception, "폴더 내용"):
                manager.execute(plan, plan.plan_digest, **records)
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(session.added, [])
        self.assertTrue(unrelated.exists())
        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertTrue(self.keep_subtitle.exists())

    def test_protected_subtitle_drift_before_execute_is_read_only(self) -> None:
        plan = self.make_plan()
        self.keep_subtitle.write_bytes(b"changed-keep-subtitle")
        harness, _module, session, manager, records = self.manager_context()
        try:
            with self.assertRaisesRegex(Exception, "유지본 자막"):
                manager.execute(plan, plan.plan_digest, **records)
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(session.added, [])
        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertEqual(self.keep_subtitle.read_bytes(), b"changed-keep-subtitle")

    def test_second_rename_failure_is_durably_partial_and_never_touches_keep_files(self) -> None:
        plan = self.make_plan()
        records = self.records()
        harness, module, session, manager, records = self.manager_context(records)
        real_rename = os.rename
        calls = []

        def fail_second(source, destination):
            calls.append((source, destination))
            if len(calls) == 2:
                raise OSError("injected subtitle rename failure")
            return real_rename(source, destination)

        try:
            with mock.patch.object(module.os, "rename", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "완결되지"):
                    manager.execute(plan, plan.plan_digest, **records)
            journal = session.added[0]
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(journal.status, "recovery_required")
        operations = json.loads(journal.unlink_json)
        video = next(value for value in operations if value["kind"] == "video")
        subtitle = next(value for value in operations if value["kind"] == "subtitle")
        self.assertEqual(video["state"], "deleted")
        self.assertEqual(subtitle["state"], "pending")
        self.assertFalse(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertTrue(self.keep_video.exists())
        self.assertEqual(self.keep_subtitle.read_bytes(), b"keep-subtitle")
        self.assertEqual(records["action_log"].status, "unknown")
        self.assertEqual(records["group"].resolution_status, "manual_check_required")
        self.assertGreaterEqual(session.commits, 1)

    def test_source_swap_during_atomic_handoff_is_not_unlinked(self) -> None:
        plan = self.make_plan()
        harness, module, session, manager, records = self.manager_context()
        real_rename = os.rename
        saved_original = self.root / "saved-original-video"
        replacement = b"replacement-must-not-be-unlinked"
        injected = False

        def swap_before_handoff(source, destination):
            nonlocal injected
            if not injected and Path(source) == self.delete_video:
                injected = True
                real_rename(source, saved_original)
                Path(source).write_bytes(replacement)
            return real_rename(source, destination)

        try:
            with mock.patch.object(module.os, "rename", side_effect=swap_before_handoff):
                with self.assertRaisesRegex(RuntimeError, "완결되지"):
                    manager.execute(plan, plan.plan_digest, **records)
            journal = session.added[0]
        finally:
            harness.__exit__(None, None, None)

        self.assertTrue(saved_original.exists())
        operations = json.loads(journal.unlink_json)
        video = next(value for value in operations if value["kind"] == "video")
        tombstone = Path(video["tombstone_path"])
        self.assertTrue(tombstone.exists())
        self.assertEqual(tombstone.read_bytes(), replacement)
        self.assertEqual(video["state"], "pending")
        self.assertTrue(self.delete_subtitle.exists())
        self.assertEqual(self.keep_subtitle.read_bytes(), b"keep-subtitle")

    def test_verify_deleted_rejects_recreated_source_and_changed_keep_subtitle(self) -> None:
        plan = self.make_plan()
        harness, _module, _session, manager, records = self.manager_context()
        try:
            journal = manager.execute(plan, plan.plan_digest, **records)
            self.delete_video.write_bytes(b"recreated")
            with self.assertRaisesRegex(Exception, "다시 생겨"):
                manager.verify_deleted(journal)
            self.delete_video.unlink()
            self.keep_subtitle.write_bytes(b"changed-after-delete")
            with self.assertRaisesRegex(Exception, "유지 자막"):
                manager.verify_deleted(journal)
        finally:
            harness.__exit__(None, None, None)

    def test_restart_preserves_scan_owned_delete_but_marks_incomplete_unknown(self) -> None:
        harness, module, session, manager, records = self.manager_context()
        pending = _Record(
            id=10,
            status="deleted_pending_scan",
            action_log_id=11,
            group_id=12,
            last_error="",
            updated_at=None,
        )
        incomplete = _Record(
            id=20,
            status="deleting",
            action_log_id=21,
            group_id=22,
            last_error="",
            updated_at=None,
        )
        action = _Record(id=21, status="direct_deleting", message="")
        group = _Record(
            id=22,
            safe_to_delete=False,
            resolution_status="delete_in_progress",
            safety_flags_json="[]",
        )
        module.ModelDirectDeleteJournal = types.SimpleNamespace(
            unfinished=lambda: [pending, incomplete]
        )
        module.ModelPostDeleteScanJob = types.SimpleNamespace(
            active_for_action=lambda action_id: _Record(id=99, status="queued")
            if int(action_id) == 11
            else None
        )
        module.ModelActionLog = types.SimpleNamespace(
            get=lambda action_id: action if int(action_id) == 21 else None
        )
        module.ModelDuplicateGroup = types.SimpleNamespace(
            get=lambda group_id: group if int(group_id) == 22 else None
        )
        try:
            count = manager.recover_interrupted()
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(count, 1)
        self.assertEqual(pending.status, "deleted_pending_scan")
        self.assertEqual(incomplete.status, "recovery_required")
        self.assertEqual(action.status, "unknown")
        self.assertEqual(group.resolution_status, "manual_check_required")
        self.assertIn("direct_delete_recovery_required", json.loads(group.safety_flags_json))
        self.assertEqual(session.commits, 1)


if __name__ == "__main__":
    unittest.main()
