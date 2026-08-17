from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from test_flaskfarm_compat import FlaskFarmImportHarness, PACKAGE_NAME


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "pdff_history_force_delete_test"


class HistoryForceDeleteIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory(prefix="pdff-history-delete-test-")
        database = Path(cls.tempdir.name) / "history-delete.db"
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
                del inner_cls, key
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

    def _clear(self):
        self.db.session.expunge_all()
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

    def _action(self, status="critical"):
        action = self.models.ModelActionLog(
            run_id=11,
            group_id=12,
            candidate_id=13,
            keep_candidate_id=14,
            action="delete_media",
            status=status,
            message="private error",
            response_status=500,
            before_json='{"private":"before"}',
            after_json='{"private":"after"}',
        )
        self.db.session.add(action)
        self.db.session.flush()
        return action

    def _direct_journal(self, action, status="recovery_required"):
        journal = self.models.ModelDirectDeleteJournal(
            action_log_id=action.id,
            run_id=action.run_id,
            group_id=action.group_id,
            candidate_id=action.candidate_id,
            keep_candidate_id=action.keep_candidate_id,
            operation_key="operation-%s" % action.id,
            status=status,
            plan_digest="a" * 64,
            manifest_json='{"recovery":"must remain"}',
            unlink_json='[{"tombstone_path":"/protected/handoff"}]',
            operation_paths_json='["/protected"]',
            last_error="manual recovery needed",
        )
        self.db.session.add(journal)
        self.db.session.flush()
        return journal

    def _job(self, action, status="blocked", suffix="one"):
        job = self.models.ModelPostDeleteScanJob(
            action_log_id=action.id,
            action_ids_json=json.dumps([action.id]),
            run_id=action.run_id,
            group_id=action.group_id,
            candidate_id=action.candidate_id,
            server_machine_id="machine-secret",
            mode="binary",
            section_key="15",
            media_type="movie",
            target_path="/media/private/movie",
            target_key="target-%s" % suffix,
            dedupe_key="dedupe-%s" % suffix,
            status=status,
            next_attempt_at=datetime.now(),
            last_error="private scan error",
            worker_token="",
        )
        self.db.session.add(job)
        self.db.session.flush()
        return job

    def _live_lease(self, expires_delta=timedelta(minutes=5)):
        lease = self.models.ModelDeletionLease(
            id=1,
            owner_token="secret-token",
            owner_kind="manual",
            owner_ref="11:12:13",
            acquired_at=datetime.now(),
            heartbeat_at=datetime.now(),
            expires_at=datetime.now() + expires_delta,
        )
        self.db.session.add(lease)
        self.db.session.flush()
        return lease

    def test_action_tombstone_hides_row_but_preserves_recovery_dependencies(self):
        with self.app.app_context():
            action = self._action()
            journal = self._direct_journal(action)
            job = self._job(action)
            self.db.session.commit()
            action_id = action.id
            journal_id = journal.id
            job_id = job.id

            result = self.models.ModelActionLog.force_delete_history(action_id)

            self.assertEqual(result["item_type"], "action")
            self.assertFalse(result["files_touched"])
            self.assertFalse(result["plex_called"])
            raw = (
                self.db.session.query(self.models.ModelActionLog)
                .filter_by(id=action_id)
                .one()
            )
            self.assertEqual(raw.status, "history_deleted")
            self.assertEqual(raw.action, "history_deleted")
            self.assertEqual(raw.before_json, "{}")
            self.assertEqual(raw.after_json, "{}")
            self.assertEqual(raw.message, "")
            self.assertEqual(raw.as_api()["history_deleted"], True)
            self.assertEqual(self.models.ModelActionLog.search()["total"], 0)
            self.assertEqual(self.models.ModelActionLog.recent(), [])

            preserved_journal = self.models.ModelDirectDeleteJournal.get(journal_id)
            self.assertEqual(preserved_journal.status, "recovery_required")
            self.assertIn("must remain", preserved_journal.manifest_json)
            self.assertIn("handoff", preserved_journal.unlink_json)
            self.assertEqual(preserved_journal.last_error, "manual recovery needed")
            preserved_job = self.models.ModelPostDeleteScanJob.get(job_id)
            self.assertEqual(preserved_job.status, "blocked")
            self.assertEqual(preserved_job.target_path, "/media/private/movie")

            successor = self._action(status="success")
            self.db.session.commit()
            self.assertGreater(successor.id, action_id)

    def test_action_delete_rejects_active_journal_job_and_batch(self):
        with self.app.app_context():
            cases = ("journal", "job", "batch")
            for case in cases:
                with self.subTest(case=case):
                    self._clear()
                    action = self._action(status="critical")
                    if case == "journal":
                        self._direct_journal(action, status="deleting")
                    elif case == "job":
                        self._job(action, status="running")
                    else:
                        batch = self.models.ModelBatchRun(
                            scan_run_id=action.run_id,
                            expires_at=datetime.now() + timedelta(minutes=5),
                            status="running",
                            lease_key="global",
                            deletion_lease_token="batch-token",
                        )
                        self.db.session.add(batch)
                        self.db.session.flush()
                        journal = self._direct_journal(action, status="recovery_required")
                        journal.batch_run_id = batch.id
                    self.db.session.commit()
                    action_id = action.id

                    with self.assertRaises(RuntimeError):
                        self.models.ModelActionLog.force_delete_history(action_id)

                    raw = self.models.ModelActionLog.get(action_id)
                    self.assertEqual(raw.status, "critical")
                    self.assertEqual(raw.before_json, '{"private":"before"}')

    def test_live_global_lease_blocks_but_expired_lease_does_not(self):
        with self.app.app_context():
            action = self._action(status="success")
            self._live_lease()
            self.db.session.commit()
            with self.assertRaises(RuntimeError):
                self.models.ModelActionLog.force_delete_history(action.id)
            self.assertEqual(self.models.ModelActionLog.get(action.id).status, "success")

            self.db.session.query(self.models.ModelDeletionLease).delete()
            self._live_lease(expires_delta=timedelta(seconds=-1))
            self.db.session.commit()
            result = self.models.ModelActionLog.force_delete_history(action.id)
            self.assertTrue(result["history_deleted"])

    def test_terminal_post_scan_job_becomes_hidden_scrubbed_tombstone(self):
        with self.app.app_context():
            action = self._action(status="blocked")
            job = self._job(action, status="blocked", suffix="terminal")
            self.db.session.commit()
            job_id = job.id
            action_id = action.id
            dedupe_key = job.dedupe_key

            result = self.models.ModelPostDeleteScanJob.force_delete_history(job_id)

            self.assertEqual(result["item_type"], "post_scan")
            self.assertFalse(result["files_touched"])
            self.assertFalse(result["plex_called"])
            raw = self.models.ModelPostDeleteScanJob.get(job_id)
            self.assertEqual(raw.status, "history_deleted")
            self.assertEqual(raw.target_path, "")
            self.assertEqual(raw.server_machine_id, "")
            self.assertEqual(raw.last_error, "")
            self.assertEqual(raw.dedupe_key, dedupe_key)
            self.assertEqual(raw.action_log_id, action_id)
            self.assertEqual(raw.as_api()["history_deleted"], True)
            self.assertEqual(self.models.ModelPostDeleteScanJob.recent(), [])
            self.assertEqual(self.models.ModelActionLog.get(action_id).status, "blocked")

            successor = self._job(action, status="failed", suffix="successor")
            self.db.session.commit()
            self.assertGreater(successor.id, job_id)

    def test_post_scan_search_paginates_every_visible_job_and_clamps_page(self):
        with self.app.app_context():
            action = self._action(status="blocked")
            jobs = [
                self._job(action, status="blocked", suffix="page-%s" % index)
                for index in range(12)
            ]
            self.db.session.commit()

            first = self.models.ModelPostDeleteScanJob.search(
                page=1, page_size=10
            )
            second = self.models.ModelPostDeleteScanJob.search(
                page=999, page_size=10
            )

            self.assertEqual(first["total"], 12)
            self.assertEqual(first["pages"], 2)
            self.assertEqual(first["page"], 1)
            self.assertEqual(
                [item.id for item in first["items"]],
                [item.id for item in reversed(jobs[2:])],
            )
            self.assertEqual(second["page"], 2)
            self.assertEqual(
                [item.id for item in second["items"]],
                [jobs[1].id, jobs[0].id],
            )

            self.models.ModelPostDeleteScanJob.force_delete_history(jobs[0].id)
            stable = self.models.ModelPostDeleteScanJob.search(
                page=999, page_size=10
            )
            self.assertEqual(stable["total"], 11)
            self.assertEqual(stable["page"], 2)
            self.assertEqual([item.id for item in stable["items"]], [jobs[1].id])

    def test_post_scan_delete_rejects_active_or_residual_worker_lease(self):
        with self.app.app_context():
            for status, lease_key, worker_token in (
                ("running", "global", "worker"),
                ("blocked", "global", "worker"),
            ):
                with self.subTest(status=status, residual=bool(lease_key)):
                    self._clear()
                    action = self._action(status="blocked")
                    job = self._job(action, status=status, suffix=status)
                    job.lease_key = lease_key
                    job.worker_token = worker_token
                    job.lease_expires_at = datetime.now() + timedelta(minutes=1)
                    self.db.session.commit()

                    with self.assertRaises(RuntimeError):
                        self.models.ModelPostDeleteScanJob.force_delete_history(job.id)

                    self.assertEqual(
                        self.models.ModelPostDeleteScanJob.get(job.id).status, status
                    )

    def test_post_scan_delete_rejects_active_connected_action_or_journal(self):
        with self.app.app_context():
            for blocker in ("action", "journal"):
                with self.subTest(blocker=blocker):
                    self._clear()
                    action = self._action(
                        status="scan_running" if blocker == "action" else "critical"
                    )
                    job = self._job(action, status="blocked", suffix=blocker)
                    if blocker == "journal":
                        self._direct_journal(action, status="scan_running")
                    self.db.session.commit()

                    with self.assertRaises(RuntimeError):
                        self.models.ModelPostDeleteScanJob.force_delete_history(job.id)

                    self.assertEqual(
                        self.models.ModelPostDeleteScanJob.get(job.id).status,
                        "blocked",
                    )


