from __future__ import annotations

import importlib.util
import json
import sys
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_pdff_module_contract"


class _Logger:
    def __getattr__(self, name):
        del name
        return lambda *args, **kwargs: None


class _Settings:
    values = {}

    @classmethod
    def get(cls, key):
        return cls.values.get(key)

    @classmethod
    def to_dict(cls):
        return dict(cls.values)

    @classmethod
    def set(cls, key, value):
        cls.values[key] = value

    @classmethod
    def set(cls, key, value):
        cls.values[key] = value


class _Session:
    def add(self, item):
        del item

    def commit(self):
        pass

    def rollback(self):
        pass

    def remove(self):
        pass


class _App:
    @contextmanager
    def app_context(self):
        yield


class _PluginModuleBase:
    def __init__(self, plugin, name=None, first_menu=None, scheduler_desc=None):
        self.plugin = plugin
        self.name = name
        self.first_menu = first_menu
        self.scheduler_desc = scheduler_desc

    def get_scheduler_id(self):
        return "%s_%s" % (self.plugin.package_name, self.name)


def _load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_modules():
    sentinel = object()
    saved_modules = {
        key: sys.modules.get(key, sentinel) for key in ("flask", "plugin", "framework")
    }
    for key in tuple(sys.modules):
        if key == PACKAGE or key.startswith(PACKAGE + "."):
            del sys.modules[key]

    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package

    flask = types.ModuleType("flask")
    flask.jsonify = lambda payload: payload
    flask.render_template = lambda template, **kwargs: (template, kwargs)
    sys.modules["flask"] = flask

    plugin = types.ModuleType("plugin")
    plugin.PluginModuleBase = _PluginModuleBase
    sys.modules["plugin"] = plugin

    framework = types.ModuleType("framework")
    framework.F = types.SimpleNamespace(
        app=_App(),
        db=types.SimpleNamespace(session=_Session()),
        PluginManager=types.SimpleNamespace(),
    )
    sys.modules["framework"] = framework

    p = types.SimpleNamespace(
        package_name="plex_dupefinder_ff",
        ModelSetting=_Settings,
        logger=_Logger(),
    )
    setup = types.ModuleType(PACKAGE + ".setup")
    setup.P = p
    sys.modules[setup.__name__] = setup

    models = types.ModuleType(PACKAGE + ".models")
    models.ModelCleanupRun = type("ModelCleanupRun", (), {})
    models.ModelCleanupAction = type("ModelCleanupAction", (), {})
    sys.modules[models.__name__] = models

    try:
        setting = _load_file(PACKAGE + ".mod_setting", ROOT / "mod_setting.py")
        cleanup = _load_file(PACKAGE + ".mod_cleanup", ROOT / "mod_cleanup.py")
        history = _load_file(PACKAGE + ".mod_history", ROOT / "mod_history.py")
        return setting, cleanup, history, p, framework.F
    finally:
        for key, previous in saved_modules.items():
            if previous is sentinel:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = previous


class _FakeRun:
    def __init__(self, mode):
        self.id = 1
        self.mode = mode
        self.status = "queued"
        self.created_at = None
        self.started_at = None
        self.finished_at = None
        self.stop_requested = False
        self.current_json = "{}"
        self.processed_groups = 0
        self.total_groups = 0
        self.groups_found = 0
        self.would_delete_count = 0
        self.would_delete_bytes = 0
        self.deleted_count = 0
        self.deleted_bytes = 0
        self.partial_count = 0
        self.error_count = 0
        self.status_message = ""
        self.error_message = ""

    def as_api(self):
        return {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "started_at": (
                self.started_at.isoformat(timespec="seconds")
                if self.started_at
                else None
            ),
            "stop_requested": self.stop_requested,
            "current": json.loads(self.current_json or "{}"),
            "progress": {
                "processed": self.processed_groups,
                "total": self.total_groups,
            },
            "summary": {
                "groups": self.groups_found,
                "would_delete": self.would_delete_count,
                "would_delete_bytes": self.would_delete_bytes,
                "deleted": self.deleted_count,
                "bytes": self.deleted_bytes,
                "partial": self.partial_count,
                "errors": self.error_count,
            },
            "message": self.error_message or self.status_message,
        }


