from __future__ import annotations

import importlib
import json
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from services.domain import MediaPart, MediaVersion, MetadataItem


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "pdff_sqlite_lease_test"


class SQLiteDeletionLeaseIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory(prefix="pdff-lease-test-")
        database = Path(cls.tempdir.name) / "lease.db"
        cls.app = Flask(__name__)
        cls.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        cls.app.config["SQLALCHEMY_BINDS"] = {
            PACKAGE: "sqlite:///%s" % database.as_posix()
        }
        cls.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        cls.db = SQLAlchemy(
            cls.app,
            session_options={"autoflush": False, "expire_on_commit": False},
        )

        plugin = types.ModuleType("plugin")
        plugin.ModelBase = cls.db.Model
        framework = types.ModuleType("framework")
        framework.F = types.SimpleNamespace(db=cls.db, app=cls.app)
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(PROJECT_ROOT)]
        setup = types.ModuleType(PACKAGE + ".setup")

        class Settings:
            @classmethod
            def get(inner_cls, key):
                return "120" if key == "setting_request_timeout" else None

        setup.P = types.SimpleNamespace(package_name=PACKAGE, ModelSetting=Settings)
        cls.saved = {
            name: sys.modules.get(name)
            for name in ("plugin", "framework", PACKAGE, PACKAGE + ".setup")
        }
        sys.modules.update(
            {
                "plugin": plugin,
                "framework": framework,
                PACKAGE: package,
                PACKAGE + ".setup": setup,
            }
        )
        cls.models = importlib.import_module(PACKAGE + ".models")
        cls.lease_module = importlib.import_module(PACKAGE + ".deletion_lease")
        cls.post_scan_module = importlib.import_module(PACKAGE + ".post_delete_scan")
        with cls.app.app_context():
            cls.db.create_all()

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.app.app_context():
            cls.db.session.remove()
            # SQLAlchemy 2.x keeps the SQLite file handle in its pool on
            # Windows until every bound engine is explicitly disposed.
            engines = getattr(cls.db, "engines", {})
            for engine in set(engines.values()):
                engine.dispose()
        for name in list(sys.modules):
            if name == PACKAGE or name.startswith(PACKAGE + "."):
                sys.modules.pop(name, None)
        for name in ("plugin", "framework"):
            sys.modules.pop(name, None)
            if cls.saved.get(name) is not None:
                sys.modules[name] = cls.saved[name]
        cls.tempdir.cleanup()

    def setUp(self) -> None:
        with self.app.app_context():
            self.db.session.query(self.models.ModelPostDeleteScanJob).delete()
            self.db.session.query(self.models.ModelBatchItem).delete()
            self.db.session.query(self.models.ModelBatchRun).delete()
            self.db.session.query(self.models.ModelActionLog).delete()
            self.db.session.query(self.models.ModelMediaCandidate).delete()
            self.db.session.query(self.models.ModelDuplicateGroup).delete()
            self.db.session.query(self.models.ModelScanRun).delete()
            self.db.session.query(self.models.ModelDeletionLease).delete()
            self.db.session.add(
                self.models.ModelDeletionLease(
                    id=1, owner_token="", owner_kind="", owner_ref=""
                )
            )
            self.db.session.commit()

    def test_singleton_acquire_renew_release_and_expired_recovery_cas(self) -> None:
        service = self.lease_module.DeletionLeaseService()
        token = service.acquire("manual", "1:2:3")
        self.assertTrue(token)
        with self.assertRaises(self.lease_module.DeletionLeaseBusy):
            self.lease_module.DeletionLeaseService().acquire("batch", "9")

        with self.app.app_context():
            before = self.models.ModelDeletionLease.get_singleton().expires_at
        service.renew(token, "manual", "1:2:3")
        with self.app.app_context():
            after = self.models.ModelDeletionLease.get_singleton().expires_at
        self.assertGreaterEqual(after, before)
        self.assertTrue(service.release(token))

        batch_token = service.acquire("batch", "77")
        with self.app.app_context():
            lease = self.models.ModelDeletionLease.get_singleton()
            lease.expires_at = datetime.now() - timedelta(seconds=1)
            self.db.session.commit()

        recovery = self.lease_module.DeletionLeaseService().acquire_for_recovery()
        self.assertIsNotNone(recovery)
        self.assertEqual(recovery.previous_kind, "batch")
        self.assertEqual(recovery.previous_ref, "77")
        self.assertTrue(recovery.previous_expired)
        self.assertNotEqual(recovery.token, batch_token)
        self.assertIsNone(
            self.lease_module.DeletionLeaseService().acquire_for_recovery()
        )
        self.assertTrue(service.release(recovery.token))

    def test_two_threads_cannot_hold_manual_and_batch_lease_together(self) -> None:
        start = threading.Barrier(3)
        attempted = threading.Barrier(3)
        results = []
        results_lock = threading.Lock()

        def contender(kind, ref):
            start.wait()
            token = ""
            try:
                token = self.lease_module.DeletionLeaseService().acquire(kind, ref)
                outcome = "acquired"
            except self.lease_module.DeletionLeaseBusy:
                outcome = "busy"
            with results_lock:
                results.append((kind, outcome))
            attempted.wait()
            if token:
                self.lease_module.DeletionLeaseService().release(token)

        first = threading.Thread(target=contender, args=("manual", "m"))
        second = threading.Thread(target=contender, args=("batch", "b"))
        first.start()
        second.start()
        start.wait()
        attempted.wait()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sorted(value for _, value in results), ["acquired", "busy"])

    def test_scan_attempt_limit_two_atomically_accepts_two_and_rejects_third(self) -> None:
        with self.app.app_context():
            run = self.models.ModelScanRun(
                status="completed",
                deletion_attempts=0,
                server_machine_id="machine-1",
            )
            self.db.session.add(run)
            self.db.session.commit()
            run_id = run.id

            self.assertTrue(self.models.ModelScanRun.claim_deletion_slot(run_id, 2))
            self.db.session.commit()
            self.assertTrue(self.models.ModelScanRun.claim_deletion_slot(run_id, 2))
            self.db.session.commit()
            self.assertFalse(self.models.ModelScanRun.claim_deletion_slot(run_id, 2))
            self.db.session.rollback()

            self.db.session.expire_all()
            stored = self.models.ModelScanRun.get(run_id)
            self.assertEqual(stored.deletion_attempts, 2)

    def test_batch_approval_cas_persists_internal_token_without_serializing_it(self) -> None:
        now = datetime.now()
        with self.app.app_context():
            batch = self.models.ModelBatchRun(
                scan_run_id=1,
                created_at=now,
                expires_at=now + timedelta(minutes=2),
                status="preview",
                nonce_hash="nonce-digest",
                total_items=1,
            )
            self.db.session.add(batch)
            self.db.session.commit()
            batch_id = batch.id

            self.assertTrue(
                self.models.ModelBatchRun.claim_for_approval(
                    batch_id,
                    "nonce-digest",
                    "internal-deletion-lease",
                    now,
                )
            )
            self.db.session.commit()
            self.assertFalse(
                self.models.ModelBatchRun.claim_for_approval(
                    batch_id,
                    "nonce-digest",
                    "another-token",
                    now,
                )
            )
            self.db.session.rollback()
            stored = self.models.ModelBatchRun.get(batch_id)
            self.assertEqual(stored.status, "queued")
            self.assertEqual(stored.deletion_lease_token, "internal-deletion-lease")
            payload = stored.as_api()
            self.assertNotIn("deletion_lease_token", payload)
            self.assertNotIn("lease_key", payload)

    def test_lease_ttl_is_at_least_twenty_minutes(self) -> None:
        self.assertGreaterEqual(self.lease_module._lease_seconds(), 20 * 60)

    def _new_scan_job(self, **overrides):
        now = datetime.now()
        values = {
            "action_log_id": 11,
            "action_ids_json": "[11]",
            "run_id": 1,
            "group_id": 2,
            "candidate_id": 3,
            "server_machine_id": "machine-1",
            "mode": "web",
            "section_key": "7",
            "media_type": "movie",
            "target_path": "/library/movies/Example",
            "target_key": "target-key",
            "dedupe_key": "dedupe-key",
            "status": "queued",
            "attempts": 0,
            "max_attempts": 3,
            "next_attempt_at": now,
        }
        values.update(overrides)
        job = self.models.ModelPostDeleteScanJob(**values)
        self.db.session.add(job)
        self.db.session.commit()
        return job.id

    def test_scan_job_claim_is_cas_and_only_one_worker_owns_the_lease(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job()
            now = datetime.now()
            lease_until = now + timedelta(minutes=2)

            self.assertTrue(
                self.models.ModelPostDeleteScanJob.claim_for_worker(
                    job_id, "worker-a", now, lease_until
                )
            )
            self.db.session.commit()
            self.assertFalse(
                self.models.ModelPostDeleteScanJob.claim_for_worker(
                    job_id, "worker-b", now, lease_until
                )
            )
            self.db.session.rollback()

            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            self.assertEqual(stored.status, "running")
            self.assertEqual(stored.attempts, 1)
            self.assertEqual(stored.worker_token, "worker-a")
            self.assertEqual(stored.lease_key, "global")

    def test_scan_job_stale_recovery_is_owner_cas_and_can_retry(self) -> None:
        now = datetime.now()
        with self.app.app_context():
            job_id = self._new_scan_job(
                status="running",
                attempts=1,
                worker_token="worker-a",
                lease_key="global",
                lease_expires_at=now - timedelta(seconds=1),
            )

            self.assertFalse(
                self.models.ModelPostDeleteScanJob.recover_stale_one(
                    job_id, "wrong-worker", now
                )
            )
            self.assertTrue(
                self.models.ModelPostDeleteScanJob.recover_stale_one(
                    job_id, "worker-a", now
                )
            )
            self.db.session.commit()
            recovered = self.models.ModelPostDeleteScanJob.get(job_id)
            self.assertEqual(recovered.status, "retry_wait")
            self.assertEqual(recovered.attempts, 1)
            self.assertEqual(recovered.worker_token, "")
            self.assertIsNone(recovered.lease_key)

            self.assertTrue(
                self.models.ModelPostDeleteScanJob.claim_for_worker(
                    job_id,
                    "worker-b",
                    now,
                    now + timedelta(minutes=2),
                )
            )
            self.db.session.commit()
            self.db.session.expire_all()
            self.assertEqual(
                self.models.ModelPostDeleteScanJob.get(job_id).attempts, 2
            )

    def test_scan_job_stale_recovery_stops_at_max_attempts(self) -> None:
        now = datetime.now()
        with self.app.app_context():
            job_id = self._new_scan_job(
                status="running",
                attempts=3,
                max_attempts=3,
                worker_token="worker-a",
                lease_key="global",
                lease_expires_at=now - timedelta(seconds=1),
            )

            self.assertTrue(
                self.models.ModelPostDeleteScanJob.recover_stale_one(
                    job_id, "worker-a", now
                )
            )
            self.db.session.commit()
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            self.assertEqual(stored.status, "failed")
            self.assertIsNotNone(stored.finished_at)

    def test_scan_job_retry_releases_worker_claim_and_schedules_backoff(self) -> None:
        now = datetime.now()
        with self.app.app_context():
            job_id = self._new_scan_job(
                status="running",
                attempts=1,
                worker_token="worker-a",
                lease_key="global",
                lease_expires_at=now + timedelta(minutes=2),
            )

        manager = self.post_scan_module.PostDeleteScanManager()
        self.assertTrue(
            manager._finish_claimed(
                job_id,
                "worker-a",
                "retry_wait",
                "transient failure",
            )
        )

        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            self.assertEqual(stored.status, "retry_wait")
            self.assertGreater(stored.next_attempt_at, now)
            self.assertEqual(stored.worker_token, "")
            self.assertIsNone(stored.lease_key)
            self.assertIsNone(stored.lease_expires_at)
            self.assertIsNone(
                self.models.ModelPostDeleteScanJob.eligible_next(datetime.now())
            )

    def test_restart_waits_for_ttl_then_recovers_job_and_global_lease(self) -> None:
        now = datetime.now()
        future = now + timedelta(minutes=2)
        manager = self.post_scan_module.PostDeleteScanManager()
        with self.app.app_context():
            job_id = self._new_scan_job(
                status="running",
                attempts=1,
                worker_token="dead-worker",
                lease_key="global",
                lease_expires_at=future,
            )
            lease = self.models.ModelDeletionLease.get_singleton()
            lease.owner_token = "dead-global-token"
            lease.owner_kind = "post_scan"
            lease.owner_ref = str(job_id)
            lease.acquired_at = now
            lease.heartbeat_at = now
            lease.expires_at = future
            self.db.session.commit()

        self.assertEqual(manager.recover_stale(), 0)
        with self.app.app_context():
            self.assertEqual(
                self.models.ModelPostDeleteScanJob.get(job_id).status, "running"
            )
            self.assertEqual(
                self.models.ModelDeletionLease.get_singleton().owner_token,
                "dead-global-token",
            )

            self.models.ModelPostDeleteScanJob.get(job_id).lease_expires_at = (
                now - timedelta(seconds=1)
            )
            self.models.ModelDeletionLease.get_singleton().expires_at = (
                now - timedelta(seconds=1)
            )
            self.db.session.commit()

        self.assertEqual(manager.recover_stale(), 1)
        with self.app.app_context():
            recovered = self.models.ModelPostDeleteScanJob.get(job_id)
            lease = self.models.ModelDeletionLease.get_singleton()
            self.assertEqual(recovered.status, "retry_wait")
            self.assertEqual(recovered.worker_token, "")
            self.assertIsNone(recovered.lease_key)
            self.assertEqual(lease.owner_token, "")

        claimed_job_id, worker_token, deletion_token = manager._claim_next()
        self.assertEqual(claimed_job_id, job_id)
        self.assertTrue(worker_token)
        self.assertTrue(deletion_token)
        self.assertTrue(manager.lease_service.release(deletion_token))

    def test_live_job_lease_prevents_earlier_global_post_scan_expiry_clear(self) -> None:
        now = datetime.now()
        manager = self.post_scan_module.PostDeleteScanManager()
        with self.app.app_context():
            job_id = self._new_scan_job(
                status="running",
                attempts=1,
                worker_token="live-worker",
                lease_key="global",
                lease_expires_at=now + timedelta(minutes=2),
            )
            lease = self.models.ModelDeletionLease.get_singleton()
            lease.owner_token = "earlier-global-token"
            lease.owner_kind = "post_scan"
            lease.owner_ref = str(job_id)
            lease.acquired_at = now - timedelta(minutes=20)
            lease.heartbeat_at = now - timedelta(minutes=20)
            lease.expires_at = now - timedelta(seconds=1)
            self.db.session.commit()

        self.assertEqual(manager.recover_stale(), 0)
        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            lease = self.models.ModelDeletionLease.get_singleton()
            self.assertEqual(stored.status, "running")
            self.assertEqual(stored.worker_token, "live-worker")
            self.assertEqual(lease.owner_token, "earlier-global-token")

    def test_expired_manual_owner_runs_audit_recovery_before_outbox_claim(self) -> None:
        delete_module = importlib.import_module(PACKAGE + ".delete_service")
        batch_module = importlib.import_module(PACKAGE + ".batch_delete_manager")
        now = datetime.now()
        with self.app.app_context():
            run = self.models.ModelScanRun(
                status="completed",
                server_machine_id="machine-1",
                deletion_attempts=1,
            )
            self.db.session.add(run)
            self.db.session.flush()
            group = self.models.ModelDuplicateGroup(
                run_id=run.id,
                section_key="7",
                rating_key="100",
                media_type="movie",
                identity_fingerprint="fingerprint",
                safe_to_delete=False,
                resolution_status="delete_in_progress",
            )
            self.db.session.add(group)
            self.db.session.flush()
            log = self.models.ModelActionLog(
                run_id=run.id,
                group_id=group.id,
                candidate_id=3,
                keep_candidate_id=4,
                action="delete_media",
                status="deleting",
                message="Plex에 Media 삭제 요청 전송",
            )
            self.db.session.add(log)
            self.db.session.commit()
            log_id = log.id
            group_id = group.id
            job_id = self._new_scan_job(
                action_log_id=log_id,
                run_id=run.id,
                group_id=group_id,
                dedupe_key="manual-recovery-job",
            )
            lease = self.models.ModelDeletionLease.get_singleton()
            lease.owner_token = "expired-manual-token"
            lease.owner_kind = "manual"
            lease.owner_ref = "%s:%s:%s" % (run.id, group_id, 3)
            lease.acquired_at = now - timedelta(minutes=30)
            lease.heartbeat_at = now - timedelta(minutes=30)
            lease.expires_at = now - timedelta(seconds=1)
            self.db.session.commit()

        post_manager = self.post_scan_module.PostDeleteScanManager()
        delete_service = delete_module.DeleteService(post_manager)
        batch_manager = batch_module.BatchDeleteManager(delete_service)
        post_manager.deletion_recovery_callback = batch_manager.recover_interrupted

        self.assertEqual(post_manager.recover_stale(), 0)
        with self.app.app_context():
            recovered_log = self.models.ModelActionLog.get(log_id)
            recovered_group = self.models.ModelDuplicateGroup.get(group_id)
            lease = self.models.ModelDeletionLease.get_singleton()
            self.assertEqual(recovered_log.status, "unknown")
            self.assertEqual(
                recovered_group.resolution_status, "manual_check_required"
            )
            self.assertEqual(lease.owner_token, "")

        claimed_job_id, worker_token, deletion_token = post_manager._claim_next()
        self.assertEqual(claimed_job_id, job_id)
        self.assertTrue(worker_token)
        self.assertTrue(deletion_token)
        self.assertTrue(post_manager.lease_service.release(deletion_token))

    def test_reload_starts_fresh_worker_generation_while_old_thread_is_alive(self) -> None:
        manager = self.post_scan_module.PostDeleteScanManager()
        old_stop = manager._unloading
        old_wake = manager._wake
        old_thread = mock.Mock()
        old_thread.is_alive.return_value = True
        manager._thread = old_thread

        manager.unload()
        self.assertTrue(old_stop.is_set())
        self.assertTrue(old_wake.is_set())
        old_thread.join.assert_called_once_with(timeout=10)

        replacement = mock.Mock()
        with mock.patch.object(manager, "recover_stale", return_value=0), mock.patch.object(
            self.post_scan_module.threading,
            "Thread",
            return_value=replacement,
        ) as thread_factory:
            self.assertEqual(manager.plugin_load(), 0)

        self.assertIs(manager._thread, replacement)
        self.assertIsNot(manager._unloading, old_stop)
        self.assertIsNot(manager._wake, old_wake)
        self.assertFalse(manager._unloading.is_set())
        replacement.start.assert_called_once_with()
        worker_args = thread_factory.call_args.kwargs["args"]
        self.assertIs(worker_args[0], manager._unloading)
        self.assertIs(worker_args[1], manager._wake)

    def test_live_machine_mismatch_is_terminal_blocked_not_retry(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job()

        manager = self.post_scan_module.PostDeleteScanManager()
        connection = types.SimpleNamespace(
            base_url="http://plex.local:32400",
            machine_id="configured-machine",
            token="secret",
        )
        provider = mock.Mock()
        provider.resolve.return_value = connection
        gateway = mock.Mock()
        gateway.validate_identity.return_value = types.SimpleNamespace(
            machine_id="live-other-machine"
        )

        with mock.patch.object(
            self.post_scan_module, "PlexMateProvider", return_value=provider
        ), mock.patch.object(
            self.post_scan_module, "PlexGateway", return_value=gateway
        ):
            self.assertTrue(manager.process_one())

        gateway.validate_identity.assert_called_once_with(
            "configured-machine", require_match=False
        )
        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            lease = self.models.ModelDeletionLease.get_singleton()
            self.assertEqual(stored.status, "blocked")
            self.assertIsNotNone(stored.finished_at)
            self.assertIsNone(stored.lease_key)
            self.assertEqual(stored.worker_token, "")
            self.assertEqual(lease.owner_token, "")

    def test_scan_job_api_never_serializes_worker_or_machine_secrets(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(
                worker_token="internal-worker-token",
                lease_key="global",
                server_machine_id="private-machine-id",
            )
            payload = self.models.ModelPostDeleteScanJob.get(job_id).as_api()

            self.assertNotIn("worker_token", payload)
            self.assertNotIn("lease_key", payload)
            self.assertNotIn("lease_expires_at", payload)
            self.assertNotIn("server_machine_id", payload)
            self.assertNotIn("internal-worker-token", json.dumps(payload))
            self.assertNotIn("private-machine-id", json.dumps(payload))

    @staticmethod
    def _scan_enqueue_values(action_id=11):
        run = types.SimpleNamespace(id=1, server_machine_id="machine-1")
        group = types.SimpleNamespace(
            id=2,
            section_key="7",
            media_type="movie",
        )
        candidate = types.SimpleNamespace(id=3, media_id="10")
        action = types.SimpleNamespace(id=action_id)
        current = MetadataItem(
            rating_key="100",
            guid="plex://movie/outbox-test",
            media_type="movie",
            title="Example",
            media=(
                MediaVersion(
                    media_id="10",
                    duration=1,
                    parts=(
                        MediaPart(
                            "101",
                            "/library/movies/Example/example.mkv",
                        ),
                    ),
                ),
            ),
        )
        return run, group, candidate, action, current

    def test_confirmed_enqueue_joins_callers_transaction_and_rollback_removes_it(self) -> None:
        manager = self.post_scan_module.PostDeleteScanManager()
        with self.app.app_context():
            jobs = manager.enqueue_confirmed(
                *self._scan_enqueue_values(),
                section_locations=["/library/movies"],
                mode="web",
            )

            self.assertEqual(len(jobs), 1)
            self.assertIn(jobs[0], self.db.session.new)
            self.assertIsNone(jobs[0].id)
            self.db.session.rollback()
            self.assertEqual(
                self.db.session.query(self.models.ModelPostDeleteScanJob).count(), 0
            )

    def test_none_mode_does_not_enqueue(self) -> None:
        manager = self.post_scan_module.PostDeleteScanManager()
        with self.app.app_context():
            jobs = manager.enqueue_confirmed(
                *self._scan_enqueue_values(),
                section_locations=["/library/movies"],
                mode="none",
            )

            self.assertEqual(jobs, [])
            self.assertFalse(
                any(
                    isinstance(value, self.models.ModelPostDeleteScanJob)
                    for value in self.db.session.new
                )
            )

    def test_batch_enqueue_deduplicates_target_and_retains_all_action_ids(self) -> None:
        manager = self.post_scan_module.PostDeleteScanManager()
        with self.app.app_context():
            manager.enqueue_confirmed(
                *self._scan_enqueue_values(11),
                section_locations=["/library/movies"],
                mode="web",
                batch_run_id=77,
            )
            # The production delete transaction commits each confirmed item.
            self.db.session.commit()
            manager.enqueue_confirmed(
                *self._scan_enqueue_values(12),
                section_locations=["/library/movies"],
                mode="web",
                batch_run_id=77,
            )
            self.db.session.commit()

            jobs = self.models.ModelPostDeleteScanJob.by_batch(77)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(json.loads(jobs[0].action_ids_json), [11, 12])

    def test_enqueue_rejects_empty_relative_or_outside_live_paths(self) -> None:
        manager = self.post_scan_module.PostDeleteScanManager()
        with self.app.app_context():
            for path in ("", "relative/example.mkv", "/outside/example.mkv"):
                values = list(self._scan_enqueue_values())
                values[-1] = MetadataItem(
                    rating_key="100",
                    guid="plex://movie/outbox-test",
                    media_type="movie",
                    title="Example",
                    media=(
                        MediaVersion(
                            media_id="10",
                            duration=1,
                            parts=(MediaPart("101", path),),
                        ),
                    ),
                )
                with self.subTest(path=path):
                    with self.assertRaises(RuntimeError):
                        manager.enqueue_confirmed(
                            *values,
                            section_locations=["/library/movies"],
                            mode="web",
                        )
                    self.db.session.rollback()

            self.assertEqual(
                self.db.session.query(self.models.ModelPostDeleteScanJob).count(), 0
            )

    def test_binary_without_observable_exit_status_is_never_success(self) -> None:
        manager = self.post_scan_module.PostDeleteScanManager()
        calls = []

        class Settings:
            values = {
                "base_bin_scanner": "/plex/Plex Media Scanner",
                "base_path_metadata": "/plex/config",
                "base_path_program": "/plex/program",
            }

            @classmethod
            def get(cls, key):
                return cls.values.get(key)

        class Scanner:
            result = None

            @classmethod
            def scan_refresh(cls, *args, **kwargs):
                calls.append((args, kwargs))
                return cls.result

        provider = types.SimpleNamespace(
            binary_scanner=lambda: (
                types.SimpleNamespace(ModelSetting=Settings),
                Scanner,
            )
        )
        job = types.SimpleNamespace(
            id=1,
            section_key="7",
            target_path="/library/movies/Example",
        )
        with mock.patch.object(
            self.post_scan_module.os.path, "isfile", return_value=True
        ), mock.patch.object(
            self.post_scan_module.os.path, "isdir", return_value=True
        ), mock.patch.object(
            self.post_scan_module.os, "access", return_value=True
        ), mock.patch.object(
            manager, "_arm_binary_claim", return_value=True
        ) as arm:
            with mock.patch.object(
                self.post_scan_module, "_BINARY_TIMEOUT_SECONDS", 0
            ), self.assertRaises(
                self.post_scan_module.PostDeleteScanQuarantined
            ):
                manager._execute_binary(
                    provider, job, "worker-token", "deletion-token"
                )

            Scanner.result = types.SimpleNamespace(
                process=types.SimpleNamespace(poll=lambda: 1)
            )
            with self.assertRaises(self.post_scan_module.PostDeleteScanRetryable):
                manager._execute_binary(
                    provider, job, "worker-token", "deletion-token"
                )

            Scanner.result = types.SimpleNamespace(
                process=types.SimpleNamespace(poll=lambda: 0)
            )
            self.assertEqual(
                manager._execute_binary(
                    provider, job, "worker-token", "deletion-token"
                ),
                0,
            )

        self.assertEqual(len(calls), 3)
        self.assertEqual(arm.call_count, 3)
        for args, kwargs in calls:
            self.assertEqual(args[:2], (7, "/library/movies/Example"))
            self.assertIsNone(kwargs.get("timeout"))
            self.assertFalse(kwargs.get("join"))

    def test_binary_prearm_failure_never_starts_scanner(self) -> None:
        manager = self.post_scan_module.PostDeleteScanManager()
        spawn_calls = []

        class Settings:
            @classmethod
            def get(cls, key):
                return {
                    "base_bin_scanner": "/plex/Plex Media Scanner",
                    "base_path_metadata": "/plex/config",
                    "base_path_program": "/plex/program",
                }.get(key)

        scanner = types.SimpleNamespace(
            scan_refresh=lambda *args, **kwargs: spawn_calls.append((args, kwargs))
        )
        provider = types.SimpleNamespace(
            binary_scanner=lambda: (
                types.SimpleNamespace(ModelSetting=Settings),
                scanner,
            )
        )
        job = types.SimpleNamespace(
            id=1,
            section_key="7",
            target_path="/library/movies/Example",
        )

        with mock.patch.object(
            self.post_scan_module.os.path, "isfile", return_value=True
        ), mock.patch.object(
            self.post_scan_module.os.path, "isdir", return_value=True
        ), mock.patch.object(
            self.post_scan_module.os, "access", return_value=True
        ), mock.patch.object(
            manager, "_arm_binary_claim", return_value=False
        ), self.assertRaises(self.post_scan_module.PostDeleteScanPrearmFailed):
            manager._execute_binary(
                provider, job, "worker-token", "deletion-token"
            )

        self.assertEqual(spawn_calls, [])

    def _assert_binary_spawn_unknown_is_prearmed(self, scanner) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(mode="binary")
        manager = self.post_scan_module.PostDeleteScanManager()

        class Settings:
            @classmethod
            def get(cls, key):
                return {
                    "base_bin_scanner": "/plex/Plex Media Scanner",
                    "base_path_metadata": "/plex/config",
                    "base_path_program": "/plex/program",
                }.get(key)

        provider = types.SimpleNamespace(
            binary_scanner=lambda: (
                types.SimpleNamespace(ModelSetting=Settings),
                scanner,
            )
        )
        real_arm = manager._arm_binary_claim
        arm_calls = []

        def arm(job, worker, deletion):
            arm_calls.append((job, worker, deletion))
            if len(arm_calls) == 1:
                return real_arm(job, worker, deletion)
            # Simulate a failed follow-up extension. The launch-time arm must
            # already be sufficient to preserve the quarantine.
            return False

        quarantine_started = datetime.now()
        with mock.patch.object(
            manager, "_validated_runtime", return_value=(provider, None, None)
        ), mock.patch.object(
            manager, "_arm_binary_claim", side_effect=arm
        ), mock.patch.object(
            self.post_scan_module.os.path, "isfile", return_value=True
        ), mock.patch.object(
            self.post_scan_module.os.path, "isdir", return_value=True
        ), mock.patch.object(
            self.post_scan_module.os, "access", return_value=True
        ):
            self.assertTrue(manager.process_one())

        self.assertEqual(len(arm_calls), 2)
        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            lease = self.models.ModelDeletionLease.get_singleton()
            self.assertEqual(stored.status, "running")
            self.assertEqual(stored.max_attempts, stored.attempts)
            self.assertEqual(stored.lease_expires_at, lease.expires_at)
            self.assertGreaterEqual(
                lease.expires_at,
                quarantine_started + timedelta(minutes=60),
            )

    def test_binary_none_handle_keeps_launch_time_quarantine(self) -> None:
        self._assert_binary_spawn_unknown_is_prearmed(
            types.SimpleNamespace(scan_refresh=lambda *args, **kwargs: None)
        )

    def test_binary_spawn_exception_keeps_launch_time_quarantine(self) -> None:
        def fail_spawn(*args, **kwargs):
            raise RuntimeError("sentinel spawn failure")

        self._assert_binary_spawn_unknown_is_prearmed(
            types.SimpleNamespace(scan_refresh=fail_spawn)
        )

    def test_known_binary_retry_restores_bounded_retry_budget_atomically(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(mode="binary")
        manager = self.post_scan_module.PostDeleteScanManager()

        def known_retry(job, worker_token, deletion_token):
            self.assertTrue(
                manager._arm_binary_claim(
                    job.id, worker_token, deletion_token
                )
            )
            raise self.post_scan_module.PostDeleteScanRetryable(
                "sentinel confirmed nonzero"
            )

        with mock.patch.object(manager, "_execute", side_effect=known_retry):
            self.assertTrue(manager.process_one())

        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            lease = self.models.ModelDeletionLease.get_singleton()
            self.assertEqual(stored.status, "retry_wait")
            self.assertEqual(stored.attempts, 1)
            self.assertEqual(stored.max_attempts, 3)
            self.assertIsNone(stored.lease_key)
            self.assertEqual(lease.owner_token, "")

    def test_known_binary_success_releases_prearmed_leases(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(mode="binary")
        manager = self.post_scan_module.PostDeleteScanManager()

        def known_success(job, worker_token, deletion_token):
            self.assertTrue(
                manager._arm_binary_claim(
                    job.id, worker_token, deletion_token
                )
            )
            return 0

        with mock.patch.object(manager, "_execute", side_effect=known_success):
            self.assertTrue(manager.process_one())

        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            lease = self.models.ModelDeletionLease.get_singleton()
            self.assertEqual(stored.status, "success")
            self.assertIsNone(stored.lease_key)
            self.assertEqual(lease.owner_token, "")

    def test_scan_completion_callback_never_runs_after_final_lease_proof_is_lost(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(mode="web")
        manager = self.post_scan_module.PostDeleteScanManager()
        completed = []
        manager.completion_callback = lambda job: completed.append(job.id)

        def lose_lease(*args, **kwargs):
            raise self.lease_module.DeletionLeaseLost(
                "recovery owner already claimed the lease"
            )

        with mock.patch.object(manager, "_execute", return_value=200), mock.patch.object(
            manager.lease_service,
            "renew",
            side_effect=lose_lease,
        ):
            self.assertTrue(manager.process_one())

        self.assertEqual(completed, [])
        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            self.assertEqual(stored.status, "running")

    def test_scan_completion_callback_requires_worker_token_cas_proof(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(mode="web")
        manager = self.post_scan_module.PostDeleteScanManager()
        completed = []
        manager.completion_callback = lambda job: completed.append(job.id)

        with mock.patch.object(manager, "_execute", return_value=200), mock.patch.object(
            manager,
            "_renew_job_claim",
            return_value=False,
        ):
            self.assertTrue(manager.process_one())

        self.assertEqual(completed, [])
        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            self.assertEqual(stored.status, "running")

    def test_web_metadata_poll_reuses_one_refresh_until_visible(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(mode="web")
        manager = self.post_scan_module.PostDeleteScanManager()
        refresh_calls = []
        callback_calls = []
        gateway = types.SimpleNamespace(
            refresh_section_path=lambda section, path: (
                refresh_calls.append((section, path)) or 200
            )
        )

        def completion(job):
            callback_calls.append(job.id)
            if len(callback_calls) == 1:
                raise self.post_scan_module.PostDeleteScanRetryable("not visible yet")

        manager.completion_callback = completion
        with mock.patch.object(
            manager, "_validated_runtime", return_value=(None, gateway, None)
        ), mock.patch.object(
            self.post_scan_module, "_WEB_POLL_INTERVAL_SECONDS", 0.0
        ), mock.patch.object(
            self.post_scan_module, "_WEB_POLL_TIMEOUT_SECONDS", 1.0
        ):
            self.assertTrue(manager.process_one())

        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(callback_calls, [job_id, job_id])
        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            self.assertEqual(stored.status, "success")
            self.assertEqual(stored.response_status, 200)

    def test_restored_subtitle_schedules_one_new_web_refresh(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(mode="web")
        manager = self.post_scan_module.PostDeleteScanManager()
        refresh_calls = []
        callback_calls = []
        gateway = types.SimpleNamespace(
            refresh_section_path=lambda section, path: (
                refresh_calls.append((section, path)) or 200
            )
        )

        def completion(job):
            callback_calls.append(job.id)
            if len(callback_calls) == 1:
                raise self.post_scan_module.PostDeleteScanRefreshRequired(
                    "protected subtitle restored"
                )

        manager.completion_callback = completion
        with mock.patch.object(
            manager, "_validated_runtime", return_value=(None, gateway, None)
        ):
            self.assertTrue(manager.process_one())
            with self.app.app_context():
                stored = self.models.ModelPostDeleteScanJob.get(job_id)
                self.assertEqual(stored.status, "retry_wait")
                self.assertIsNone(stored.response_status)
                stored.next_attempt_at = datetime.now() - timedelta(seconds=1)
                self.db.session.commit()
            self.assertTrue(manager.process_one())

        self.assertEqual(len(refresh_calls), 2)
        self.assertEqual(callback_calls, [job_id, job_id])
        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            self.assertEqual(stored.status, "success")
            self.assertEqual(stored.response_status, 200)

    def test_binary_refresh_required_reexecutes_scanner_next_attempt(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(mode="binary")
        manager = self.post_scan_module.PostDeleteScanManager()
        scanner_calls = []
        callback_calls = []

        def execute_binary(provider, job, worker_token, deletion_token):
            scanner_calls.append(job.id)
            return 0

        def completion(job):
            callback_calls.append(job.id)
            if len(callback_calls) == 1:
                raise self.post_scan_module.PostDeleteScanRefreshRequired(
                    "protected subtitle restored"
                )

        manager.completion_callback = completion
        with mock.patch.object(
            manager,
            "_validated_runtime",
            return_value=(types.SimpleNamespace(), None, None),
        ), mock.patch.object(
            manager, "_execute_binary", side_effect=execute_binary
        ):
            self.assertTrue(manager.process_one())
            with self.app.app_context():
                stored = self.models.ModelPostDeleteScanJob.get(job_id)
                self.assertEqual(stored.status, "retry_wait")
                self.assertIsNone(stored.response_status)
                stored.next_attempt_at = datetime.now() - timedelta(seconds=1)
                self.db.session.commit()
            self.assertTrue(manager.process_one())

        self.assertEqual(scanner_calls, [job_id, job_id])
        self.assertEqual(callback_calls, [job_id, job_id])
        with self.app.app_context():
            self.assertEqual(
                self.models.ModelPostDeleteScanJob.get(job_id).status,
                "success",
            )

    def test_terminal_failure_callback_runs_while_both_leases_are_owned(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(mode="web")
        manager = self.post_scan_module.PostDeleteScanManager()
        observed = []

        def failure_callback(job, status, message):
            lease = self.models.ModelDeletionLease.get_singleton()
            stored = self.models.ModelPostDeleteScanJob.get(job.id)
            observed.append(
                (
                    status,
                    lease.owner_kind,
                    lease.owner_ref,
                    bool(lease.owner_token),
                    stored.status,
                    bool(stored.worker_token),
                )
            )

        manager.failure_callback = failure_callback
        blocked = self.post_scan_module.PostDeleteScanBlocked("blocked")
        with mock.patch.object(manager, "_execute", side_effect=blocked):
            self.assertTrue(manager.process_one())

        self.assertEqual(
            observed,
            [("blocked", "post_scan", str(job_id), True, "running", True)],
        )
        with self.app.app_context():
            self.assertEqual(
                self.models.ModelPostDeleteScanJob.get(job_id).status,
                "blocked",
            )
            self.assertEqual(
                self.models.ModelDeletionLease.get_singleton().owner_token,
                "",
            )

    def test_failure_callback_exception_does_not_terminally_close_job(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(mode="web")
        manager = self.post_scan_module.PostDeleteScanManager()

        def failure_callback(job, status, message):
            raise RuntimeError("injected reconciliation failure")

        manager.failure_callback = failure_callback
        blocked = self.post_scan_module.PostDeleteScanBlocked("blocked")
        with mock.patch.object(manager, "_execute", side_effect=blocked):
            try:
                manager.process_one()
            except RuntimeError:
                pass

        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            self.assertEqual(stored.status, "running")
            self.assertTrue(stored.worker_token)

    def test_stale_terminal_recovery_reconciles_pending_quarantine_state(self) -> None:
        now = datetime.now()
        with self.app.app_context():
            job_id = self._new_scan_job(
                status="running",
                attempts=3,
                max_attempts=3,
                worker_token="dead-worker",
                lease_key="global",
                lease_expires_at=now - timedelta(seconds=1),
            )
            lease = self.models.ModelDeletionLease.get_singleton()
            lease.owner_token = "dead-global-token"
            lease.owner_kind = "post_scan"
            lease.owner_ref = str(job_id)
            lease.acquired_at = now - timedelta(minutes=30)
            lease.heartbeat_at = now - timedelta(minutes=30)
            lease.expires_at = now - timedelta(seconds=1)
            self.db.session.commit()

        pending = {
            "journal": "scan_running",
            "action": "scan_running",
            "group": "delete_in_progress",
            "batch": "scan_pending",
        }
        reconciled = []
        manager = self.post_scan_module.PostDeleteScanManager()

        def failure_callback(job, status, message):
            reconciled.append((job.id, status))
            pending.update(
                journal="recovery_required",
                action="unknown",
                group="manual_check_required",
                batch="failed",
            )

        manager.failure_callback = failure_callback
        self.assertEqual(manager.recover_stale(), 1)

        self.assertEqual(reconciled, [(job_id, "failed")])
        self.assertEqual(
            pending,
            {
                "journal": "recovery_required",
                "action": "unknown",
                "group": "manual_check_required",
                "batch": "failed",
            },
        )

    def test_binary_failed_kill_is_quarantined_instead_of_retried(self) -> None:
        manager = self.post_scan_module.PostDeleteScanManager()

        class Settings:
            @classmethod
            def get(cls, key):
                return {
                    "base_bin_scanner": "/plex/Plex Media Scanner",
                    "base_path_metadata": "/plex/config",
                    "base_path_program": "/plex/program",
                }.get(key)

        class Process:
            def __init__(self):
                self.kill_calls = 0

            def poll(self):
                return None

            def kill(self):
                self.kill_calls += 1
                raise OSError("sentinel kill failure")

            def wait(self, timeout=None):
                raise TimeoutError("sentinel child still alive")

        process = Process()

        class Handle:
            thread = None

            def __init__(self):
                self.process = process

            def process_close(self):
                raise OSError("sentinel wrapper close failure")

        scanner = types.SimpleNamespace(scan_refresh=lambda *args, **kwargs: Handle())
        provider = types.SimpleNamespace(
            binary_scanner=lambda: (
                types.SimpleNamespace(ModelSetting=Settings),
                scanner,
            )
        )
        job = types.SimpleNamespace(
            id=1,
            section_key="7",
            target_path="/library/movies/Example",
        )

        with mock.patch.object(
            self.post_scan_module.os.path, "isfile", return_value=True
        ), mock.patch.object(
            self.post_scan_module.os.path, "isdir", return_value=True
        ), mock.patch.object(
            self.post_scan_module.os, "access", return_value=True
        ), mock.patch.object(
            self.post_scan_module, "_BINARY_TIMEOUT_SECONDS", 0
        ), mock.patch.object(
            manager, "_arm_binary_claim", return_value=True
        ), self.assertRaises(self.post_scan_module.PostDeleteScanQuarantined):
            manager._execute_binary(
                provider, job, "worker-token", "deletion-token"
            )

        self.assertEqual(process.kill_calls, 1)

    def test_quarantined_binary_keeps_job_and_global_lease_until_ttl(self) -> None:
        with self.app.app_context():
            job_id = self._new_scan_job(mode="binary")

        manager = self.post_scan_module.PostDeleteScanManager()
        quarantine_started = datetime.now()
        with mock.patch.object(
            manager,
            "_execute",
            side_effect=self.post_scan_module.PostDeleteScanQuarantined(
                "sentinel unknown child"
            ),
        ):
            self.assertTrue(manager.process_one())

        with self.app.app_context():
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            lease = self.models.ModelDeletionLease.get_singleton()
            self.assertEqual(stored.status, "running")
            self.assertEqual(stored.lease_key, "global")
            self.assertTrue(stored.worker_token)
            self.assertIsNotNone(stored.lease_expires_at)
            self.assertIn("격리", stored.last_error)
            self.assertEqual(stored.max_attempts, stored.attempts)
            self.assertEqual(lease.owner_kind, "post_scan")
            self.assertEqual(lease.owner_ref, str(job_id))
            self.assertTrue(lease.owner_token)
            self.assertEqual(lease.expires_at, stored.lease_expires_at)
            self.assertGreaterEqual(
                lease.expires_at,
                quarantine_started + timedelta(minutes=60),
            )

            expired = datetime.now() - timedelta(seconds=1)
            stored.lease_expires_at = expired
            lease.expires_at = expired
            self.db.session.commit()

        self.assertEqual(manager.recover_stale(), 1)
        with self.app.app_context():
            recovered = self.models.ModelPostDeleteScanJob.get(job_id)
            lease = self.models.ModelDeletionLease.get_singleton()
            self.assertEqual(recovered.status, "failed")
            self.assertIsNotNone(recovered.finished_at)
            self.assertEqual(lease.owner_token, "")

    def test_quarantine_commit_failure_rolls_back_both_lease_extensions(self) -> None:
        with self.app.app_context():
            queued_id = self._new_scan_job(mode="binary")

        manager = self.post_scan_module.PostDeleteScanManager()
        job_id, worker_token, deletion_token = manager._claim_next()
        self.assertEqual(job_id, queued_id)
        with self.app.app_context():
            job_before = self.models.ModelPostDeleteScanJob.get(job_id)
            lease_before = self.models.ModelDeletionLease.get_singleton()
            job_expiry = job_before.lease_expires_at
            global_expiry = lease_before.expires_at
            maximum = job_before.max_attempts

        with mock.patch.object(
            self.db.session,
            "commit",
            side_effect=RuntimeError("sentinel commit failure"),
        ):
            self.assertFalse(
                manager._arm_binary_claim(
                    job_id, worker_token, deletion_token
                )
            )

        with self.app.app_context():
            self.db.session.remove()
            stored = self.models.ModelPostDeleteScanJob.get(job_id)
            lease = self.models.ModelDeletionLease.get_singleton()
            self.assertEqual(stored.status, "running")
            self.assertEqual(stored.lease_expires_at, job_expiry)
            self.assertEqual(stored.max_attempts, maximum)
            self.assertEqual(lease.expires_at, global_expiry)
            self.assertEqual(lease.owner_token, deletion_token)

        self.assertTrue(manager.lease_service.release(deletion_token))

    def test_worker_survives_one_iteration_error(self) -> None:
        manager = self.post_scan_module.PostDeleteScanManager()
        calls = []

        def process_one():
            calls.append("call")
            if len(calls) == 1:
                raise RuntimeError("sentinel driver error")
            manager._unloading.set()
            return False

        manager._wake.set()
        with mock.patch.object(manager, "recover_stale", return_value=0), mock.patch.object(
            manager, "process_one", side_effect=process_one
        ):
            thread = threading.Thread(target=manager._worker)
            thread.start()
            thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, ["call", "call"])


if __name__ == "__main__":
    unittest.main()
