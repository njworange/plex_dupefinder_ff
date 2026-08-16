from __future__ import annotations

import errno
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
        journal_at_first_mutation = []
        handoffs = []
        fsynced = []

        def observed_rename(source, destination):
            commits_at_first_mutation.append(session.commits)
            journal_at_first_mutation.append(
                json.loads(session.added[0].unlink_json)
            )
            handoffs.append((Path(source), Path(destination)))
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
        self.assertEqual(json.loads(journal.operation_paths_json), [])
        self.assertTrue(journal_at_first_mutation)
        self.assertEqual(
            {value["state"] for value in journal_at_first_mutation[0]},
            {"pending"},
        )
        for source, destination in handoffs:
            self.assertEqual(source.parent, destination.parent)
            self.assertTrue(destination.name.startswith(".pdff-direct-"))
            self.assertTrue(destination.name.endswith(".tombstone"))
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
        self.assertEqual(
            {value["handoff_strategy"] for value in operations},
            {"same_parent_v2"},
        )

    def test_first_handoff_exdev_is_retryable_no_mutation_and_redacted(self) -> None:
        plan = self.make_plan()
        harness, module, session, manager, records = self.manager_context()
        attempted = []

        def fail_with_realistic_exdev(source, destination):
            attempted.append((str(source), str(destination)))
            error = OSError(errno.EXDEV, "cross-device link")
            error.filename = str(source)
            error.filename2 = str(destination)
            raise error

        try:
            with mock.patch.object(
                module.os, "rename", side_effect=fail_with_realistic_exdev
            ):
                with self.assertRaisesRegex(RuntimeError, "원본 삭제는 시작되지"):
                    manager.execute(plan, plan.plan_digest, **records)
            journal = session.added[0]
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(len(attempted), 1)
        self.assertEqual(journal.status, "failed_no_mutation")
        self.assertIsNotNone(journal.finished_at)
        self.assertEqual(records["action_log"].status, "blocked")
        self.assertEqual(records["group"].resolution_status, "open")
        self.assertTrue(records["group"].safe_to_delete)
        self.assertEqual(json.loads(records["group"].safety_flags_json), [])
        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertTrue(self.keep_video.exists())
        self.assertTrue(self.keep_subtitle.exists())
        self.assertFalse(any(self.folder.glob(".pdff-direct-*")))
        self.assertIn("stage=rename_video_0", journal.last_error)
        self.assertIn("error=OSError", journal.last_error)
        self.assertIn("errno=%s" % errno.EXDEV, journal.last_error)
        self.assertIn("journal=1", journal.last_error)
        self.assertIn("action=5", journal.last_error)
        for secret in (
            attempted[0][0],
            attempted[0][1],
            journal.operation_key,
            ".pdff-direct-",
            "cross-device link",
        ):
            self.assertNotIn(secret, journal.last_error)

    def test_posix_handoff_accepts_path_hash_inode_change_only_with_content_proof(self) -> None:
        harness, module, _session, _manager, _records = self.manager_context()
        original = _write(self.folder / "proof-source.bin", b"A" * 4096)
        handoff = _write(self.folder / ".proof-target", b"A" * 4096)
        timestamp = 1_700_000_000_123_456_700
        os.utime(original, ns=(timestamp, timestamp))
        os.utime(handoff, ns=(timestamp, timestamp))
        actual = module.capture_file_snapshot(str(original), content_hash=False)
        path_hash_identity = module.FileSnapshot(
            path=actual.path,
            size=actual.size,
            mtime_ns=actual.mtime_ns,
            device=actual.device + 101,
            inode=actual.inode + 202,
            links=actual.links,
            sha256="",
        )
        descriptor = os.open(str(original), os.O_RDONLY)
        try:
            module._prove_posix_handoff(
                descriptor, str(handoff), path_hash_identity, False
            )
            handoff.write_bytes(b"B" * 4096)
            os.utime(handoff, ns=(timestamp, timestamp))
            with self.assertRaisesRegex(Exception, "내용이 원본과 다릅니다"):
                module._prove_posix_handoff(
                    descriptor, str(handoff), path_hash_identity, False
                )
        finally:
            os.close(descriptor)
            harness.__exit__(None, None, None)

    def test_posix_subtitle_handoff_uses_raw_full_sha256_proof(self) -> None:
        harness, module, _session, _manager, _records = self.manager_context()
        content = b"1\n00:00:01,000 --> 00:00:02,000\nsubtitle proof\n"
        original = _write(self.folder / "proof-source.ko.srt", content)
        handoff = _write(self.folder / ".proof-target.ko.srt", content)
        timestamp = 1_700_000_100_123_456_700
        os.utime(original, ns=(timestamp, timestamp))
        os.utime(handoff, ns=(timestamp, timestamp))
        actual = module.capture_file_snapshot(str(original), content_hash=True)
        path_hash_identity = module.FileSnapshot(
            path=actual.path,
            size=actual.size,
            mtime_ns=actual.mtime_ns,
            device=actual.device + 303,
            inode=actual.inode + 404,
            links=actual.links,
            sha256=actual.sha256,
        )
        descriptor = os.open(str(original), os.O_RDONLY)
        try:
            module._prove_posix_handoff(
                descriptor, str(handoff), path_hash_identity, True
            )
        finally:
            os.close(descriptor)
            harness.__exit__(None, None, None)

    def test_directory_fsync_tolerates_only_explicitly_unsupported_errors(self) -> None:
        harness, module, _session, _manager, _records = self.manager_context()
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            getattr(errno, "ENOSYS", errno.EINVAL),
        }
        try:
            module.P.logger.messages.clear()
            for code in unsupported:
                with self.subTest(errno=code), mock.patch.object(
                    module.os, "name", "posix"
                ), mock.patch.object(module.os, "open", return_value=91), mock.patch.object(
                    module.os, "fsync", side_effect=OSError(code, "unsupported")
                ), mock.patch.object(module.os, "close") as close:
                    self.assertFalse(module._fsync_directory(str(self.folder)))
                    close.assert_called_once_with(91)
            for code in unsupported:
                self.assertTrue(
                    any(
                        "directory fsync unsupported" in message
                        and str(code) in message
                        for message in module.P.logger.messages
                    )
                )

            with mock.patch.object(
                module.os, "name", "posix"
            ), mock.patch.object(module.os, "open", return_value=92), mock.patch.object(
                module.os, "fsync", side_effect=OSError(errno.EIO, "fatal")
            ), mock.patch.object(module.os, "close") as close:
                with self.assertRaises(OSError) as raised:
                    module._fsync_directory(str(self.folder))
                self.assertEqual(raised.exception.errno, errno.EIO)
                close.assert_called_once_with(92)
        finally:
            harness.__exit__(None, None, None)

    def test_fatal_directory_fsync_after_handoff_requires_manual_recovery(self) -> None:
        plan = self.make_plan()
        harness, module, session, manager, records = self.manager_context()
        try:
            with mock.patch.object(
                module,
                "_fsync_directory",
                side_effect=OSError(errno.EIO, "injected fatal directory fsync"),
            ):
                with self.assertRaisesRegex(RuntimeError, "수동 확인"):
                    manager.execute(plan, plan.plan_digest, **records)
            journal = session.added[0]
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(journal.status, "recovery_required")
        self.assertEqual(records["action_log"].status, "unknown")
        self.assertEqual(records["group"].resolution_status, "manual_check_required")
        operations = json.loads(journal.unlink_json)
        video = next(value for value in operations if value["kind"] == "video")
        self.assertEqual(video["state"], "handoff_unverified")
        self.assertFalse(self.delete_video.exists())
        self.assertTrue(Path(video["tombstone_path"]).exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertTrue(self.keep_video.exists())
        self.assertEqual(self.keep_subtitle.read_bytes(), b"keep-subtitle")
        self.assertIn("stage=handoff_fsync_video_0", journal.last_error)
        self.assertIn("errno=%s" % errno.EIO, journal.last_error)

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
        self.assertEqual(video["state"], "handoff_unverified")
        self.assertTrue(self.delete_subtitle.exists())
        self.assertEqual(self.keep_subtitle.read_bytes(), b"keep-subtitle")

    def test_tombstone_swap_during_unlink_guards_never_unlinks_replacement(self) -> None:
        plan = self.make_plan()
        harness, module, session, manager, records = self.manager_context()
        real_verify = manager._verify_protected
        real_unlink = os.unlink
        verify_calls = 0
        saved_original = self.root / "saved-original-tombstone"
        replacement = b"replacement-must-not-be-unlinked"
        unlinked = []

        def swap_from_final_guard(current_plan):
            nonlocal verify_calls
            verify_calls += 1
            real_verify(current_plan)
            if verify_calls == 3:
                tombstones = list(self.folder.glob(".pdff-direct-*.tombstone"))
                self.assertEqual(len(tombstones), 1)
                os.rename(tombstones[0], saved_original)
                tombstones[0].write_bytes(replacement)
                os.utime(
                    tombstones[0],
                    ns=(plan.video.mtime_ns, plan.video.mtime_ns),
                )

        def observed_unlink(path):
            unlinked.append(str(path))
            return real_unlink(path)

        try:
            with mock.patch.object(
                manager, "_verify_protected", side_effect=swap_from_final_guard
            ), mock.patch.object(module.os, "unlink", side_effect=observed_unlink):
                with self.assertRaisesRegex(RuntimeError, "완결되지"):
                    manager.execute(plan, plan.plan_digest, **records)
            journal = session.added[0]
        finally:
            harness.__exit__(None, None, None)

        operations = json.loads(journal.unlink_json)
        video = next(value for value in operations if value["kind"] == "video")
        replacement_path = Path(video["tombstone_path"])
        self.assertEqual(verify_calls, 3)
        self.assertEqual(unlinked, [])
        self.assertEqual(journal.status, "recovery_required")
        self.assertEqual(video["state"], "tombstoned")
        self.assertEqual(saved_original.read_bytes(), b"delete-video")
        self.assertEqual(replacement_path.read_bytes(), replacement)
        self.assertTrue(self.delete_subtitle.exists())
        self.assertTrue(self.keep_video.exists())
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

    def test_restart_classifies_new_all_pending_source_only_as_no_mutation(self) -> None:
        harness, module, session, manager, _records = self.manager_context()
        tombstone = self.folder / ".pdff-direct-private-000.tombstone"
        pending = _Record(
            id=30,
            status="deleting",
            action_log_id=31,
            group_id=32,
            last_error="",
            updated_at=None,
            finished_at=None,
            operation_paths_json="[]",
            unlink_json=json.dumps(
                [
                    {
                        "source_path": str(self.delete_video),
                        "tombstone_path": str(tombstone),
                        "kind": "video",
                        "state": "pending",
                        "handoff_strategy": "same_parent_v2",
                    }
                ]
            ),
        )
        action = _Record(id=31, status="direct_deleting", message="")
        group = _Record(
            id=32,
            safe_to_delete=True,
            resolution_status="delete_in_progress",
            safety_flags_json="[]",
        )
        module.ModelDirectDeleteJournal = types.SimpleNamespace(
            unfinished=lambda: [pending]
        )
        module.ModelPostDeleteScanJob = types.SimpleNamespace(
            active_for_action=lambda _action_id: None
        )
        module.ModelActionLog = types.SimpleNamespace(get=lambda _action_id: action)
        module.ModelDuplicateGroup = types.SimpleNamespace(get=lambda _group_id: group)
        before = self.delete_video.read_bytes()
        try:
            count = manager.recover_interrupted()
        finally:
            harness.__exit__(None, None, None)

        self.assertEqual(count, 1)
        self.assertEqual(pending.status, "failed_no_mutation")
        self.assertIsNotNone(pending.finished_at)
        self.assertIn("stage=startup_recovery", pending.last_error)
        self.assertEqual(action.status, "blocked")
        self.assertEqual(group.resolution_status, "open")
        self.assertFalse(group.safe_to_delete)
        self.assertEqual(
            json.loads(group.safety_flags_json),
            ["direct_delete_repreview_required"],
        )
        self.assertEqual(self.delete_video.read_bytes(), before)
        self.assertFalse(tombstone.exists())
        self.assertEqual(session.commits, 1)


if __name__ == "__main__":
    unittest.main()
