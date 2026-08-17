from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event

from test_flaskfarm_compat import FlaskFarmImportHarness, PACKAGE_NAME


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "pdff_scan_result_delete_test"


class ScanResultDeletionIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory(prefix="pdff-scan-delete-test-")
        database = Path(cls.tempdir.name) / "scan-delete.db"
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
                return None

        setup.P = types.SimpleNamespace(
            package_name=PACKAGE,
            ModelSetting=Settings,
            logger=types.SimpleNamespace(),
        )
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
        cls.scan_manager = importlib.import_module(PACKAGE + ".scan_manager")
        cls.batch_manager = importlib.import_module(
            PACKAGE + ".batch_delete_manager"
        )
        with cls.app.app_context():
            cls.db.create_all()

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.app.app_context():
            cls.db.session.remove()
            for engine in set(getattr(cls.db, "engines", {}).values()):
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
            self._clear()

    def _clear(self) -> None:
        for model in (
            self.models.ModelPostDeleteScanJob,
            self.models.ModelDirectDeleteJournal,
            self.models.ModelQuarantineJournal,
            self.models.ModelBatchExclusion,
            self.models.ModelBatchItem,
            self.models.ModelBatchRun,
            self.models.ModelActionLog,
            self.models.ModelMediaCandidate,
            self.models.ModelDuplicateGroup,
            self.models.ModelScanRun,
            self.models.ModelDeletionLease,
        ):
            self.db.session.query(model).delete()
        self.db.session.commit()

    def _run(self, status="completed"):
        row = self.models.ModelScanRun(status=status, section_ids_json="[]")
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def _group(self, run_id, suffix):
        row = self.models.ModelDuplicateGroup(
            run_id=run_id,
            section_key="1",
            rating_key="rk-%s" % suffix,
            identity_fingerprint="group-%s" % suffix,
            resolution_status="open",
        )
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def _candidate(self, group_id, suffix):
        row = self.models.ModelMediaCandidate(
            group_id=group_id,
            media_id="media-%s" % suffix,
            fingerprint="candidate-%s" % suffix,
        )
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def _action(self, run_id, group_id=1, candidate_id=1, status="success"):
        row = self.models.ModelActionLog(
            run_id=run_id,
            group_id=group_id,
            candidate_id=candidate_id,
            keep_candidate_id=candidate_id + 1,
            action="delete_media",
            status=status,
        )
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def _batch(self, run_id, status="completed", expires_at=None):
        row = self.models.ModelBatchRun(
            scan_run_id=run_id,
            expires_at=expires_at or datetime.now() - timedelta(minutes=5),
            status=status,
        )
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def _batch_item(self, batch_id, run_id, group_id, candidate_id, status="success"):
        row = self.models.ModelBatchItem(
            batch_run_id=batch_id,
            scan_run_id=run_id,
            group_id=group_id,
            keep_candidate_id=candidate_id + 1,
            delete_candidate_id=candidate_id,
            status=status,
            keep_media_id="keep",
            delete_media_id="delete",
        )
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def _post_job(self, run_id, action_id, status="success"):
        row = self.models.ModelPostDeleteScanJob(
            action_log_id=action_id,
            action_ids_json="[]",
            run_id=run_id,
            group_id=1,
            candidate_id=1,
            server_machine_id="machine",
            mode="web",
            section_key="1",
            media_type="movie",
            target_path="/media/movie",
            target_key="target-%s" % action_id,
            dedupe_key="dedupe-%s-%s" % (run_id, action_id),
            status=status,
            next_attempt_at=datetime.now(),
        )
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def test_all_excluded_batch_review_is_durable_and_non_approvable(self) -> None:
        with self.app.app_context():
            run = self._run("completed")
            self.db.session.commit()
            manager = self.batch_manager.BatchDeleteManager(
                delete_service=types.SimpleNamespace()
            )
            manager._assert_settings_snapshot = lambda value: None

            payload = manager._persist_exclusion_review(
                run,
                "direct",
                [
                    {
                        "group_id": 7,
                        "title": "Unsafe <movie>",
                        "media_type": "movie",
                        "reason": "filesystem_plan_blocked",
                        "message": "보호 계획 확인 필요",
                    }
                ],
            )

            self.assertGreater(payload["plan_id"], 0)
            self.assertEqual(payload["status"], "completed_with_warnings")
            self.assertEqual(payload["backend"], "direct")
            self.assertEqual(payload["total"], 0)
            self.assertFalse(payload["executable"])
            self.assertNotIn("nonce", payload)
            self.assertNotIn("confirmation", payload)
            self.assertEqual(
                payload["excluded_groups"][0]["reason"],
                "filesystem_plan_blocked",
            )
            restored = manager.status(run_id=run.id)
            self.assertEqual(restored["plan_id"], payload["plan_id"])
            self.assertEqual(restored["excluded_groups"], payload["excluded_groups"])
            self.assertEqual(
                self.models.ModelBatchItem.by_batch(payload["plan_id"]), []
            )

    def test_mixed_batch_status_restores_exclusions_without_worker_items(self) -> None:
        with self.app.app_context():
            run = self._run("completed")
            batch = self._batch(run.id, status="preview")
            batch.confirmation = "BATCH DELETE %s ITEMS 1" % batch.id
            batch.total_items = 1
            item = self._batch_item(batch.id, run.id, 3, 30, status="planned")
            exclusion = self.models.ModelBatchExclusion(
                batch_run_id=batch.id,
                scan_run_id=run.id,
                group_id=9,
                title="Tie",
                media_type="movie",
                reason="highest_score_tie",
                message="최고 점수 동률",
            )
            self.db.session.add(exclusion)
            self.db.session.commit()
            manager = self.batch_manager.BatchDeleteManager(
                delete_service=types.SimpleNamespace()
            )

            payload = manager.status(batch_id=batch.id)

            self.assertEqual([value["id"] for value in payload["items"]], [item.id])
            self.assertEqual(payload["total"], 1)
            self.assertEqual(
                payload["excluded_groups"],
                [
                    {
                        "group_id": 9,
                        "title": "Tie",
                        "media_type": "movie",
                        "reason": "highest_score_tie",
                        "message": "최고 점수 동률",
                    }
                ],
            )

    def _journal(self, model, run_id, action_id, status, suffix):
        values = dict(
            action_log_id=action_id,
            run_id=run_id,
            group_id=1,
            candidate_id=1,
            keep_candidate_id=2,
            operation_key="operation-%s" % suffix,
            status=status,
            plan_digest=("a" * 63) + str(suffix)[-1],
            manifest_json="{}",
        )
        if model is self.models.ModelQuarantineJournal:
            row = model(moved_json="[]", backups_json="[]", **values)
        else:
            row = model(unlink_json="[]", operation_paths_json="[]", **values)
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def test_delete_exact_result_rows_and_preserve_every_audit_table(self) -> None:
        with self.app.app_context():
            target = self._run()
            other = self._run()
            target_group = self._group(target.id, "target")
            other_group = self._group(other.id, "other")
            target_candidate = self._candidate(target_group.id, "target-a")
            target_keep = self._candidate(target_group.id, "target-b")
            self._candidate(other_group.id, "other")
            action = self._action(
                target.id,
                target_group.id,
                target_candidate.id,
            )
            batch = self._batch(target.id)
            batch_item = self._batch_item(
                batch.id,
                target.id,
                target_group.id,
                target_candidate.id,
            )
            batch_exclusion = self.models.ModelBatchExclusion(
                batch_run_id=batch.id,
                scan_run_id=target.id,
                group_id=999,
                title="Excluded review",
                media_type="movie",
                reason="unsafe_group",
                message="preserved review reason",
            )
            self.db.session.add(batch_exclusion)
            self._post_job(target.id, action.id)
            self._journal(
                self.models.ModelQuarantineJournal,
                target.id,
                action.id,
                "verified",
                "q1",
            )
            self._journal(
                self.models.ModelDirectDeleteJournal,
                target.id,
                action.id,
                "failed_no_mutation",
                "d2",
            )
            self.db.session.commit()
            target_id = target.id
            other_id = other.id

            data = self.scan_manager.ScanManager().delete_run(target_id)

            self.assertEqual(
                data["deleted"],
                {
                    "run": 1,
                    "run_tombstone": 1,
                    "groups": 1,
                    "candidates": 2,
                },
            )
            self.assertEqual(
                data["preserved"],
                {
                    "action_logs": 1,
                    "batch_runs": 1,
                    "batch_items": 1,
                    "batch_exclusions": 1,
                    "post_delete_scan_jobs": 1,
                    "quarantine_journals": 1,
                    "direct_delete_journals": 1,
                },
            )
            self.assertIsNone(self.models.ModelScanRun.get(target_id))
            tombstone = (
                self.db.session.query(self.models.ModelScanRun)
                .filter_by(id=target_id)
                .one()
            )
            self.assertEqual(tombstone.status, "results_deleted")
            self.assertEqual(
                tombstone.as_api(),
                {
                    "id": target_id,
                    "status": "results_deleted",
                    "results_deleted": True,
                },
            )
            self.assertEqual(tombstone.section_ids_json, "[]")
            self.assertEqual(tombstone.settings_snapshot_json, "{}")
            self.assertEqual(tombstone.server_machine_id, "")
            self.assertEqual(tombstone.total_groups, 0)
            self.assertNotIn(
                target_id,
                [item.id for item in self.models.ModelScanRun.recent(100)],
            )
            self.assertIsNotNone(self.models.ModelScanRun.get(other_id))
            self.assertEqual(
                self.models.ModelBatchExclusion.by_batch(batch.id)[0].message,
                "preserved review reason",
            )
            self.assertIsNone(self.models.ModelDuplicateGroup.get(target_group.id))
            self.assertIsNone(
                self.models.ModelMediaCandidate.get(target_candidate.id)
            )
            self.assertEqual(
                self.models.ModelDuplicateGroup.by_run(target_id), []
            )
            self.assertEqual(
                self.models.ModelDuplicateGroup.all_by_run(target_id), []
            )
            self.assertEqual(
                self.models.ModelDuplicateGroup.search(target_id)["total"], 0
            )
            self.assertEqual(
                self.models.ModelMediaCandidate.by_group(target_group.id), []
            )
            self.assertEqual(
                self.db.session.query(self.models.ModelDuplicateGroup)
                .filter_by(run_id=target_id)
                .count(),
                1,
            )
            raw_group = self.models.ModelDuplicateGroup.get(
                target_group.id, include_results_deleted=True
            )
            raw_candidate = self.models.ModelMediaCandidate.get(
                target_candidate.id, include_results_deleted=True
            )
            raw_keep = self.models.ModelMediaCandidate.get(
                target_keep.id, include_results_deleted=True
            )
            self.assertIsNotNone(raw_group)
            self.assertIsNotNone(raw_candidate)
            self.assertIsNotNone(raw_keep)
            self.assertEqual(raw_group.resolution_status, "results_deleted")
            self.assertFalse(raw_group.safe_to_delete)
            self.assertEqual(raw_group.section_key, "")
            self.assertEqual(raw_group.rating_key, "")
            self.assertEqual(raw_group.guid, "")
            self.assertEqual(raw_group.title, "")
            self.assertEqual(raw_group.identity_fingerprint, "")
            self.assertEqual(raw_group.safety_details_json, "{}")
            self.assertEqual(
                raw_group.as_api(),
                {
                    "id": target_group.id,
                    "run_id": target_id,
                    "status": "results_deleted",
                    "results_deleted": True,
                },
            )
            for raw in (raw_candidate, raw_keep):
                self.assertEqual(raw.media_id, "")
                self.assertEqual(raw.parts_json, "[]")
                self.assertEqual(raw.audio_tracks_json, "[]")
                self.assertEqual(raw.score_breakdown_json, "{}")
                self.assertEqual(raw.total_size, 0)
                self.assertTrue(raw.deleted)
                self.assertEqual(
                    raw.as_api(),
                    {
                        "id": raw.id,
                        "group_id": target_group.id,
                        "status": "results_deleted",
                        "results_deleted": True,
                    },
                )
            self.assertEqual(
                len(
                    self.models.ModelMediaCandidate.by_group(
                        target_group.id,
                        include_results_deleted=True,
                    )
                ),
                2,
            )
            self.assertIsNotNone(self.models.ModelDuplicateGroup.get(other_group.id))
            self.assertEqual(self.db.session.query(self.models.ModelActionLog).count(), 1)
            self.assertEqual(self.db.session.query(self.models.ModelBatchRun).count(), 1)
            self.assertEqual(self.db.session.query(self.models.ModelBatchItem).count(), 1)
            self.assertEqual(
                self.db.session.query(self.models.ModelPostDeleteScanJob).count(), 1
            )
            self.assertEqual(
                self.db.session.query(self.models.ModelQuarantineJournal).count(), 1
            )
            self.assertEqual(
                self.db.session.query(self.models.ModelDirectDeleteJournal).count(), 1
            )
            action_api = self.models.ModelActionLog.get(action.id).as_api()
            self.assertEqual(action_api["run_id"], target_id)
            self.assertEqual(action_api["group_id"], target_group.id)
            self.assertIn("subtitle_cleanup", action_api)
            item_api = self.models.ModelBatchItem.get(batch_item.id).as_api()
            self.assertEqual(item_api["run_id"], target_id)
            self.assertEqual(item_api["group_id"], target_group.id)

    def test_only_explicit_scan_terminal_states_are_allowed(self) -> None:
        with self.app.app_context():
            for status in ("queued", "running", "cancelling", "deleting_results", "future"):
                with self.subTest(status=status):
                    self._clear()
                    run = self._run(status)
                    self.db.session.commit()
                    run_id = run.id
                    with self.assertRaises(RuntimeError):
                        self.models.ModelScanRun.delete_results(run_id)
                    stored = self.models.ModelScanRun.get(run_id)
                    self.assertIsNotNone(stored)
                    self.assertEqual(stored.status, status)

            for status in (
                "completed",
                "completed_with_warnings",
                "cancelled",
                "failed",
                "interrupted",
            ):
                with self.subTest(status=status):
                    self._clear()
                    run = self._run(status)
                    self.db.session.commit()
                    result = self.models.ModelScanRun.delete_results(run.id)
                    self.assertEqual(result["deleted"]["run"], 1)
                    self.assertIsNone(self.models.ModelScanRun.get(run.id))
                    raw = (
                        self.db.session.query(self.models.ModelScanRun)
                        .filter_by(id=run.id)
                        .one()
                    )
                    self.assertEqual(raw.status, "results_deleted")

    def test_tombstones_reserve_every_audit_id_without_cross_link(self) -> None:
        with self.app.app_context():
            for force in (False, True):
                with self.subTest(force=force):
                    self.db.session.expunge_all()
                    self._clear()
                    run = self._run("completed")
                    group = self._group(run.id, "old")
                    candidate = self._candidate(group.id, "old-delete")
                    keep = self._candidate(group.id, "old-keep")
                    action = self._action(
                        run.id,
                        group.id,
                        candidate.id,
                        status="success",
                    )
                    action.keep_candidate_id = keep.id
                    self.db.session.commit()
                    old_ids = (run.id, group.id, candidate.id, keep.id)

                    self.models.ModelScanRun.delete_results(
                        run.id, force=force
                    )
                    successor = self._run("completed")
                    successor_group = self._group(successor.id, "new")
                    successor_candidate = self._candidate(
                        successor_group.id, "new-delete"
                    )
                    successor_keep = self._candidate(
                        successor_group.id, "new-keep"
                    )
                    self.db.session.commit()

                    self.assertGreater(successor.id, old_ids[0])
                    self.assertGreater(successor_group.id, old_ids[1])
                    self.assertGreater(successor_candidate.id, old_ids[2])
                    self.assertGreater(successor_keep.id, old_ids[3])
                    stored_action = self.models.ModelActionLog.get(action.id)
                    self.assertEqual(stored_action.run_id, old_ids[0])
                    self.assertEqual(stored_action.group_id, old_ids[1])
                    self.assertEqual(stored_action.candidate_id, old_ids[2])
                    self.assertEqual(stored_action.keep_candidate_id, old_ids[3])
                    self.assertIsNone(
                        self.models.ModelScanRun.get(old_ids[0])
                    )
                    self.assertIsNone(
                        self.models.ModelDuplicateGroup.get(old_ids[1])
                    )
                    self.assertIsNone(
                        self.models.ModelMediaCandidate.get(old_ids[2])
                    )
                    self.assertEqual(
                        self.models.ModelScanRun.get(
                            old_ids[0], include_results_deleted=True
                        ).status,
                        "results_deleted",
                    )
                    self.assertEqual(
                        self.models.ModelDuplicateGroup.get(
                            old_ids[1], include_results_deleted=True
                        ).resolution_status,
                        "results_deleted",
                    )
                    self.assertTrue(
                        self.models.ModelMediaCandidate.get(
                            old_ids[2], include_results_deleted=True
                        ).deleted
                    )

    def test_active_and_unfinished_deletion_dependencies_block_cleanup(self) -> None:
        with self.app.app_context():
            def future_batch_item(run):
                batch = self._batch(run.id, status="completed")
                return self._batch_item(
                    batch.id, run.id, 1, 1, status="future_item_state"
                )

            blockers = (
                (
                    "active_batch",
                    lambda run: self._batch(run.id, status="running"),
                ),
                (
                    "unexpired_preview",
                    lambda run: self._batch(
                        run.id,
                        status="preview",
                        expires_at=datetime.now() + timedelta(minutes=5),
                    ),
                ),
                (
                    "active_manual_delete",
                    lambda run: self._action(run.id, status="validating"),
                ),
                (
                    "future_action_state",
                    lambda run: self._action(run.id, status="future_action_state"),
                ),
                (
                    "pending_post_scan",
                    lambda run: self._post_job(
                        run.id, self._action(run.id).id, status="queued"
                    ),
                ),
                (
                    "future_post_scan_state",
                    lambda run: self._post_job(
                        run.id,
                        self._action(run.id).id,
                        status="future_post_scan_state",
                    ),
                ),
                ("future_batch_item_state", future_batch_item),
                (
                    "unfinished_quarantine",
                    lambda run: self._journal(
                        self.models.ModelQuarantineJournal,
                        run.id,
                        self._action(run.id).id,
                        "quarantining",
                        "q3",
                    ),
                ),
                (
                    "unfinished_direct",
                    lambda run: self._journal(
                        self.models.ModelDirectDeleteJournal,
                        run.id,
                        self._action(run.id).id,
                        "recovery_required",
                        "d4",
                    ),
                ),
            )
            for label, add_blocker in blockers:
                with self.subTest(blocker=label):
                    self._clear()
                    run = self._run()
                    add_blocker(run)
                    self.db.session.commit()
                    run_id = run.id
                    with self.assertRaises(RuntimeError):
                        self.models.ModelScanRun.delete_results(run_id)
                    stored = self.models.ModelScanRun.get(run_id)
                    self.assertIsNotNone(stored)
                    self.assertEqual(stored.status, "completed")

    def test_force_delete_accepts_terminal_manual_review_journals_only(self) -> None:
        with self.app.app_context():
            run = self._run()
            group = self._group(run.id, "forced-review")
            self._candidate(group.id, "forced-review")
            action = self._action(run.id, group.id, 1, status="unknown")
            direct = self._journal(
                self.models.ModelDirectDeleteJournal,
                run.id,
                action.id,
                "recovery_required",
                "force-direct-7",
            )
            quarantine = self._journal(
                self.models.ModelQuarantineJournal,
                run.id,
                action.id,
                "critical",
                "force-quarantine-8",
            )
            self.db.session.commit()
            run_id = run.id
            direct_id = direct.id
            quarantine_id = quarantine.id

            result = self.models.ModelScanRun.delete_results(run_id, force=True)

            self.assertTrue(result["forced"])
            self.assertEqual(
                result["forced_review_records"],
                {"quarantine_journals": 1, "direct_delete_journals": 1},
            )
            self.assertIsNone(self.models.ModelScanRun.get(run_id))
            self.assertEqual(
                self.models.ModelScanRun.get(
                    run_id, include_results_deleted=True
                ).status,
                "results_deleted",
            )
            self.assertEqual(
                self.models.ModelDirectDeleteJournal.get(direct_id).status,
                "recovery_required",
            )
            self.assertEqual(
                self.db.session.query(self.models.ModelQuarantineJournal)
                .filter_by(id=quarantine_id)
                .one()
                .status,
                "critical",
            )
            self.assertIsNotNone(self.models.ModelActionLog.get(action.id))

            successor = self._run()
            self.db.session.commit()
            self.assertGreater(successor.id, run_id)

    def test_force_delete_accepts_failed_action_as_manual_terminal(self) -> None:
        with self.app.app_context():
            run = self._run()
            action = self._action(run.id, status="failed")
            self.db.session.commit()
            run_id = run.id

            with self.assertRaises(RuntimeError):
                self.models.ModelScanRun.delete_results(run_id)

            result = self.models.ModelScanRun.delete_results(run_id, force=True)

            self.assertTrue(result["forced"])
            self.assertIsNone(self.models.ModelScanRun.get(run_id))
            self.assertEqual(self.models.ModelActionLog.get(action.id).status, "failed")

    def test_force_delete_rejects_unknown_future_action_state(self) -> None:
        with self.app.app_context():
            run = self._run()
            self._action(run.id, status="future_action_state")
            self.db.session.commit()
            run_id = run.id

            with self.assertRaises(RuntimeError):
                self.models.ModelScanRun.delete_results(run_id, force=True)

            stored = self.models.ModelScanRun.get(run_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, "completed")

    def test_force_delete_still_rejects_live_journal_mutation(self) -> None:
        with self.app.app_context():
            run = self._run()
            action = self._action(run.id, status="unknown")
            self._journal(
                self.models.ModelDirectDeleteJournal,
                run.id,
                action.id,
                "deleting",
                "force-live-9",
            )
            self.db.session.commit()
            run_id = run.id

            with self.assertRaises(RuntimeError):
                self.models.ModelScanRun.delete_results(run_id, force=True)

            stored = self.models.ModelScanRun.get(run_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, "completed")

    def test_force_delete_rejects_valid_global_deletion_lease(self) -> None:
        with self.app.app_context():
            run = self._run()
            self.db.session.add(
                self.models.ModelDeletionLease(
                    id=1,
                    owner_token="live-owner",
                    owner_kind="post_scan",
                    owner_ref="17",
                    expires_at=datetime.now() + timedelta(minutes=1),
                )
            )
            self.db.session.commit()
            run_id = run.id

            with self.assertRaisesRegex(RuntimeError, "전역 잠금"):
                self.models.ModelScanRun.delete_results(run_id, force=True)

            stored = self.models.ModelScanRun.get(run_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, "completed")

    def test_force_delete_does_not_treat_expired_lease_as_live_worker(self) -> None:
        with self.app.app_context():
            run = self._run()
            action = self._action(run.id, status="unknown")
            self._journal(
                self.models.ModelDirectDeleteJournal,
                run.id,
                action.id,
                "recovery_required",
                "force-expired-0",
            )
            self.db.session.add(
                self.models.ModelDeletionLease(
                    id=1,
                    owner_token="expired-owner",
                    owner_kind="manual",
                    owner_ref="old",
                    expires_at=datetime.now() - timedelta(seconds=1),
                )
            )
            self.db.session.commit()

            result = self.models.ModelScanRun.delete_results(run.id, force=True)

            self.assertTrue(result["forced"])
            self.assertIsNone(self.models.ModelScanRun.get(run.id))

    def test_expired_preview_planned_items_are_preserved_but_not_active(self) -> None:
        with self.app.app_context():
            run = self._run()
            group = self._group(run.id, "expired-preview")
            candidate = self._candidate(group.id, "expired-preview")
            batch = self._batch(
                run.id,
                status="preview",
                expires_at=datetime.now() - timedelta(seconds=1),
            )
            item = self._batch_item(
                batch.id,
                run.id,
                group.id,
                candidate.id,
                status="planned",
            )
            self._journal(
                self.models.ModelDirectDeleteJournal,
                run.id,
                None,
                "batch_preview",
                "expired5",
            ).batch_run_id = batch.id
            self.db.session.commit()
            run_id = run.id
            batch_id = batch.id
            item_id = item.id

            data = self.models.ModelScanRun.delete_results(run_id)

            self.assertEqual(data["deleted"]["run"], 1)
            self.assertEqual(data["preserved"]["batch_runs"], 1)
            self.assertEqual(data["preserved"]["batch_items"], 1)
            self.assertIsNotNone(self.models.ModelBatchRun.get(batch_id))
            self.assertIsNotNone(self.models.ModelBatchItem.get(item_id))
            self.assertEqual(
                self.db.session.query(self.models.ModelDirectDeleteJournal)
                .filter_by(run_id=run_id, status="batch_preview")
                .count(),
                1,
            )

    def test_explicitly_expired_batch_planned_items_are_inactive(self) -> None:
        with self.app.app_context():
            run = self._run()
            group = self._group(run.id, "expired-batch")
            candidate = self._candidate(group.id, "expired-batch")
            batch = self._batch(run.id, status="expired")
            item = self._batch_item(
                batch.id,
                run.id,
                group.id,
                candidate.id,
                status="planned",
            )
            self._journal(
                self.models.ModelDirectDeleteJournal,
                run.id,
                None,
                "batch_preview",
                "expired6",
            ).batch_run_id = batch.id
            self.db.session.commit()
            run_id = run.id

            data = self.models.ModelScanRun.delete_results(run_id)

            self.assertEqual(data["deleted"]["run_tombstone"], 1)
            self.assertIsNotNone(self.models.ModelBatchRun.get(batch.id))
            self.assertIsNotNone(self.models.ModelBatchItem.get(item.id))
            self.assertIsNone(self.models.ModelScanRun.get(run_id))
            self.assertEqual(
                self.models.ModelScanRun.get(
                    run_id, include_results_deleted=True
                ).status,
                "results_deleted",
            )

    def test_completed_batch_warning_and_error_states_are_safe_audit_terminals(self) -> None:
        with self.app.app_context():
            for batch_status in (
                "completed_with_warnings",
                "completed_with_errors",
            ):
                with self.subTest(batch_status=batch_status):
                    self._clear()
                    run = self._run("completed")
                    self._batch(run.id, status=batch_status)
                    self.db.session.commit()

                    result = self.models.ModelScanRun.delete_results(run.id)

                    self.assertEqual(result["deleted"]["run_tombstone"], 1)
                    self.assertIsNone(self.models.ModelScanRun.get(run.id))

    def test_database_error_rolls_back_every_result_delete(self) -> None:
        with self.app.app_context():
            run = self._run()
            group = self._group(run.id, "rollback")
            self._candidate(group.id, "rollback")
            self.db.session.commit()
            run_id = run.id
            group_id = group.id
            engine = self.db.engines[PACKAGE]

            def fail_group_tombstone(
                connection, cursor, statement, parameters, context, executemany
            ):
                del connection, cursor, parameters, context, executemany
                normalized = " ".join(str(statement).lower().split())
                if normalized.startswith("update duplicate_group"):
                    raise RuntimeError("injected tombstone failure")

            event.listen(engine, "before_cursor_execute", fail_group_tombstone)
            try:
                with self.assertRaisesRegex(
                    RuntimeError, "injected tombstone failure"
                ):
                    self.models.ModelScanRun.delete_results(run_id)
            finally:
                event.remove(
                    engine, "before_cursor_execute", fail_group_tombstone
                )

            stored = self.models.ModelScanRun.get(run_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, "completed")
            self.assertIsNotNone(self.models.ModelDuplicateGroup.get(group_id))
            self.assertEqual(
                self.db.session.query(self.models.ModelMediaCandidate)
                .filter_by(group_id=group_id)
                .count(),
                1,
            )

    def test_cleanup_claim_and_tombstone_are_both_status_cas_guarded(self) -> None:
        with self.app.app_context():
            run = self._run("completed")
            self.db.session.commit()
            engine = self.db.engines[PACKAGE]
            statements = []

            def capture(connection, cursor, statement, parameters, context, executemany):
                del connection, cursor, parameters, context, executemany
                normalized = " ".join(str(statement).lower().split())
                if normalized.startswith("update scan_run") or normalized.startswith(
                    "delete from scan_run"
                ):
                    statements.append(normalized)

            event.listen(engine, "before_cursor_execute", capture)
            try:
                self.models.ModelScanRun.delete_results(run.id)
            finally:
                event.remove(engine, "before_cursor_execute", capture)

            updates = [value for value in statements if value.startswith("update")]
            self.assertEqual(len(updates), 2)
            self.assertFalse(
                any(value.startswith("delete from scan_run") for value in statements)
            )
            for statement in updates:
                self.assertIn("scan_run.id =", statement)
                self.assertIn("scan_run.status =", statement)


