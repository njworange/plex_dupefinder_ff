from __future__ import annotations

import importlib
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask
from flask_sqlalchemy import SQLAlchemy


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
            self.db.session.query(self.models.ModelBatchItem).delete()
            self.db.session.query(self.models.ModelBatchRun).delete()
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


if __name__ == "__main__":
    unittest.main()
