from __future__ import annotations

import json
import sys
import traceback
import types
import unittest
from datetime import datetime, timedelta

from test_flaskfarm_compat import FlaskFarmImportHarness, PACKAGE_NAME


class _Record:
    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)


class _Session:
    def __init__(self, commit_error=None):
        self.commit_error = commit_error
        self.commits = 0
        self.rollbacks = 0
        self.removes = 0

    def add(self, item):
        pass

    def flush(self):
        pass

    def commit(self):
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self):
        self.rollbacks += 1

    def remove(self):
        self.removes += 1


class BatchBackendTest(unittest.TestCase):
    def _module(self, harness):
        return sys.modules[PACKAGE_NAME + ".batch_delete_manager"]

    def test_batch_serializers_never_expose_nonce_hash_or_global_lease(self) -> None:
        with FlaskFarmImportHarness() as harness:
            batch_type = harness.setup_module.P.ModelBatchRun
            item_type = harness.setup_module.P.ModelBatchItem
            batch = batch_type()
            for key, value in {
                "id": 8,
                "scan_run_id": 3,
                "created_at": None,
                "approved_at": None,
                "started_at": None,
                "finished_at": None,
                "expires_at": None,
                "status": "preview",
                "total_items": 1,
                "processed_items": 0,
                "succeeded_items": 0,
                "failed_items": 0,
                "skipped_items": 0,
                "cancellation_requested": False,
                "current_message": "waiting",
                "error_summary": "",
                "nonce_hash": "secret-digest",
                "lease_key": "global",
                "deletion_lease_token": "internal-secret",
                "confirmation": "BATCH DELETE 8 ITEMS 1",
            }.items():
                setattr(batch, key, value)
            payload = batch.as_api()
            self.assertEqual(payload["plan_id"], 8)
            self.assertNotIn("nonce", payload)
            self.assertNotIn("nonce_hash", payload)
            self.assertNotIn("lease_key", payload)
            self.assertNotIn("deletion_lease_token", payload)
            self.assertNotIn("confirmation", payload)

            item = item_type()
            for key, value in {
                "id": 11,
                "batch_run_id": 8,
                "scan_run_id": 3,
                "group_id": 4,
                "keep_candidate_id": 5,
                "delete_candidate_id": 6,
                "action_log_id": None,
                "created_at": None,
                "started_at": None,
                "finished_at": None,
                "status": "planned",
                "message": "waiting",
                "title": "Movie",
                "media_type": "movie",
                "keep_media_id": "20",
                "delete_media_id": "10",
                "keep_score": 2.0,
                "delete_score": 1.0,
                "keep_paths_json": '["/media/keep.mkv"]',
                "delete_paths_json": '["/media/delete.mkv"]',
            }.items():
                setattr(item, key, value)
            item_payload = item.as_api()
            self.assertEqual(item_payload["keep"]["candidate_id"], 5)
            self.assertEqual(item_payload["delete"]["paths"], ["/media/delete.mkv"])

    def test_db_lease_driver_errors_are_sanitized_and_renew_is_lost(self) -> None:
        with FlaskFarmImportHarness() as harness:
            lease_module = sys.modules[PACKAGE_NAME + ".deletion_lease"]
            sensitive = "OWNER-TOKEN-SHOULD-NOT-ESCAPE"

            def driver_error(operation, token=sensitive):
                return RuntimeError(
                    "SQL %s failed; params={'owner_token': '%s'}" % (operation, token)
                )

            class ExplodingLease:
                @classmethod
                def get_singleton(cls):
                    raise driver_error("SELECT")

                @classmethod
                def claim_free(cls, owner_token, *args):
                    raise driver_error("UPDATE", owner_token)

                @classmethod
                def renew(cls, owner_token, *args):
                    raise driver_error("RENEW", owner_token)

                @classmethod
                def release(cls, owner_token):
                    raise driver_error("RELEASE", owner_token)

                @classmethod
                def claim_expired_for_recovery(cls, *args):
                    raise driver_error("RECOVERY")

            lease_module.ModelDeletionLease = ExplodingLease
            lease_module.F.db.session = _Session()

            service = lease_module.DeletionLeaseService()
            with self.assertRaises(lease_module.DeletionLeaseError) as init_error:
                service._ensure_row()
            self.assertNotIn(sensitive, str(init_error.exception))
            self.assertNotIn("SQL", str(init_error.exception))
            init_traceback = "".join(
                traceback.format_exception(
                    type(init_error.exception),
                    init_error.exception,
                    init_error.exception.__traceback__,
                )
            )
            self.assertNotIn(sensitive, init_traceback)
            self.assertNotIn("params=", init_traceback)

            # Exercise each public DB path without repeating initialization.
            service._ensure_row = lambda: None
            operations = (
                (lambda: service.acquire("batch", "41"), lease_module.DeletionLeaseError),
                (
                    lambda: service.renew(sensitive, "batch", "41"),
                    lease_module.DeletionLeaseLost,
                ),
                (lambda: service.release(sensitive), lease_module.DeletionLeaseError),
                (service.acquire_for_recovery, lease_module.DeletionLeaseError),
                (service.recovery_state, lease_module.DeletionLeaseError),
                (service.active_batch_id, lease_module.DeletionLeaseError),
            )
            renew_error = None
            for operation, expected_type in operations:
                with self.assertRaises(expected_type) as raised:
                    operation()
                message = str(raised.exception)
                self.assertNotIn(sensitive, message)
                self.assertNotIn("SQL", message)
                formatted = "".join(
                    traceback.format_exception(
                        type(raised.exception),
                        raised.exception,
                        raised.exception.__traceback__,
                    )
                )
                self.assertNotIn(sensitive, formatted)
                self.assertNotIn("params=", formatted)
                if expected_type is lease_module.DeletionLeaseLost:
                    renew_error = raised.exception

            # A worker that cannot prove lease ownership must leave persisted
            # status/error_summary untouched for the recovery CAS winner.
            batch = _Record(
                id=41,
                status="running",
                cancellation_requested=False,
                deletion_lease_token=sensitive,
                error_summary="",
                current_message="working",
            )

            class Batches:
                @classmethod
                def get(cls, batch_id):
                    return batch

            class LostLease:
                @staticmethod
                def renew(*args):
                    raise renew_error

            module = self._module(harness)
            module.ModelBatchRun = Batches
            manager = module.BatchDeleteManager()
            manager.lease_service = LostLease()
            stop = manager._worker_should_stop(41)
            self.assertEqual(stop[0], "lease_lost")
            self.assertNotIn(sensitive, stop[1])
            self.assertEqual(batch.status, "running")
            self.assertEqual(batch.error_summary, "")
            self.assertEqual(batch.current_message, "working")

    def test_plan_requires_exactly_two_active_candidates_and_unique_winner(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)
            group = _Record(id=1, safe_to_delete=True, resolution_status="open")
            first = _Record(id=10, score=100.0)
            second = _Record(id=20, score=50.0)

            class Candidates:
                values = [first, second]

                @classmethod
                def by_group(cls, group_id, include_deleted=False):
                    return list(cls.values)

            module.ModelMediaCandidate = Candidates
            group.recommended_candidate_id = 10
            pair = module.BatchDeleteManager._eligible_pair(group)
            self.assertEqual((pair[0].id, pair[1].id), (10, 20))

            second.score = 100.0
            self.assertIsNone(module.BatchDeleteManager._eligible_pair(group))
            second.score = 50.0
            Candidates.values.append(_Record(id=30, score=1.0))
            self.assertIsNone(module.BatchDeleteManager._eligible_pair(group))

    def test_cross_group_paths_use_remote_case_policy_and_block_both_groups(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)
            groups = [_Record(id=1), _Record(id=2), _Record(id=3)]

            class Groups:
                @classmethod
                def all_by_run(cls, run_id):
                    return groups

            candidates = {
                1: [_Record(parts_json='[{"file":"D:/Media/Same.mkv"}]')],
                2: [_Record(parts_json='[{"file":"d:\\\\media\\\\same.mkv"}]')],
                3: [_Record(parts_json='[{"file":"/media/Unique.mkv"}]')],
            }

            class Candidates:
                @classmethod
                def by_group(cls, group_id, include_deleted=False):
                    return candidates[group_id]

            path_module = sys.modules[PACKAGE_NAME + ".path_conflicts"]
            path_module.ModelDuplicateGroup = Groups
            path_module.ModelMediaCandidate = Candidates
            self.assertEqual(
                module.BatchDeleteManager._cross_group_path_conflicts(9), {1, 2}
            )

    def test_snapshot_must_match_at_preview_and_approval_validation(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)
            current = json.loads(
                module._json(
                    module._config_snapshot(
                        module.current_score_config(), module.current_safety_policy()
                    )
                )
            )
            run = _Record(settings_snapshot_json=json.dumps(current))
            module.BatchDeleteManager._assert_settings_snapshot(run)
            current["score"]["bitrate_weight"] += 1
            run.settings_snapshot_json = json.dumps(current)
            with self.assertRaisesRegex(RuntimeError, "다시 스캔"):
                module.BatchDeleteManager._assert_settings_snapshot(run)

    def test_quarantine_batch_rejects_shared_video_or_subtitle_source(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)
            shared_video = "/media/movies/Shared/Film.mkv"
            shared_subtitle = "/media/movies/Shared/Film.ko.srt"

            with self.assertRaisesRegex(RuntimeError, "공유"):
                module.BatchDeleteManager._assert_source_sets_disjoint(
                    [
                        {
                            "subtitle_cleanup": {
                                "video": {"path": shared_video},
                                "eligible": [{"source_path": shared_subtitle}],
                            }
                        },
                        {
                            "subtitle_cleanup": {
                                "video": {"path": shared_video},
                                "eligible": [],
                            }
                        },
                    ]
                )

            with self.assertRaisesRegex(RuntimeError, "공유"):
                module.BatchDeleteManager._assert_source_sets_disjoint(
                    [
                        {
                            "subtitle_cleanup": {
                                "video": {"path": "/media/movies/A/A.mkv"},
                                "eligible": [{"source_path": shared_subtitle}],
                            }
                        },
                        {
                            "subtitle_cleanup": {
                                "video": {"path": "/media/movies/B/B.mkv"},
                                "eligible": [{"path": shared_subtitle}],
                            }
                        },
                    ]
                )

    def test_quarantine_preview_cannot_be_approved_after_backend_switch_to_plex(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)
            harness.setup_module.P.ModelSetting._data.update(
                {
                    "setting_delete_backend": "plex",
                    "setting_post_delete_scan_mode": "web",
                    "setting_quarantine_root": "/quarantine",
                    "setting_max_delete_per_run": "10",
                    "setting_batch_max_items": "10",
                }
            )
            run = _Record(id=9, status="completed", deletion_attempts=0)
            batch = _Record(
                id=7,
                scan_run_id=9,
                total_items=1,
                confirmation="BATCH QUARANTINE 7 ITEMS 1 SUBTITLES 1 aaaaaaaaaaaa",
            )
            group = _Record(
                id=4,
                run_id=9,
                safe_to_delete=True,
                resolution_status="open",
                recommended_candidate_id=5,
            )
            keep = _Record(id=5, media_id="20", score=100.0)
            target = _Record(id=6, media_id="10", score=50.0)
            item = _Record(
                batch_run_id=7,
                group_id=4,
                keep_candidate_id=5,
                delete_candidate_id=6,
                keep_media_id="20",
                delete_media_id="10",
            )
            journal = _Record(
                plan_digest="a" * 64,
                manifest_json=json.dumps(
                    {
                        "batch_binding": {
                            "backend": "quarantine",
                            "post_delete_scan_mode": "web",
                            "quarantine_root": "/quarantine",
                        }
                    }
                ),
            )

            class Runs:
                @classmethod
                def get(cls, run_id):
                    return run

            class Items:
                @classmethod
                def by_batch(cls, batch_id):
                    return [item]

            class Groups:
                @classmethod
                def get(cls, group_id):
                    return group

            class Candidates:
                @classmethod
                def by_group(cls, group_id, include_deleted=False):
                    return [keep, target]

            class Journals:
                @classmethod
                def for_batch_candidate(cls, batch_id, candidate_id, status=""):
                    return journal

            module.ModelScanRun = Runs
            module.ModelBatchItem = Items
            module.ModelDuplicateGroup = Groups
            module.ModelMediaCandidate = Candidates
            module.ModelQuarantineJournal = Journals
            manager = module.BatchDeleteManager(
                types.SimpleNamespace(
                    preview=lambda **kwargs: (_ for _ in ()).throw(
                        AssertionError("backend drift must block before fresh preview")
                    )
                )
            )
            manager._assert_settings_snapshot = lambda value: None
            manager._cross_group_path_conflicts = lambda run_id: set()

            with self.assertRaisesRegex(RuntimeError, "변경|사전확인|격리"):
                manager._validate_plan_unchanged(batch)

    def test_approve_requires_byte_exact_confirmation_and_maps_lease_conflict(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)
            harness.setup_module.P.ModelSetting._data.update(
                {
                    "setting_delete_enabled": "True",
                    "setting_batch_delete_enabled": "True",
                }
            )
            nonce = "one-time-nonce"
            batch = _Record(
                id=7,
                status="preview",
                expires_at=datetime.now() + timedelta(seconds=60),
                nonce_hash=module._nonce_hash(nonce),
                confirmation="BATCH DELETE 7 ITEMS 2",
            )

            class Batches:
                @classmethod
                def get(cls, batch_id):
                    return batch

                @classmethod
                def active(cls):
                    return None

                @classmethod
                def claim_for_approval(cls, batch_id, digest, lease_token, now):
                    return True

            class LeaseStub:
                def __init__(self):
                    self.released = []

                def acquire(self, owner_kind, owner_ref):
                    return "APPROVAL-LEASE-TOKEN-SHOULD-NOT-ESCAPE"

                def release(self, token):
                    self.released.append(token)
                    return True

            module.ModelBatchRun = Batches
            manager = module.BatchDeleteManager()
            manager.lease_service = LeaseStub()
            manager._validate_plan_unchanged = lambda value: None
            manager._start_worker = lambda value: None
            manager.status = lambda **kwargs: {"plan_id": kwargs["batch_id"]}

            session = _Session()
            module.F.db.session = session
            with self.assertRaisesRegex(ValueError, "확인 문구"):
                manager.approve(7, nonce, " BATCH DELETE 7 ITEMS 2")

            sensitive = "APPROVAL-LEASE-TOKEN-SHOULD-NOT-ESCAPE"
            session.commit_error = ValueError(
                "SQL UPDATE batch_run params={'deletion_lease_token': '%s'}" % sensitive
            )
            with self.assertRaisesRegex(RuntimeError, "다른 삭제") as approval_error:
                manager.approve(7, nonce, "BATCH DELETE 7 ITEMS 2")
            formatted = "".join(
                traceback.format_exception(
                    type(approval_error.exception),
                    approval_error.exception,
                    approval_error.exception.__traceback__,
                )
            )
            self.assertNotIn(sensitive, str(approval_error.exception))
            self.assertNotIn(sensitive, formatted)
            self.assertNotIn("deletion_lease_token", formatted)
            self.assertNotIn("params=", formatted)
            self.assertEqual(session.rollbacks, 1)

    def test_worker_stops_after_first_failure_and_skips_remaining_items(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)
            batch = _Record(
                id=1,
                scan_run_id=9,
                status="queued",
                total_items=3,
                processed_items=0,
                succeeded_items=0,
                failed_items=0,
                skipped_items=0,
                lease_key="global",
                nonce_hash="",
                current_message="",
                error_summary="",
                finished_at=None,
                deletion_lease_token="batch-lease",
            )
            items = [
                _Record(
                    id=index,
                    batch_run_id=1,
                    scan_run_id=9,
                    group_id=index,
                    delete_candidate_id=100 + index,
                    keep_candidate_id=200 + index,
                    delete_media_id=str(100 + index),
                    status="planned",
                    message="",
                    action_log_id=None,
                    started_at=None,
                    finished_at=None,
                )
                for index in (1, 2, 3)
            ]

            class Batches:
                @classmethod
                def claim_for_worker(cls, batch_id, now):
                    batch.status = "running"
                    return True

                @classmethod
                def get(cls, batch_id):
                    return batch

            class Items:
                @classmethod
                def by_batch(cls, batch_id):
                    return items

                @classmethod
                def get(cls, item_id):
                    return next(item for item in items if item.id == item_id)

                @classmethod
                def claim_for_worker(cls, item_id, now):
                    item = cls.get(item_id)
                    if item.status != "planned":
                        return False
                    item.status = "running"
                    item.started_at = now
                    return True

            class Actions:
                @classmethod
                def latest_for_delete(cls, run_id, group_id, candidate_id):
                    return _Record(id=88, status="blocked", message="fresh check failed")

            class Journals:
                @classmethod
                def for_batch_candidate(cls, batch_id, candidate_id, status=""):
                    return None

            class Deletes:
                def __init__(self):
                    self.calls = []

                def delete(self, **kwargs):
                    self.calls.append(kwargs["group_id"])
                    if kwargs["group_id"] == 2:
                        raise RuntimeError("fresh check failed")
                    return {"action_id": 77}

            module.ModelBatchRun = Batches
            module.ModelBatchItem = Items
            module.ModelActionLog = Actions
            module.ModelQuarantineJournal = Journals
            session = _Session()
            module.F.db.session = session
            deletes = Deletes()
            manager = module.BatchDeleteManager(deletes)
            manager.lease_service = types.SimpleNamespace(
                renew=lambda *args: None,
                release=lambda *args: True,
            )
            manager._worker_should_stop = lambda batch_id: None
            manager._worker(1)

            self.assertEqual(deletes.calls, [1, 2])
            self.assertEqual([item.status for item in items], ["success", "blocked", "skipped"])
            self.assertEqual(batch.status, "stopped")
            self.assertIsNone(batch.lease_key)
            self.assertEqual(batch.processed_items, 2)
            self.assertEqual(batch.skipped_items, 1)
            self.assertEqual(session.removes, 1)

    def test_quarantine_worker_backend_drift_makes_zero_delete_service_calls(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)
            harness.setup_module.P.ModelSetting._data.update(
                {
                    "setting_delete_enabled": "True",
                    "setting_batch_delete_enabled": "True",
                    "setting_delete_backend": "plex",
                }
            )
            batch = _Record(
                id=31,
                scan_run_id=9,
                status="queued",
                confirmation="BATCH QUARANTINE 31 ITEMS 1 SUBTITLES 1 aaaaaaaaaaaa",
                total_items=1,
                processed_items=0,
                succeeded_items=0,
                failed_items=0,
                skipped_items=0,
                lease_key="global",
                nonce_hash="",
                current_message="",
                error_summary="",
                finished_at=None,
                deletion_lease_token="batch-lease",
            )
            item = _Record(
                id=32,
                batch_run_id=31,
                scan_run_id=9,
                group_id=33,
                delete_candidate_id=34,
                keep_candidate_id=35,
                delete_media_id="10",
                status="planned",
                message="",
                action_log_id=None,
                started_at=None,
                finished_at=None,
            )

            class Batches:
                @classmethod
                def claim_for_worker(cls, batch_id, now):
                    batch.status = "running"
                    return True

                @classmethod
                def get(cls, batch_id):
                    return batch

            class Items:
                @classmethod
                def by_batch(cls, batch_id):
                    return [item]

                @classmethod
                def get(cls, item_id):
                    return item

                @classmethod
                def claim_for_worker(cls, item_id, now):
                    item.status = "running"
                    item.started_at = now
                    return True

            class Actions:
                @classmethod
                def latest_for_delete(cls, run_id, group_id, candidate_id):
                    return None

            class Deletes:
                def __init__(self):
                    self.calls = []

                def delete(self, **kwargs):
                    self.calls.append(kwargs)
                    return {"action_id": 99, "verification": "confirmed"}

            module.ModelBatchRun = Batches
            module.ModelBatchItem = Items
            module.ModelActionLog = Actions
            module.F.db.session = _Session()
            deletes = Deletes()
            manager = module.BatchDeleteManager(deletes)
            manager.lease_service = types.SimpleNamespace(
                renew=lambda *args: None,
                release=lambda *args: True,
            )
            manager._worker_should_stop = lambda batch_id: None

            manager._worker(31)

            self.assertEqual(deletes.calls, [])
            self.assertEqual(item.status, "failed")
            self.assertEqual(batch.status, "stopped")

    def test_restart_running_item_uses_unknown_audit_and_never_looks_skipped(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)
            batch = _Record(
                id=1,
                status="running",
                total_items=1,
                processed_items=0,
                succeeded_items=0,
                failed_items=0,
                skipped_items=0,
                lease_key="global",
                nonce_hash="hash",
                current_message="",
                error_summary="",
                finished_at=None,
                deletion_lease_token="expired-batch-lease",
            )
            item = _Record(
                id=2,
                batch_run_id=1,
                scan_run_id=3,
                group_id=4,
                delete_candidate_id=5,
                status="running",
                message="",
                action_log_id=None,
                finished_at=None,
            )
            log = _Record(id=6, status="unknown", message="Plex 확인 필요")

            class Batches:
                @classmethod
                def unfinished(cls):
                    return [batch]

            class Items:
                @classmethod
                def by_batch(cls, batch_id):
                    return [item]

            class Actions:
                @classmethod
                def latest_for_delete(cls, run_id, group_id, candidate_id):
                    return log

            module.ModelBatchRun = Batches
            module.ModelBatchItem = Items
            module.ModelActionLog = Actions
            module.F.db.session = _Session()
            manager = module.BatchDeleteManager()
            manager.delete_service = types.SimpleNamespace(
                recover_interrupted=lambda: {"blocked": 0, "unknown": 1}
            )
            manager.lease_service = types.SimpleNamespace(
                recovery_state=lambda: "expired",
                acquire_for_recovery=lambda: _Record(token="recovery-lease"),
                release=lambda token: True,
            )
            count = manager.recover_interrupted()
            self.assertEqual(count, 1)
            self.assertEqual(item.status, "unknown")
            self.assertEqual(item.action_log_id, 6)
            self.assertEqual(batch.status, "interrupted")
            self.assertIsNone(batch.lease_key)

    def test_valid_db_batch_lease_excludes_other_worker_from_recovery(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)
            batch = _Record(id=41, status="running")
            item = _Record(
                status="running",
                scan_run_id=3,
                group_id=4,
                delete_candidate_id=5,
            )

            class Batches:
                @classmethod
                def unfinished(cls):
                    return [batch]

            class Items:
                @classmethod
                def by_batch(cls, batch_id):
                    return [item]

            module.ModelBatchRun = Batches
            module.ModelBatchItem = Items
            module.F.db.session = _Session()
            manager = module.BatchDeleteManager()
            manager.lease_service = types.SimpleNamespace(
                recovery_state=lambda: "busy",
                acquire_for_recovery=lambda: None,
                active_batch_id=lambda: 41,
            )
            self.assertEqual(manager.recover_interrupted(), 0)
            self.assertEqual(manager.live_delete_keys(), {(3, 4, 5)})
            self.assertEqual(batch.status, "running")

    def test_worker_checks_db_lease_before_honouring_unload(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)
            batch = _Record(
                id=41,
                status="running",
                cancellation_requested=False,
                deletion_lease_token="expired-owner",
            )

            class Batches:
                @classmethod
                def get(cls, batch_id):
                    return batch

            class Lease:
                @staticmethod
                def renew(token, owner_kind, owner_ref):
                    raise module.DeletionLeaseLost("recovery already owns the lease")

            module.ModelBatchRun = Batches
            manager = module.BatchDeleteManager()
            manager.lease_service = Lease()
            manager._unloading.set()

            stop = manager._worker_should_stop(41)

            self.assertEqual(stop[0], "lease_lost")
            self.assertIn("recovery", stop[1])

    def test_clean_free_recovery_fast_path_does_not_write_lease(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module(harness)

            class Batches:
                @classmethod
                def unfinished(cls):
                    return []

            class Actions:
                @classmethod
                def interrupted(cls):
                    return []

            class Lease:
                @staticmethod
                def recovery_state():
                    return "free"

                @staticmethod
                def acquire_for_recovery():
                    raise AssertionError("clean polling must not write the lease")

            module.ModelBatchRun = Batches
            module.ModelActionLog = Actions
            manager = module.BatchDeleteManager()
            manager.lease_service = Lease()
            self.assertEqual(manager.recover_interrupted(), 0)


if __name__ == "__main__":
    unittest.main()
