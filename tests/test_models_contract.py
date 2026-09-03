from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_pdff_models_contract"


class _Expression:
    def in_(self, values):
        return ("in", tuple(values))

    def desc(self):
        return ("desc", self)

    def __eq__(self, value):
        return ("eq", value)


class _Column(_Expression):
    def __init__(self, *args, default=None, **kwargs):
        del args, kwargs
        self.default = default
        self.name = ""

    def __set_name__(self, owner, name):
        del owner
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        if self.name in instance.__dict__:
            return instance.__dict__[self.name]
        return self.default() if callable(self.default) else self.default

    def __set__(self, instance, value):
        instance.__dict__[self.name] = value


class _Session:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, item):
        if getattr(item, "id", None) is None:
            item.id = len(self.added) + 1
        self.added.append(item)

    def commit(self):
        self.commits += 1


class _DB:
    Integer = int
    BigInteger = int
    DateTime = datetime
    Boolean = bool
    Text = str
    Float = float
    String = staticmethod(lambda length=None: str)
    Column = _Column

    def __init__(self):
        self.session = _Session()


def _load_models():
    sentinel = object()
    saved_modules = {
        key: sys.modules.get(key, sentinel) for key in ("framework", "plugin")
    }
    for key in tuple(sys.modules):
        if key == PACKAGE or key.startswith(PACKAGE + "."):
            del sys.modules[key]

    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package

    db = _DB()
    framework = types.ModuleType("framework")
    framework.F = types.SimpleNamespace(db=db)
    sys.modules["framework"] = framework

    plugin = types.ModuleType("plugin")
    plugin.ModelBase = type("ModelBase", (), {})
    sys.modules["plugin"] = plugin

    setup = types.ModuleType(PACKAGE + ".setup")
    setup.P = types.SimpleNamespace(package_name="plex_dupefinder_ff")
    sys.modules[setup.__name__] = setup

    try:
        spec = importlib.util.spec_from_file_location(
            PACKAGE + ".models", ROOT / "models.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module, db
    finally:
        for key, previous in saved_modules.items():
            if previous is sentinel:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = previous


class ModelContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models, cls.db = _load_models()

    def test_tables_are_plugin_prefixed_and_only_two_models_are_exported(self):
        self.assertEqual(
            self.models.ModelCleanupRun.__tablename__,
            "plex_dupefinder_ff_cleanup_run",
        )
        self.assertEqual(
            self.models.ModelCleanupAction.__tablename__,
            "plex_dupefinder_ff_cleanup_action",
        )
        self.assertEqual(
            set(self.models.__all__),
            {"ACTIVE_RUN_STATUSES", "ModelCleanupRun", "ModelCleanupAction"},
        )

    def test_run_serializer_exposes_ui_status_and_byte_contract(self):
        run = self.models.ModelCleanupRun()
        run.id = 7
        run.mode = "dry_run"
        run.status = "completed"
        run.created_at = datetime(2026, 1, 2, 3, 4, 5)
        run.started_at = datetime(2026, 1, 2, 3, 5, 0)
        run.finished_at = datetime(2026, 1, 2, 3, 6, 0)
        run.stop_requested = False
        run.current_json = json.dumps({"rating_key": "42"})
        run.processed_groups = 2
        run.total_groups = 3
        run.groups_found = 3
        run.would_delete_count = 4
        run.would_delete_bytes = 123456
        run.deleted_count = 0
        run.deleted_bytes = 0
        run.partial_count = 1
        run.error_count = 2
        run.status_message = "완료"
        run.error_message = ""

        payload = run.as_api()
        self.assertEqual(payload["progress"], {"processed": 2, "total": 3})
        self.assertEqual(payload["summary"]["groups"], 3)
        self.assertEqual(payload["summary"]["would_delete"], 4)
        self.assertEqual(payload["summary"]["would_delete_bytes"], 123456)
        self.assertEqual(payload["summary"]["bytes"], 0)
        self.assertEqual(payload["current"], {"rating_key": "42"})

    def test_action_serializer_contains_target_size_paths_and_sidecars(self):
        action = self.models.ModelCleanupAction()
        action.id = 9
        action.run_id = 7
        action.created_at = datetime(2026, 1, 2, 3, 4, 5)
        action.finished_at = datetime(2026, 1, 2, 3, 4, 6)
        action.mode = "live"
        action.section_id = "1"
        action.rating_key = "42"
        action.media_type = "movie"
        action.title = "Example"
        action.keep_media_id = "100"
        action.delete_media_id = "101"
        action.keep_score = 200.0
        action.delete_score = 100.0
        action.file_size = 4096
        action.file_path = "/media/example.mkv"
        action.sidecars_json = '["/media/example.srt"]'
        action.candidate_snapshot_json = '{"media_id":"101"}'
        action.status = "deleted"
        action.response_status = 200
        action.message = "ok"

        payload = action.as_api(include_snapshot=True)
        self.assertEqual(payload["file_size"], 4096)
        self.assertEqual(payload["sidecars"], ["/media/example.srt"])
        self.assertEqual(payload["candidate_snapshot"], {"media_id": "101"})
        for key in (
            "run_id",
            "section_id",
            "rating_key",
            "keep_media_id",
            "delete_media_id",
            "keep_score",
            "delete_score",
            "status",
            "message",
        ):
            self.assertIn(key, payload)

    def test_create_persists_initial_run_and_action_states(self):
        self.db.session.added.clear()
        self.db.session.commits = 0
        run = self.models.ModelCleanupRun.create("live", {"library_ids": ["1"]})
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.mode, "live")
        self.assertEqual(run.would_delete_bytes, 0)
        self.assertGreaterEqual(self.db.session.commits, 1)

        action = self.models.ModelCleanupAction.create(
            run_id=run.id,
            mode="live",
            section_id="1",
            rating_key="42",
            keep_media_id="100",
            delete_media_id="101",
            file_size=99,
            status="deleting",
        )
        self.assertEqual(action.status, "deleting")
        self.assertEqual(action.file_size, 99)
        self.assertEqual(action.as_api()["sidecars"], [])


if __name__ == "__main__":
    unittest.main()