class _FakeRunModel:
    current = None

    @classmethod
    def get(cls, run_id):
        return cls.current if cls.current and cls.current.id == run_id else None

    @classmethod
    def request_stop(cls, run_id):
        run = cls.get(run_id)
        if run is None or run.status not in ("queued", "running", "stopping"):
            return False
        run.stop_requested = True
        run.status = "stopping"
        run.status_message = "중지 요청됨"
        return True


class _FakeAction:
    def __init__(self, action_id, values):
        self.id = action_id
        self.created_at = None
        self.finished_at = None
        self.response_status = values.get("response_status")
        for key, value in values.items():
            setattr(self, key, value)

    def as_api(self):
        return {
            "id": self.id,
            "run_id": self.run_id,
            "mode": self.mode,
            "section_id": self.section_id,
            "rating_key": self.rating_key,
            "keep_media_id": self.keep_media_id,
            "delete_media_id": self.delete_media_id,
            "file_size": self.file_size,
            "file_path": self.file_path,
            "sidecars": list(self.sidecars),
            "status": self.status,
            "response_status": self.response_status,
            "message": self.message,
        }


class _FakeActionModel:
    items = []
    create_entered = None
    create_release = None

    @classmethod
    def create(cls, **values):
        values.setdefault("sidecars", [])
        values.setdefault("message", "")
        item = _FakeAction(len(cls.items) + 1, values)
        cls.items.append(item)
        if cls.create_entered is not None:
            cls.create_entered.set()
            if not cls.create_release.wait(5):
                raise RuntimeError("action-create test gate timed out")
        return item


def _candidate(media_id, path, size, score):
    return types.SimpleNamespace(
        media_id=str(media_id),
        paths=(str(path),),
        parts=(),
        total_size=size,
        duration=100,
        bitrate=1000,
        width=1920,
        height=1080,
        video_resolution="1080",
        video_codec="h264",
        audio_codec="aac",
        audio_channels=2.0,
        audio_tracks=(),
        best_audio_channels=2.0,
        container="mkv",
        test_score=float(score),
    )


def _group(keep, *duplicates):
    return types.SimpleNamespace(
        rating_key="42",
        title="Example",
        media_type="movie",
        candidates=(keep,) + tuple(duplicates),
    )


class _Adapter:
    def __init__(
        self,
        group,
        *,
        delete_error=None,
        media_exists=False,
        sidecars=None,
        post_delete_group=None,
        pre_delete_entered=None,
        pre_delete_release=None,
        delete_entered=None,
        delete_release=None,
    ):
        self.group = group
        self.delete_error = delete_error
        self.exists_after_delete = media_exists
        self.sidecars = sidecars or {}
        self.post_delete_group = post_delete_group
        self.pre_delete_entered = pre_delete_entered
        self.pre_delete_release = pre_delete_release
        self.delete_entered = delete_entered
        self.delete_release = delete_release
        self.delete_calls = []
        self.sidecar_delete_calls = []
        self.get_calls = 0

    def iter_duplicate_groups(self, section_id, cancel_check=None):
        self.section_id = section_id
        if cancel_check is None or not cancel_check():
            yield self.group

    def rank(self, group):
        keep = group.candidates[0]
        duplicates = tuple(group.candidates[1:])
        ranked = tuple(
            types.SimpleNamespace(
                candidate=item,
                score=types.SimpleNamespace(total=item.test_score),
            )
            for item in (keep,) + duplicates
        )
        return types.SimpleNamespace(
            keep=keep,
            delete_candidates=duplicates,
            ranked=ranked,
            scores=tuple(item.score for item in ranked),
        )

    def get_group(self, rating_key):
        self.get_calls += 1
        self.rating_key = rating_key
        if self.get_calls == 1 and self.pre_delete_entered is not None:
            self.pre_delete_entered.set()
            if not self.pre_delete_release.wait(5):
                raise RuntimeError("pre-delete test gate timed out")
        if self.delete_calls:
            if self.post_delete_group is not None:
                return self.post_delete_group
            if not self.exists_after_delete:
                deleted_ids = {media_id for _, media_id in self.delete_calls}
                return types.SimpleNamespace(
                    rating_key=self.group.rating_key,
                    title=self.group.title,
                    media_type=self.group.media_type,
                    candidates=tuple(
                        item
                        for item in self.group.candidates
                        if item.media_id not in deleted_ids
                    ),
                )
        return self.group

    def find_sidecars(self, candidate):
        return tuple(self.sidecars.get(candidate.media_id, ()))

    def delete_media(self, rating_key, media_id):
        self.delete_calls.append((rating_key, media_id))
        if self.delete_entered is not None:
            self.delete_entered.set()
            if not self.delete_release.wait(5):
                raise RuntimeError("delete test gate timed out")
        if self.delete_error is not None:
            raise self.delete_error
        return types.SimpleNamespace(status_code=200)

    def media_exists(self, rating_key, media_id):
        self.verify_call = (rating_key, media_id)
        return self.exists_after_delete

    def delete_sidecars(self, paths):
        self.sidecar_delete_calls.append(tuple(paths))
        return types.SimpleNamespace(failed=())


class ModuleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.setting, cls.cleanup, cls.history, cls.p, cls.f = _load_modules()

    def setUp(self):
        self.p.ModelSetting.values = dict(self.setting.ModuleSetting.db_default)
        if hasattr(self.p, "logic"):
            delattr(self.p, "logic")
        _FakeActionModel.items = []
        _FakeActionModel.create_entered = None
        _FakeActionModel.create_release = None

    def _run_worker(self, mode, adapter):
        run = _FakeRun(mode)
        _FakeRunModel.current = run
        module = self.cleanup.ModuleCleanup(self.p)
        module.adapter_factory = lambda config: adapter
        with mock.patch.object(self.cleanup, "ModelCleanupRun", _FakeRunModel), mock.patch.object(
            self.cleanup, "ModelCleanupAction", _FakeActionModel
        ):
            module._worker(run.id, mode, {"library_ids": ["1"]})
        return run, module, list(_FakeActionModel.items)

    def test_setting_keys_and_runtime_parsing_match_ui(self):
        expected = {
            "setting_library_id",
            "setting_score_json",
            "setting_filename_score",
            "setting_size_score",
            "setting_subtitle_extensions",
            "setting_subs_search",
            "setting_timeout",
            "setting_scheduler_mode",
            "setting_scheduler_interval",
        }
        self.assertTrue(expected.issubset(self.setting.ModuleSetting.db_default))
        self.assertEqual(
            self.setting.ModuleSetting.db_default["setting_subtitle_extensions"],
            ".srt,.ass,.ssa,.sub,.idx,.vtt,.smi,.sup",
        )
        self.assertEqual(
            self.setting.ModuleSetting.db_default["setting_db_version"], "3"
        )
        self.assertIn(
            '"h264": 10000',
            self.setting.ModuleSetting.db_default["setting_score_json"],
        )
        self.assertIn(
            '"*Remux*": 20000',
            self.setting.ModuleSetting.db_default["setting_filename_score"],
        )
        self.assertEqual(
            self.setting.ModuleSetting.db_default["setting_size_score"], "True"
        )
        self.p.ModelSetting.values.update(
            {
                "setting_library_id": "1, 2;3\n2",
                "setting_filename_score": '{"*REMUX*":10000}',
                "setting_size_score": "True",
            }
        )
        config = self.setting.runtime_config()
        self.assertEqual(config["library_ids"], ["1", "2", "3"])
        self.assertEqual(config["filename_scores"], {"*REMUX*": 10000.0})
        self.assertTrue(config["include_size"])

    def test_invalid_score_settings_are_rejected_instead_of_defaulted(self):
        self.p.ModelSetting.values["setting_filename_score"] = '{"*x*":"NaN"}'
        with self.assertRaisesRegex(ValueError, "유한"):
            self.setting.runtime_config()
        self.p.ModelSetting.values["setting_filename_score"] = "{}"
        self.p.ModelSetting.values["setting_size_score"] = "maybe"
        with self.assertRaisesRegex(ValueError, "True 또는 False"):
            self.setting.runtime_config()
        self.p.ModelSetting.values["setting_size_score"] = "False"
        self.p.ModelSetting.values["setting_score_json"] = '{"duration_divisor":0}'
        config = self.setting.runtime_config()
        score_module = __import__(
            PACKAGE + ".services.score_engine", fromlist=["ScoreEngine"]
        )
        with self.assertRaisesRegex(ValueError, "positive"):
            score_module.ScoreEngine(self.cleanup._score_config(config))

    def test_invalid_library_id_is_rejected_before_worker_start(self):
        self.p.ModelSetting.values["setting_library_id"] = "1, movies"
        with self.assertRaisesRegex(ValueError, "숫자"):
            self.setting.runtime_config()

    def test_library_lookup_command_returns_clickable_payload(self):
        module = self.setting.ModuleSetting(self.p)
        expected = [
            {"id": "3", "name": "영화", "type": "movie"},
            {"id": "7", "name": "드라마", "type": "show"},
        ]
        with mock.patch.object(self.setting, "library_sections", return_value=expected):
            result = module.process_command("libraries", "", "", "", None)
        self.assertEqual(result, {"ret": "success", "data": expected})

    def test_setting_page_replaces_legacy_empty_json_for_display(self):
        self.p.ModelSetting.values["setting_score_json"] = "{}"
        self.p.ModelSetting.values["setting_filename_score"] = "{}"
        template, values = self.setting.ModuleSetting(self.p).process_menu(
            "setting", None
        )
        self.assertEqual(template, "plex_dupefinder_ff_setting_setting.html")
        self.assertIn('"h264": 10000', values["arg"]["setting_score_json"])
        self.assertIn(
            '"*Remux*": 20000', values["arg"]["setting_filename_score"]
        )

    def test_library_lookup_matches_plex_mate_webhook_db_and_filters_music(self):
        provider_module = __import__(
            PACKAGE + ".services.plex_mate_provider",
            fromlist=["PlexMateProvider"],
        )

        class Handle:
            @staticmethod
            def library_sections():
                return [
                    {"id": 12, "name": "드라마", "section_type": 2},
                    {"id": 3, "name": "영화", "section_type": 1},
                    {"id": 9, "name": "음악", "section_type": 8},
                ]

        provider = types.SimpleNamespace(
            get_plugin=lambda: types.SimpleNamespace(PlexDBHandle=Handle)
        )
        with mock.patch.object(
            provider_module, "PlexMateProvider", return_value=provider
        ):
            result = self.setting.library_sections()
        self.assertEqual(
            result,
            [
                {"id": "3", "name": "영화", "type": "movie"},
                {"id": "12", "name": "드라마", "type": "show"},
            ],
        )

    def test_library_lookup_falls_back_to_plex_web_when_mate_db_is_unavailable(self):
        provider_module = __import__(
            PACKAGE + ".services.plex_mate_provider",
            fromlist=["PlexMateProvider"],
        )
        gateway_module = __import__(
            PACKAGE + ".services.plex_gateway", fromlist=["PlexGateway"]
        )

        class Handle:
            @staticmethod
            def library_sections():
                return None

        provider = types.SimpleNamespace(
            get_plugin=lambda: types.SimpleNamespace(PlexDBHandle=Handle),
            resolve=lambda require_machine_id=False: "connection",
        )

        class Gateway:
            def __init__(self, connection, timeout):
                self.connection = connection
                self.timeout = timeout

            def list_sections(self):
                return (
                    types.SimpleNamespace(
                        key="7", title="TV", section_type="show", plex_item_type=4
                    ),
                    types.SimpleNamespace(
                        key="8", title="Music", section_type="artist", plex_item_type=None
                    ),
                )

        with mock.patch.object(
            provider_module, "PlexMateProvider", return_value=provider
        ), mock.patch.object(gateway_module, "PlexGateway", Gateway):
            result = self.setting.library_sections()
        self.assertEqual(result, [{"id": "7", "name": "TV", "type": "show"}])

    def test_v2_empty_scores_migrate_to_upstream_example(self):
        self.p.ModelSetting.values.update(
            {
                "setting_db_version": "2",
                "setting_score_json": "{}",
                "setting_filename_score": "",
                "setting_size_score": "False",
            }
        )
        self.setting.ModuleSetting(self.p).migration()
        self.assertEqual(self.p.ModelSetting.get("setting_db_version"), "3")
        self.assertIn('"h264": 10000', self.p.ModelSetting.get("setting_score_json"))
        self.assertIn(
            '"*Remux*": 20000',
            self.p.ModelSetting.get("setting_filename_score"),
        )
        self.assertEqual(self.p.ModelSetting.get("setting_size_score"), "True")

    def test_v2_custom_scores_are_preserved_during_migration(self):
        self.p.ModelSetting.values.update(
            {
                "setting_db_version": "2",
                "setting_score_json": '{"bitrate_weight":9}',
                "setting_filename_score": '{"*CUSTOM*":7}',
                "setting_size_score": "False",
            }
        )
        self.setting.ModuleSetting(self.p).migration()
        self.assertEqual(
            self.p.ModelSetting.get("setting_score_json"),
            '{"bitrate_weight":9}',
        )
        self.assertEqual(
            self.p.ModelSetting.get("setting_filename_score"),
            '{"*CUSTOM*":7}',
        )
        self.assertEqual(self.p.ModelSetting.get("setting_size_score"), "False")

    def test_filename_glob_is_not_double_translated_and_empty_uses_defaults(self):
        score_module = __import__(
            PACKAGE + ".services.score_engine", fromlist=["ScoreEngine"]
        )
        base = {
            "score": {},
            "filename_scores": {},
            "include_size": False,
        }
        default_config = self.cleanup._score_config(base)
        self.assertEqual(default_config.filename_scores["*Remux*"], 20000)
        custom = dict(base, filename_scores={"*REMUX*": 10000.0})
        custom_config = self.cleanup._score_config(custom)
        self.assertEqual(custom_config.filename_scores, {"*REMUX*": 10000.0})
        engine = score_module.ScoreEngine(custom_config)
        candidate = _candidate("1", "/media/Movie.REMUX.mkv", 1, 0)
        self.assertEqual(engine.score(candidate).breakdown["filename"], 10000.0)

    def test_subs_search_only_controls_subdirectories_not_same_directory(self):
        base_config = {
            "timeout": 20,
            "score": {},
            "filename_scores": {},
            "include_size": False,
            "subtitle_extensions": [".srt"],
            "subs_search": False,
        }
        with mock.patch.object(self.cleanup, "_resolve_plex_connection", return_value=object()), mock.patch.object(
            self.cleanup, "_create_gateway", return_value=object()
        ):
            same_dir_only = self.cleanup.CleanupServiceAdapter(base_config)
            with_subdirs = self.cleanup.CleanupServiceAdapter(
                dict(base_config, subs_search=True)
            )
        self.assertEqual(same_dir_only.subtitle_finder.subtitle_dirs, ())
        self.assertEqual(
            with_subdirs.subtitle_finder.subtitle_dirs, ("Subs", "Subtitles")
        )

    def test_shared_sidecars_are_preserved_by_set_difference(self):
        module = self.cleanup.ModuleCleanup(self.p)
        keep = _candidate("1", "/video/keep.mkv", 1, 10)
        duplicate = _candidate("2", "/video/delete.mkv", 1, 5)
        adapter = _Adapter(
            _group(keep, duplicate),
            sidecars={
                "1": ("/video/shared.srt",),
                "2": ("/video/shared.srt", "/video/delete.en.srt"),
            },
        )
        exclusive, shared = module._shared_sidecars(
            adapter, {"1": keep, "2": duplicate}, "2"
        )
        self.assertEqual(exclusive, ("/video/delete.en.srt",))
        self.assertEqual(shared, ("/video/shared.srt",))

    def test_dry_run_records_bytes_without_delete_or_unlink(self):
        keep = _candidate("1", "/missing/keep.mkv", 100, 10)
        duplicate = _candidate("2", "/missing/delete.mkv", 4096, 5)
        adapter = _Adapter(
            _group(keep, duplicate), sidecars={"2": ("/missing/delete.srt",)}
        )
        run, module, actions = self._run_worker("dry_run", adapter)
        self.assertEqual(adapter.delete_calls, [])
        self.assertEqual(adapter.sidecar_delete_calls, [])
        self.assertEqual(actions[0].status, "would_delete")
        self.assertEqual(actions[0].file_size, 4096)
        self.assertEqual(run.would_delete_count, 1)
        self.assertEqual(run.would_delete_bytes, 4096)
        self.assertEqual(run.status, "completed")
        self.assertFalse(module.status_payload()["running"])
        self.assertEqual(module.status_payload()["status"], "completed")

    def test_stop_response_immediately_exposes_stopping_status(self):
        run = _FakeRun("live")
        run.status = "running"
        _FakeRunModel.current = run
        module = self.cleanup.ModuleCleanup(self.p)
        module.worker_thread = types.SimpleNamespace(is_alive=lambda: True)
        module.current_run_id = run.id
        module._status.update({"running": True, "status": "running"})

        with mock.patch.object(self.cleanup, "ModelCleanupRun", _FakeRunModel):
            response = module._stop()

        self.assertEqual(response["ret"], "success")
        self.assertEqual(response["data"]["status"], "stopping")
        self.assertTrue(response["data"]["running"])
        self.assertTrue(response["data"]["stop_requested"])
        self.assertEqual(run.status, "stopping")
        self.assertTrue(run.stop_requested)

    def test_stop_during_pre_delete_read_prevents_delete_and_sidecar_unlink(self):
        keep = _candidate("1", "/missing/keep.mkv", 100, 10)
        duplicate = _candidate("2", "/missing/delete.mkv", 4096, 5)
        entered = threading.Event()
        release = threading.Event()
        adapter = _Adapter(
            _group(keep, duplicate),
            sidecars={"2": ("/missing/delete.srt",)},
            pre_delete_entered=entered,
            pre_delete_release=release,
        )
        run = _FakeRun("live")
        _FakeRunModel.current = run
        module = self.cleanup.ModuleCleanup(self.p)
        module.adapter_factory = lambda config: adapter
        worker = threading.Thread(
            target=module._worker,
            args=(run.id, "live", {"library_ids": ["1"]}),
        )
        module.worker_thread = worker
        module.current_run_id = run.id

        with mock.patch.object(self.cleanup, "ModelCleanupRun", _FakeRunModel), mock.patch.object(
            self.cleanup, "ModelCleanupAction", _FakeActionModel
        ):
            worker.start()
            try:
                self.assertTrue(entered.wait(2), "worker did not enter pre-delete read")
                response = module._stop()
                self.assertEqual(response["ret"], "success")
            finally:
                release.set()
                worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(adapter.delete_calls, [])
        self.assertEqual(adapter.sidecar_delete_calls, [])
        self.assertEqual(_FakeActionModel.items, [])
        self.assertEqual(run.processed_groups, 0)
        self.assertEqual(run.status, "stopped")
        self.assertTrue(run.stop_requested)
        self.assertEqual(module.status_payload()["status"], "stopped")
        self.assertFalse(module.status_payload()["running"])

    def test_stop_during_delete_action_commit_prevents_plex_delete(self):
        keep = _candidate("1", "/missing/keep.mkv", 100, 10)
        duplicate = _candidate("2", "/missing/delete.mkv", 4096, 5)
        entered = threading.Event()
        release = threading.Event()
        _FakeActionModel.create_entered = entered
        _FakeActionModel.create_release = release
        adapter = _Adapter(
            _group(keep, duplicate),
            sidecars={"2": ("/missing/delete.srt",)},
        )
        run = _FakeRun("live")
        _FakeRunModel.current = run
        module = self.cleanup.ModuleCleanup(self.p)
        module.adapter_factory = lambda config: adapter
        worker = threading.Thread(
            target=module._worker,
            args=(run.id, "live", {"library_ids": ["1"]}),
        )
        module.worker_thread = worker
        module.current_run_id = run.id

        with mock.patch.object(self.cleanup, "ModelCleanupRun", _FakeRunModel), mock.patch.object(
            self.cleanup, "ModelCleanupAction", _FakeActionModel
        ):
            worker.start()
            try:
                self.assertTrue(entered.wait(2), "worker did not persist delete action")
                response = module._stop()
                self.assertEqual(response["ret"], "success")
            finally:
                release.set()
                worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(adapter.delete_calls, [])
        self.assertEqual(adapter.sidecar_delete_calls, [])
        self.assertEqual(len(_FakeActionModel.items), 1)
        self.assertEqual(_FakeActionModel.items[0].status, "skipped")
        self.assertEqual(
            _FakeActionModel.items[0].message,
            "stop_requested_before_delete",
        )
        self.assertEqual(run.deleted_count, 0)
        self.assertEqual(run.processed_groups, 0)
        self.assertEqual(run.status, "stopped")

    def test_stop_during_delete_finishes_current_cleanup_then_stops(self):
        keep = _candidate("1", "/missing/keep.mkv", 100, 30)
        first = _candidate("2", "/missing/delete-one.mkv", 200, 20)
        second = _candidate("3", "/missing/delete-two.mkv", 300, 10)
        entered = threading.Event()
        release = threading.Event()
        adapter = _Adapter(
            _group(keep, first, second),
            sidecars={
                "2": ("/missing/delete-one.srt",),
                "3": ("/missing/delete-two.srt",),
            },
            delete_entered=entered,
            delete_release=release,
        )
        run = _FakeRun("live")
        _FakeRunModel.current = run
        module = self.cleanup.ModuleCleanup(self.p)
        module.adapter_factory = lambda config: adapter
        worker = threading.Thread(
            target=module._worker,
            args=(run.id, "live", {"library_ids": ["1"]}),
        )
        module.worker_thread = worker
        module.current_run_id = run.id

        with mock.patch.object(self.cleanup, "ModelCleanupRun", _FakeRunModel), mock.patch.object(
            self.cleanup, "ModelCleanupAction", _FakeActionModel
        ):
            worker.start()
            try:
                self.assertTrue(entered.wait(2), "worker did not enter Plex DELETE")
                response = module._stop()
                self.assertEqual(response["ret"], "success")
            finally:
                release.set()
                worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(adapter.delete_calls, [("42", "2")])
        self.assertEqual(adapter.get_calls, 2)
        self.assertEqual(
            adapter.sidecar_delete_calls,
            [("/missing/delete-one.srt",)],
        )
        self.assertEqual(len(_FakeActionModel.items), 1)
        self.assertEqual(_FakeActionModel.items[0].status, "deleted")
        self.assertEqual(run.deleted_count, 1)
        self.assertEqual(run.processed_groups, 0)
        self.assertEqual(run.status, "stopped")

    def test_terminal_status_is_not_reported_running_or_changed_by_late_stop(self):
        run = _FakeRun("live")
        run.status = "completed"
        _FakeRunModel.current = run
        module = self.cleanup.ModuleCleanup(self.p)
        module.worker_thread = types.SimpleNamespace(is_alive=lambda: True)
        module.current_run_id = run.id
        module._status.update(
            {"running": False, "status": "completed", "message": "완료"}
        )

        with mock.patch.object(self.cleanup, "ModelCleanupRun", _FakeRunModel):
            response = module._stop()

        self.assertEqual(response["ret"], "warning")
        self.assertEqual(run.status, "completed")
        self.assertFalse(module.stop_event.is_set())
        self.assertFalse(module.status_payload()["running"])

    def test_delete_transport_error_reconciles_once_without_retry(self):
        keep = _candidate("1", "/missing/keep.mkv", 100, 10)
        duplicate = _candidate("2", "/missing/delete.mkv", 4096, 5)
        adapter = _Adapter(
            _group(keep, duplicate),
            delete_error=TimeoutError("lost response"),
            media_exists=False,
            sidecars={"2": ("/missing/delete.srt",)},
        )
        run, _, actions = self._run_worker("live", adapter)
        self.assertEqual(adapter.delete_calls, [("42", "2")])
        self.assertEqual(adapter.sidecar_delete_calls, [("/missing/delete.srt",)])
        self.assertEqual(actions[0].status, "deleted")
        self.assertIn("plex_delete_exception_reconciled:TimeoutError", actions[0].message)
        self.assertEqual(run.deleted_count, 1)
        self.assertEqual(run.deleted_bytes, 4096)

    def test_sidecars_are_not_unlinked_while_original_video_path_exists(self):
        keep = _candidate("1", "/video/keep.mkv", 100, 10)
        duplicate = _candidate("2", "/video/delete.mkv", 4096, 5)
        adapter = _Adapter(
            _group(keep, duplicate),
            media_exists=False,
            sidecars={"2": ("/video/delete.srt",)},
        )
        with mock.patch.object(self.cleanup.os.path, "lexists", return_value=True):
            run, _, actions = self._run_worker("live", adapter)
        self.assertEqual(adapter.delete_calls, [("42", "2")])
        self.assertEqual(adapter.sidecar_delete_calls, [])
        self.assertEqual(actions[0].status, "partial")
        self.assertEqual(actions[0].message, "video_file_still_present")
        self.assertEqual(run.partial_count, 1)
        self.assertEqual(run.deleted_count, 0)
        self.assertEqual(run.deleted_bytes, 0)

    def test_post_delete_keep_disappearance_stops_group_and_preserves_sidecars(self):
        keep = _candidate("1", "/missing/keep.mkv", 100, 30)
        first = _candidate("2", "/missing/delete-one.mkv", 200, 20)
        second = _candidate("3", "/missing/delete-two.mkv", 300, 10)
        original = _group(keep, first, second)
        # Target 2 disappeared, but Plex also lost keep candidate 1. Candidate
        # 3 remains and must not be attempted after this invariant failure.
        changed = types.SimpleNamespace(
            rating_key="42",
            title="Example",
            media_type="movie",
            candidates=(second,),
        )
        adapter = _Adapter(
            original,
            post_delete_group=changed,
            sidecars={"2": ("/missing/delete-one.srt",)},
        )
        run, _, actions = self._run_worker("live", adapter)
        self.assertEqual(adapter.delete_calls, [("42", "2")])
        self.assertEqual(adapter.sidecar_delete_calls, [])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].status, "partial")
        self.assertEqual(
            actions[0].message, "post_delete_remaining_media_changed"
        )
        self.assertEqual(run.partial_count, 1)
        self.assertEqual(run.deleted_count, 0)

    def test_shared_video_path_skips_entire_group(self):
        keep = _candidate("1", "/video/shared.mkv", 100, 10)
        duplicate = _candidate("2", "/video/shared.mkv", 100, 5)
        adapter = _Adapter(_group(keep, duplicate))
        run, _, actions = self._run_worker("live", adapter)
        self.assertEqual(adapter.delete_calls, [])
        self.assertEqual(actions[0].status, "skipped")
        self.assertEqual(actions[0].message, "shared_video_path")
        self.assertEqual(run.processed_groups, 1)

    def test_command_and_scheduler_surface_match_templates(self):
        module = self.cleanup.ModuleCleanup(self.p)
        module._start = lambda mode: {"ret": "success", "mode": mode}
        module._stop = lambda: {"ret": "success", "stopped": True}
        self.assertEqual(
            module.process_command("dry_run", "", "", "", None)["mode"],
            "dry_run",
        )
        self.assertEqual(
            module.process_command("start_live", "", "", "", None)["mode"],
            "live",
        )
        self.assertTrue(
            module.process_command("stop", "", "", "", None)["stopped"]
        )
        status = module.process_command("status", "", "", "", None)["data"]
        for key in (
            "running",
            "status",
            "mode",
            "stop_requested",
            "started_at",
            "current",
            "progress",
            "summary",
            "recent_actions",
        ):
            self.assertIn(key, status)

        self.p.ModelSetting.values["setting_scheduler_mode"] = "dry_run"
        self.assertTrue(module.scheduler_function())
        self.assertEqual(module.get_scheduler_interval(), "60")

    def test_setting_save_synchronizes_flaskfarm_scheduler_keys(self):
        calls = []
        self.p.logic = types.SimpleNamespace(
            scheduler_stop=lambda name: calls.append(("stop", name)),
            scheduler_start=lambda name: calls.append(("start", name)),
        )
        self.p.ModelSetting.values.update(
            {
                "setting_scheduler_mode": "live",
                "setting_scheduler_interval": "15",
            }
        )
        module = self.setting.ModuleSetting(self.p)
        module.setting_save_after([])
        self.assertEqual(self.p.ModelSetting.get("cleanup_interval"), "15")
        self.assertEqual(self.p.ModelSetting.get("cleanup_auto_start"), "True")
        self.assertEqual(calls, [("stop", "cleanup"), ("start", "cleanup")])

        calls.clear()
        cleanup_module = self.cleanup.ModuleCleanup(self.p)
        cleanup_module.setting_save_after([])
        self.assertEqual(self.p.ModelSetting.get("cleanup_interval"), "15")
        self.assertEqual(calls, [("stop", "cleanup"), ("start", "cleanup")])

    def test_setup_declares_expected_modules_and_home(self):
        source = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn('"home_module": "cleanup"', source)
        self.assertIn(
            "P.set_module_list([ModuleSetting, ModuleCleanup, ModuleHistory])",
            source,
        )
        self.assertIn("P.ModelCleanupRun = ModelCleanupRun", source)
        self.assertIn("P.ModelCleanupAction = ModelCleanupAction", source)


if __name__ == "__main__":
    unittest.main()