class ScanResultDeletionAjaxTest(unittest.TestCase):
    def test_delete_recovery_runs_only_after_post_and_csrf_validation(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = sys.modules[PACKAGE_NAME + ".mod_scan"]
            scan_module = harness.setup_module.P.module_list[1]
            recovery_calls = []

            class BatchManagerStub:
                def recover_interrupted(self):
                    recovery_calls.append("recover")
                    return 0

                def status(self, batch_id=None, run_id=None):
                    return {
                        "plan_id": int(batch_id or 1),
                        "run_id": run_id,
                        "status": "completed",
                    }

                def preview(self, run_id):
                    return {
                        "plan_id": 31,
                        "nonce": "batch-nonce",
                        "expires_at": 9999999999,
                        "run_id": int(run_id),
                    }

                def approve(self, batch_id, nonce, confirmation):
                    return {
                        "plan_id": int(batch_id),
                        "nonce_seen": nonce,
                        "confirmation_seen": confirmation,
                        "status": "queued",
                    }

                def cancel(self, batch_id):
                    return {"plan_id": int(batch_id), "status": "cancelled"}

            group = types.SimpleNamespace(
                id=11, safe_to_delete=True, resolution_status="open"
            )
            candidate = types.SimpleNamespace(
                id=21, group_id=11, deleted=False
            )
            keep = types.SimpleNamespace(id=22, group_id=11, deleted=False)

            class GroupModel:
                @classmethod
                def get(cls, group_id):
                    return group if int(group_id) == group.id else None

            class CandidateModel:
                @classmethod
                def get(cls, candidate_id):
                    return {
                        candidate.id: candidate,
                        keep.id: keep,
                    }.get(int(candidate_id))

            class DeleteServiceStub:
                def preview(self, group_id, candidate_id, keep_candidate_id):
                    return {
                        "confirmation": "DELETE media-21",
                        "backend": "plex",
                        "plan_digest": "",
                        "group_id": int(group_id),
                        "candidate_id": int(candidate_id),
                        "keep_candidate_id": int(keep_candidate_id),
                    }

                def delete(self, **kwargs):
                    return {"verification": "verified", "request": kwargs}

            scan_module.batch_manager = BatchManagerStub()
            scan_module.delete_service = DeleteServiceStub()
            module.ModelDuplicateGroup = GroupModel
            module.ModelMediaCandidate = CandidateModel
            harness.setup_module.P.ModelSetting.set("setting_delete_enabled", "True")
            module.session["plex_dupefinder_ff_csrf"] = "valid-token"

            status_request = types.SimpleNamespace(
                method="GET",
                values={"plan_id": "31"},
                form={},
            )
            payload = scan_module.process_ajax("batch_status", status_request)
            self.assertEqual(payload["ret"], "success")
            self.assertEqual(recovery_calls, [])

            wrong_method = types.SimpleNamespace(
                method="GET",
                values={},
                form={"plan_id": "31", "csrf_token": "valid-token"},
            )
            payload, status = scan_module.process_ajax("batch_cancel", wrong_method)
            self.assertEqual(status, 400)
            self.assertEqual(payload["ret"], "danger")
            self.assertEqual(recovery_calls, [])

            wrong_csrf = types.SimpleNamespace(
                method="POST",
                values={},
                form={"run_id": "7", "csrf_token": "wrong-token"},
            )
            payload, status = scan_module.process_ajax("batch_preview", wrong_csrf)
            self.assertEqual(status, 400)
            self.assertEqual(payload["ret"], "danger")
            self.assertEqual(recovery_calls, [])

            valid_mutations = [
                (
                    "batch_preview",
                    {"run_id": "7", "csrf_token": "valid-token"},
                ),
                (
                    "batch_approve",
                    {
                        "plan_id": "31",
                        "nonce": "batch-nonce",
                        "confirmation": "server-confirmation",
                        "csrf_token": "valid-token",
                    },
                ),
                (
                    "batch_cancel",
                    {"plan_id": "31", "csrf_token": "valid-token"},
                ),
                (
                    "delete_preview",
                    {
                        "group_id": "11",
                        "candidate_id": "21",
                        "keep_candidate_id": "22",
                        "csrf_token": "valid-token",
                    },
                ),
                (
                    "delete_media",
                    {
                        "group_id": "11",
                        "candidate_id": "21",
                        "keep_candidate_id": "22",
                        "nonce": "delete-nonce",
                        "plan_digest": "",
                        "confirmation": "DELETE media-21",
                        "csrf_token": "valid-token",
                    },
                ),
            ]
            for expected_count, (sub, form) in enumerate(valid_mutations, 1):
                if sub == "batch_approve":
                    module.session["plex_dupefinder_ff_batch_preview"] = {
                        "plan_id": 31,
                        "nonce": "batch-nonce",
                        "expires_at": 9999999999,
                    }
                elif sub == "delete_media":
                    module.session["plex_dupefinder_ff_delete_preview"] = {
                        "nonce": "delete-nonce",
                        "group_id": 11,
                        "candidate_id": 21,
                        "keep_candidate_id": 22,
                        "plan_digest": "",
                        "backend": "plex",
                        "expires_at": 9999999999,
                    }
                request = types.SimpleNamespace(
                    method="POST", values=dict(form), form=dict(form)
                )
                payload = scan_module.process_ajax(sub, request)
                self.assertEqual(payload["ret"], "success", sub)
                self.assertEqual(len(recovery_calls), expected_count, sub)

    def test_delete_scan_requires_post_and_csrf_before_manager_call(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = sys.modules[PACKAGE_NAME + ".mod_scan"]
            scan_module = harness.setup_module.P.module_list[1]
            calls = []
            scan_module.manager = types.SimpleNamespace(
                delete_run=lambda run_id: calls.append(run_id)
                or {
                    "run_id": run_id,
                    "deleted": {"run": 1, "groups": 0, "candidates": 0},
                    "preserved": {},
                }
            )
            module.session["plex_dupefinder_ff_csrf"] = "valid-token"

            bad = types.SimpleNamespace(
                method="POST",
                form={"run_id": "7", "csrf_token": "wrong-token"},
            )
            payload, status = scan_module.process_ajax("delete_scan", bad)
            self.assertEqual(status, 400)
            self.assertEqual(payload["ret"], "danger")
            self.assertEqual(calls, [])

            get_request = types.SimpleNamespace(
                method="GET",
                form={"run_id": "7", "csrf_token": "valid-token"},
            )
            payload, status = scan_module.process_ajax("delete_scan", get_request)
            self.assertEqual(status, 400)
            self.assertEqual(payload["ret"], "danger")
            self.assertEqual(calls, [])

            valid = types.SimpleNamespace(
                method="POST",
                form={"run_id": "7", "csrf_token": "valid-token"},
            )
            payload = scan_module.process_ajax("delete_scan", valid)
            self.assertEqual(payload["ret"], "success")
            self.assertEqual(payload["data"]["run_id"], 7)
            self.assertEqual(calls, [7])
            self.assertIn("journal", payload["msg"])

    def test_force_delete_scan_requires_exact_server_side_confirmation(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = sys.modules[PACKAGE_NAME + ".mod_scan"]
            scan_module = harness.setup_module.P.module_list[1]
            calls = []

            def delete_run(run_id, force=False):
                calls.append((int(run_id), bool(force)))
                return {"run_id": int(run_id), "forced": bool(force)}

            scan_module.manager = types.SimpleNamespace(delete_run=delete_run)
            module.session["plex_dupefinder_ff_csrf"] = "valid-token"

            missing = types.SimpleNamespace(
                method="POST",
                form={
                    "run_id": "17",
                    "csrf_token": "valid-token",
                    "force": "1",
                },
            )
            payload, status = scan_module.process_ajax("delete_scan", missing)
            self.assertEqual(status, 400)
            self.assertEqual(payload["ret"], "danger")
            self.assertEqual(calls, [])

            wrong = types.SimpleNamespace(
                method="POST",
                form={
                    "run_id": "17",
                    "csrf_token": "valid-token",
                    "force": "1",
                    "confirmation": "FORCE DELETE SCAN 18",
                },
            )
            payload, status = scan_module.process_ajax("delete_scan", wrong)
            self.assertEqual(status, 400)
            self.assertEqual(payload["ret"], "danger")
            self.assertEqual(calls, [])

            valid = types.SimpleNamespace(
                method="POST",
                form={
                    "run_id": "17",
                    "csrf_token": "valid-token",
                    "force": "1",
                    "confirmation": "FORCE DELETE SCAN 17",
                },
            )
            payload = scan_module.process_ajax("delete_scan", valid)
            self.assertEqual(payload["ret"], "success")
            self.assertTrue(payload["data"]["forced"])
            self.assertIn("강제로", payload["msg"])
            self.assertEqual(calls, [(17, True)])

    def test_explicit_deleted_run_status_does_not_fall_back_to_another_run(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = sys.modules[PACKAGE_NAME + ".mod_scan"]
            scan_module = harness.setup_module.P.module_list[1]
            calls = []

            class Runs:
                @classmethod
                def get(cls, run_id):
                    calls.append(("get", int(run_id)))
                    return None

                @classmethod
                def active(cls):
                    calls.append(("active", None))
                    raise AssertionError("explicit run lookup must not use active")

                @classmethod
                def recent(cls, limit):
                    calls.append(("recent", limit))
                    raise AssertionError("explicit deleted run must not fall back")

            module.ModelScanRun = Runs
            request = types.SimpleNamespace(
                method="GET",
                values={"run_id": "7"},
                form={},
            )

            payload = scan_module.process_ajax("status", request)

            self.assertEqual(payload, {"ret": "success", "data": None})
            self.assertEqual(calls, [("get", 7)])


if __name__ == "__main__":
    unittest.main()