class HistoryForceDeleteAjaxTest(unittest.TestCase):
    def test_post_scan_status_returns_paginated_server_contract(self):
        with FlaskFarmImportHarness() as harness:
            module = sys.modules[PACKAGE_NAME + ".mod_scan"]
            scan_module = harness.setup_module.P.module_list[1]
            calls = []

            class Row:
                def as_api(self):
                    return {"id": 91, "target_path": "<escaped-by-ui>"}

            class Jobs:
                @classmethod
                def search(cls, page, page_size):
                    calls.append((page, page_size))
                    return {
                        "items": [Row()],
                        "total": 121,
                        "page": 3,
                        "page_size": 50,
                        "pages": 3,
                    }

            module.ModelPostDeleteScanJob = Jobs
            request = types.SimpleNamespace(
                method="GET",
                args={"page": "3", "page_size": "50"},
            )

            response = scan_module.process_ajax(
                "post_delete_scan_status", request
            )

            self.assertEqual(calls, [(3, 50)])
            self.assertEqual(response["ret"], "success")
            self.assertEqual(response["data"]["total"], 121)
            self.assertEqual(response["data"]["pages"], 3)
            self.assertEqual(response["data"]["items"][0]["id"], 91)

    def test_force_delete_requires_post_csrf_exact_type_id_confirmation(self):
        with FlaskFarmImportHarness() as harness:
            module = sys.modules[PACKAGE_NAME + ".mod_history"]
            history_module = harness.setup_module.P.module_list[2]
            calls = []

            class Actions:
                @classmethod
                def force_delete_history(cls, item_id):
                    calls.append(("action", int(item_id)))
                    return {"item_type": "action", "item_id": int(item_id)}

            class Jobs:
                @classmethod
                def force_delete_history(cls, item_id):
                    calls.append(("post_scan", int(item_id)))
                    return {"item_type": "post_scan", "item_id": int(item_id)}

            module.ModelActionLog = Actions
            module.ModelPostDeleteScanJob = Jobs
            module.session["plex_dupefinder_ff_csrf"] = "valid-token"

            def request(method="POST", **overrides):
                form = {
                    "item_type": "action",
                    "item_id": "17",
                    "confirmation": "FORCE DELETE ACTION 17",
                    "csrf_token": "valid-token",
                }
                form.update(overrides)
                return types.SimpleNamespace(method=method, form=form)

            for bad_request in (
                request(method="GET"),
                request(csrf_token="wrong"),
                request(item_type="other"),
                request(item_id="0"),
                request(confirmation="FORCE DELETE ACTION 18"),
            ):
                response, status = history_module.process_ajax(
                    "force_delete", bad_request
                )
                self.assertEqual(status, 400)
                self.assertEqual(response["ret"], "danger")
                self.assertEqual(calls, [])

            response = history_module.process_ajax("force_delete", request())
            self.assertEqual(response["ret"], "success")
            self.assertEqual(response["data"]["item_id"], 17)
            self.assertEqual(calls, [("action", 17)])
            self.assertIn("파일과 Plex", response["msg"])

            response = history_module.process_ajax(
                "force_delete",
                request(
                    item_type="post_scan",
                    item_id="8",
                    confirmation="FORCE DELETE POST_SCAN 8",
                ),
            )
            self.assertEqual(response["ret"], "success")
            self.assertEqual(calls[-1], ("post_scan", 8))

    def test_history_menu_issues_csrf_token(self):
        with FlaskFarmImportHarness() as harness:
            module = sys.modules[PACKAGE_NAME + ".mod_history"]
            history_module = harness.setup_module.P.module_list[2]
            module.session.clear()
            response = history_module.process_menu(
                "list", types.SimpleNamespace(args={})
            )
            token = response["arg"]["csrf_token"]
            self.assertTrue(token)
            self.assertEqual(
                token, module.session["plex_dupefinder_ff_csrf"]
            )


class HistoryForceDeleteUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = (
            PROJECT_ROOT / "templates" / "plex_dupefinder_ff_history_list.html"
        ).read_text(encoding="utf-8")

    def test_each_history_kind_has_a_database_force_delete_control(self):
        self.assertIn('data-type="post_scan"', self.template)
        self.assertIn('data-type="action"', self.template)
        self.assertGreaterEqual(self.template.count("history-force-delete"), 3)
        self.assertIn("DB 강제삭제", self.template)

    def test_post_scan_history_has_independent_stable_pagination(self):
        for element_id in (
            "post_scan_prev_btn",
            "post_scan_next_btn",
            "post_scan_page_label",
            "post_scan_summary",
        ):
            self.assertIn('id="%s"' % element_id, self.template)
        self.assertIn(
            "{page: postScanPage, page_size: postScanPageSize}",
            self.template,
        )
        self.assertIn("postScanPage = Math.max", self.template)
        self.assertIn("postScanTotalPages = Math.max", self.template)
        self.assertIn("loadPostDeleteScans(false)", self.template)

    def test_mutation_is_confirmed_post_csrf_and_uses_only_numeric_id(self):
        handler = self.template.split(
            "$(document).on('click', '.history-force-delete'", 1
        )[1].split("$(document).ready", 1)[0]
        self.assertIn("window.confirm(warning)", handler)
        self.assertIn("삭제 감사 Action", handler)
        self.assertIn("삭제 후 부분 스캔 Job", handler)
        self.assertIn("'FORCE DELETE '", handler)
        self.assertIn("item_type: itemType", handler)
        self.assertIn("item_id: itemId", handler)
        self.assertIn("csrf_token: csrfToken", handler)
        self.assertIn("'POST'", handler)
        self.assertIn("Number.isInteger(itemId)", handler)
        self.assertNotIn("item.target_path", handler)
        self.assertNotIn("item.last_error", handler)

    def test_warning_is_explicitly_database_only_and_preserves_recovery_data(self):
        for phrase in (
            "ID 재사용 방지용 tombstone",
            "연결된 복구 journal과 보호 정보는 보존",
            "미디어·자막·격리·보호 파일",
            "Plex에도 명령을 보내지 않습니다",
            "실행 중이거나 잠긴 작업은 거부",
        ):
            self.assertIn(phrase, self.template)


if __name__ == "__main__":
    unittest.main()
