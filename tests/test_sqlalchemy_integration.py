from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from flask import Flask
from flask_sqlalchemy import SQLAlchemy


ROOT = Path(__file__).resolve().parents[1]


class SqlAlchemyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.saved_modules = {
            name: sys.modules.get(name)
            for name in ("framework", "plugin", "_pdff_sqltest", "_pdff_sqltest.setup")
        }

        app = Flask(__name__)
        app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_BINDS={"plex_dupefinder_ff": "sqlite:///:memory:"},
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        self.app = app
        self.db = SQLAlchemy(app)

        framework = types.ModuleType("framework")
        framework.F = SimpleNamespace(db=self.db)
        plugin = types.ModuleType("plugin")
        plugin.ModelBase = self.db.Model
        package = types.ModuleType("_pdff_sqltest")
        package.__path__ = [str(ROOT)]
        setup = types.ModuleType("_pdff_sqltest.setup")
        setup.P = SimpleNamespace(package_name="plex_dupefinder_ff")
        sys.modules.update(
            {
                "framework": framework,
                "plugin": plugin,
                "_pdff_sqltest": package,
                "_pdff_sqltest.setup": setup,
            }
        )

        spec = importlib.util.spec_from_file_location(
            "_pdff_sqltest.models", ROOT / "models.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        self.models = module

    def tearDown(self):
        with self.app.app_context():
            self.db.session.remove()
        sys.modules.pop("_pdff_sqltest.models", None)
        for name, value in self.saved_modules.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

    def test_real_sqlite_tables_and_serializers(self):
        with self.app.app_context():
            self.db.create_all()
            run = self.models.ModelCleanupRun.create(
                "dry_run", {"library_ids": ["1"]}
            )
            action = self.models.ModelCleanupAction.create(
                run_id=run.id,
                mode="dry_run",
                section_id="1",
                rating_key="20",
                keep_media_id="101",
                delete_media_id="102",
                file_path="/media/Movie.mkv",
                file_size=1234,
                sidecars=["/media/Movie.ko.srt"],
                status="would_delete",
            )

            self.assertGreater(run.id, 0)
            self.assertGreater(action.id, 0)
            self.assertEqual(
                self.models.ModelCleanupRun.get(run.id).as_api()["mode"], "dry_run"
            )
            payload = self.models.ModelCleanupAction.get(action.id).as_api()
            self.assertEqual(payload["file_size"], 1234)
            self.assertEqual(payload["sidecars"], ["/media/Movie.ko.srt"])
            table_names = set(self.db.inspect(self.db.engines["plex_dupefinder_ff"]).get_table_names())
            self.assertEqual(
                table_names,
                {
                    "plex_dupefinder_ff_cleanup_action",
                    "plex_dupefinder_ff_cleanup_run",
                },
            )

    def test_stop_request_only_updates_an_active_run(self):
        with self.app.app_context():
            self.db.create_all()
            run = self.models.ModelCleanupRun.create(
                "live", {"library_ids": ["1"]}
            )
            run.status = "running"
            run.status_message = "실행 중"
            self.db.session.commit()

            self.assertTrue(self.models.ModelCleanupRun.request_stop(run.id))
            self.db.session.expire_all()
            stopping = self.models.ModelCleanupRun.get(run.id)
            self.assertEqual(stopping.status, "stopping")
            self.assertTrue(stopping.stop_requested)

            stopping.status = "stopped"
            stopping.status_message = "사용자 요청으로 중지됨"
            self.db.session.commit()

            self.assertFalse(self.models.ModelCleanupRun.request_stop(run.id))
            self.db.session.expire_all()
            terminal = self.models.ModelCleanupRun.get(run.id)
            self.assertEqual(terminal.status, "stopped")
            self.assertEqual(terminal.status_message, "사용자 요청으로 중지됨")


if __name__ == "__main__":
    unittest.main()
