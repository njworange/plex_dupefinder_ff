from __future__ import annotations

import ast
import contextlib
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "plex_dupefinder_ff"


class _Expression:
    def __init__(self, name: str = "") -> None:
        self.name = name

    def __call__(self, *args: Any, **kwargs: Any) -> "_Expression":
        return self

    def __eq__(self, other: Any) -> "_Expression":  # type: ignore[override]
        return self

    def __ne__(self, other: Any) -> "_Expression":  # type: ignore[override]
        return self

    def __lt__(self, other: Any) -> "_Expression":
        return self

    def __le__(self, other: Any) -> "_Expression":
        return self

    def __gt__(self, other: Any) -> "_Expression":
        return self

    def __ge__(self, other: Any) -> "_Expression":
        return self

    def in_(self, values: Iterable[Any]) -> "_Expression":
        return self

    def asc(self) -> "_Expression":
        return self

    def desc(self) -> "_Expression":
        return self


class _FakeDB:
    Model = object

    def __init__(self) -> None:
        self.Integer = _Expression("Integer")
        self.BigInteger = _Expression("BigInteger")
        self.Float = _Expression("Float")
        self.Boolean = _Expression("Boolean")
        self.DateTime = _Expression("DateTime")
        self.Text = _Expression("Text")
        self.String = _Expression("String")
        self.JSON = _Expression("JSON")
        self.session = _Expression("session")

    def Column(self, *args: Any, **kwargs: Any) -> _Expression:
        return _Expression("Column")


class _FakeApp:
    @contextlib.contextmanager
    def app_context(self):
        yield


class _FakeLogger:
    def __init__(self) -> None:
        self.messages: List[str] = []

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.messages.append(" ".join(str(arg) for arg in args))

        return record


class _FakeModelSetting:
    _data: Dict[str, str] = {}

    @classmethod
    def get(cls, key: str):
        return cls._data.get(key)

    @classmethod
    def set(cls, key: str, value: str) -> None:
        cls._data[key] = value

    @classmethod
    def to_dict(cls) -> Dict[str, str]:
        return dict(cls._data)


class _FakeModuleBase:
    db_default = None

    def __init__(
        self,
        plugin: Any,
        first_menu: Optional[str] = None,
        name: Optional[str] = None,
        scheduler_desc: Optional[str] = None,
    ) -> None:
        self.P = plugin
        self.first_menu = first_menu
        self.name = name
        self.scheduler_desc = scheduler_desc


class _FakeModelBase:
    pass


class _FakePlugin:
    def __init__(self, setting: Dict[str, Any]) -> None:
        self.setting = setting
        self.package_name = PACKAGE_NAME
        self.ModelSetting = _FakeModelSetting
        self.logger = _FakeLogger()
        self.module_list: List[Any] = []
        self.home_module = setting.get("home_module")

    def set_module_list(self, module_types: Iterable[type]) -> None:
        self.module_list = [module_type(self) for module_type in module_types]


class FlaskFarmImportHarness:
    """Import the plugin with the smallest FlaskFarm 4.1-compatible surface."""

    def __init__(self) -> None:
        self.saved_modules: Dict[str, types.ModuleType] = {}
        self.plex_mate_lookups = 0

    def _install(self, name: str, module: types.ModuleType) -> None:
        if name in sys.modules:
            self.saved_modules[name] = sys.modules[name]
        sys.modules[name] = module

    def __enter__(self):
        fake_db = _FakeDB()

        class _PluginManager:
            @classmethod
            def get_plugin_instance(inner_cls, package_name: str):
                self.plex_mate_lookups += 1
                raise AssertionError("plex_mate must not be resolved while setup.py is imported")

        fake_f = types.SimpleNamespace(db=fake_db, app=_FakeApp(), PluginManager=_PluginManager)
        framework = types.ModuleType("framework")
        framework.F = fake_f
        framework.db = fake_db

        plugin = types.ModuleType("plugin")
        plugin.PluginModuleBase = _FakeModuleBase
        plugin.ModelBase = _FakeModelBase
        plugin.create_plugin_instance = lambda setting: _FakePlugin(setting)
        plugin.__all__ = ["PluginModuleBase", "ModelBase", "create_plugin_instance"]

        flask = types.ModuleType("flask")
        flask.jsonify = lambda value, *args, **kwargs: value
        flask.render_template = lambda template, **kwargs: {"template": template, **kwargs}
        flask.session = {}

        requests = types.ModuleType("requests")
        requests.Session = type("Session", (), {})
        requests.Response = type("Response", (), {})
        requests.Timeout = type("Timeout", (Exception,), {})
        requests.RequestException = type("RequestException", (Exception,), {})

        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PROJECT_ROOT)]
        package.__package__ = PACKAGE_NAME

        self._install("framework", framework)
        self._install("plugin", plugin)
        self._install("flask", flask)
        self._install("requests", requests)
        self._install(PACKAGE_NAME, package)

        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME + ".setup", PROJECT_ROOT / "setup.py"
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("setup.py import spec could not be created")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.setup_module = module
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for name in list(sys.modules):
            if name == PACKAGE_NAME or name.startswith(PACKAGE_NAME + "."):
                sys.modules.pop(name, None)
        for name in ("requests", "flask", "plugin", "framework"):
            sys.modules.pop(name, None)
        sys.modules.update(self.saved_modules)


class FlaskFarmSetupCompatibilityTest(unittest.TestCase):
    def test_setup_imports_without_resolving_plex_mate(self) -> None:
        with FlaskFarmImportHarness() as harness:
            plugin = harness.setup_module.P
            self.assertEqual(harness.plex_mate_lookups, 0)
            self.assertEqual(plugin.package_name, PACKAGE_NAME)
            self.assertEqual(plugin.home_module, "scan")
            self.assertEqual([module.name for module in plugin.module_list], ["setting", "scan", "history"])

    def test_post_delete_scan_capability_check_is_non_destructive(self) -> None:
        with FlaskFarmImportHarness() as harness:
            scanner_called = []

            class Scanner:
                @classmethod
                def scan_refresh(cls, *args: Any, **kwargs: Any) -> None:
                    scanner_called.append((args, kwargs))

            class PlexMateSetting:
                @classmethod
                def get(cls, key: str):
                    return "/opt/plex/Plex Media Scanner" if key == "base_bin_scanner" else None

            plex_mate = types.SimpleNamespace(
                ModelSetting=PlexMateSetting,
                PlexBinaryScanner=Scanner,
            )

            class PluginManager:
                @classmethod
                def get_plugin_instance(cls, package_name: str):
                    self.assertEqual(package_name, "plex_mate")
                    return plex_mate

            sys.modules["framework"].F.PluginManager = PluginManager
            harness.setup_module.P.ModelSetting.set("setting_post_delete_scan_mode", "binary")
            setting_module = sys.modules[PACKAGE_NAME + ".mod_setting"]
            payload = setting_module._post_delete_scan_capabilities(
                web_connection_validated=True
            )

            self.assertEqual(scanner_called, [])
            self.assertEqual(payload["mode"], "binary")
            self.assertTrue(payload["binary_helper_exported"])
            self.assertTrue(payload["binary_scanner_configured"])
            self.assertTrue(payload["web_connection_validated"])
            self.assertTrue(payload["selected_supported"])

    def test_models_are_registered_on_plugin_instance(self) -> None:
        with FlaskFarmImportHarness() as harness:
            plugin = harness.setup_module.P
            expected = {
                "ModelScanRun": "scan_run",
                "ModelDuplicateGroup": "duplicate_group",
                "ModelMediaCandidate": "media_candidate",
                "ModelActionLog": "action_log",
                "ModelPostDeleteScanJob": "post_delete_scan_job",
                "ModelQuarantineJournal": "quarantine_journal",
                "ModelDirectDeleteJournal": "direct_delete_journal",
                "ModelBatchRun": "batch_run",
                "ModelBatchExclusion": "batch_exclusion",
                "ModelBatchItem": "batch_item",
                "ModelDeletionLease": "deletion_lease",
            }
            for attribute, table_name in expected.items():
                model = getattr(plugin, attribute)
                self.assertEqual(model.__tablename__, table_name)
                self.assertEqual(model.__bind_key__, PACKAGE_NAME)

    def test_module_lifecycle_delegates_to_scan_manager(self) -> None:
        with FlaskFarmImportHarness() as harness:
            scan_module = harness.setup_module.P.module_list[1]

            class ManagerStub:
                def __init__(self) -> None:
                    self.recovered = False
                    self.unloaded = False

                def recover_interrupted(self) -> int:
                    self.recovered = True
                    return 0

                def unload(self) -> None:
                    self.unloaded = True

            class DeleteServiceStub:
                def __init__(self) -> None:
                    self.recovered = False

                def recover_interrupted(self, exclude_delete_keys=None) -> Dict[str, int]:
                    self.recovered = True
                    self.excluded = exclude_delete_keys
                    return {"blocked": 0, "unknown": 0}

            class BatchManagerStub:
                def __init__(self) -> None:
                    self.recovered = False
                    self.unloaded = False
                    self.last_delete_recovery_counts = {"blocked": 0, "unknown": 0}

                def recover_interrupted(self) -> int:
                    self.recovered = True
                    return 0

                def live_delete_keys(self):
                    return {(1, 2, 3)}

                def unload(self) -> None:
                    self.unloaded = True

            class PostDeleteScanManagerStub:
                def __init__(self) -> None:
                    self.loaded = False
                    self.unloaded = False

                def plugin_load(self) -> int:
                    self.loaded = True
                    return 0

                def unload(self) -> None:
                    self.unloaded = True

            manager = ManagerStub()
            delete_service = DeleteServiceStub()
            batch_manager = BatchManagerStub()
            post_scan_manager = PostDeleteScanManagerStub()
            scan_module.manager = manager
            scan_module.delete_service = delete_service
            scan_module.batch_manager = batch_manager
            scan_module.post_delete_scan_manager = post_scan_manager
            scan_module.plugin_load()
            scan_module.plugin_unload()
            self.assertTrue(manager.recovered)
            self.assertFalse(delete_service.recovered)
            self.assertTrue(batch_manager.recovered)
            self.assertTrue(post_scan_manager.loaded)
            self.assertTrue(manager.unloaded)
            self.assertTrue(batch_manager.unloaded)
            self.assertTrue(post_scan_manager.unloaded)

    def test_post_scan_worker_uses_full_delete_recovery_callback(self) -> None:
        with FlaskFarmImportHarness() as harness:
            scan_module = harness.setup_module.P.module_list[1]
            callback = (
                scan_module.post_delete_scan_manager.deletion_recovery_callback
            )
            self.assertIsNotNone(callback)
            self.assertIs(callback.__self__, scan_module.batch_manager)
            self.assertEqual(callback.__func__.__name__, "recover_interrupted")

    def test_action_log_supports_summary_and_detail_serialization(self) -> None:
        with FlaskFarmImportHarness() as harness:
            model = harness.setup_module.P.ModelActionLog
            item = model()
            item.id = 1
            item.created_at = None
            item.run_id = 2
            item.group_id = 3
            item.candidate_id = 4
            item.keep_candidate_id = 5
            item.action = "delete_media"
            item.status = "success"
            item.message = "ok"
            item.response_status = 200
            item.before_json = '{"before":true}'
            item.after_json = '{"after":true}'

            summary = item.as_api(include_snapshots=False)
            detail = item.as_api(include_snapshots=True)
            self.assertNotIn("before", summary)
            self.assertNotIn("after", summary)
            self.assertEqual(detail["before"], {"before": True})
            self.assertEqual(detail["after"], {"after": True})


class FlaskFarmStaticContractTest(unittest.TestCase):
    def test_manifest_setup_and_directory_name_agree(self) -> None:
        info = (PROJECT_ROOT / "info.yaml").read_text(encoding="utf-8")
        match = re.search(r'^package_name:\s*["\']?([^"\'\s]+)', info, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), PACKAGE_NAME)

        setup_tree = ast.parse((PROJECT_ROOT / "setup.py").read_text(encoding="utf-8"))
        assignments = {
            target.id: node.value
            for node in setup_tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertIn("setting", assignments)

    def test_public_version_values_agree(self) -> None:
        info = (PROJECT_ROOT / "info.yaml").read_text(encoding="utf-8")
        manifest = re.search(r'^version:\s*["\']?([^"\'\s]+)', info, re.MULTILINE)
        package = re.search(
            r'^__version__\s*=\s*["\']([^"\']+)',
            (PROJECT_ROOT / "__init__.py").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        readme = re.search(
            r'^현재 버전:\s*`([^`]+)`',
            (PROJECT_ROOT / "README.md").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        gateway = re.search(
            r'^\s*VERSION\s*=\s*["\']([^"\']+)',
            (PROJECT_ROOT / "services" / "plex_gateway.py").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        self.assertIsNotNone(manifest)
        self.assertIsNotNone(package)
        self.assertIsNotNone(readme)
        self.assertIsNotNone(gateway)
        self.assertEqual(
            {manifest.group(1), package.group(1), readme.group(1), gateway.group(1)},
            {"1.6.0"},
        )

    def test_post_delete_scan_mode_normalization_is_fail_closed(self) -> None:
        with FlaskFarmImportHarness():
            module = sys.modules[PACKAGE_NAME + ".mod_setting"]
            normalize = module._normalize_post_delete_scan_mode
            cases = {
                None: "none",
                "": "none",
                "none": "none",
                " Binary ": "binary",
                "WEB": "web",
                "full": "none",
                "../../../scan": "none",
            }
            for raw, expected in cases.items():
                with self.subTest(raw=raw):
                    self.assertEqual(normalize(raw), expected)

    def test_menu_routes_have_modules_and_templates(self) -> None:
        with FlaskFarmImportHarness() as harness:
            plugin = harness.setup_module.P
            module_names = {module.name for module in plugin.module_list}
            menu = plugin.setting["menu"]["list"]
            routed = [item for item in menu if item["uri"] not in {"manual", "log"}]
            self.assertEqual({item["uri"] for item in routed}, module_names)
            for item in routed:
                for page in item.get("list", []):
                    template = PROJECT_ROOT / "templates" / (
                        f"{PACKAGE_NAME}_{item['uri']}_{page['uri']}.html"
                    )
                    self.assertTrue(template.is_file(), str(template))

    def test_manual_menu_targets_existing_files(self) -> None:
        with FlaskFarmImportHarness() as harness:
            menu = harness.setup_module.P.setting["menu"]["list"]
            manual = next(item for item in menu if item["uri"] == "manual")
            for item in manual.get("list", []):
                self.assertTrue((PROJECT_ROOT / item["uri"]).is_file(), item["uri"])

    def test_template_ajax_actions_are_implemented(self) -> None:
        module_sources = {
            name: (PROJECT_ROOT / f"mod_{name}.py").read_text(encoding="utf-8")
            for name in ("setting", "scan", "history")
        }
        implemented: Dict[str, set[str]] = {}
        for module_name, source in module_sources.items():
            tree = ast.parse(source)
            implemented[module_name] = {
                node.comparators[0].value
                for node in ast.walk(tree)
                if isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "sub"
                and len(node.ops) == 1
                and isinstance(node.ops[0], (ast.Eq, ast.NotEq))
                and len(node.comparators) == 1
                and isinstance(node.comparators[0], ast.Constant)
                and isinstance(node.comparators[0].value, str)
            }

        request_pattern = re.compile(
            r"PDFF\.request\(\s*packageName\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]"
        )
        missing = []
        for template in (PROJECT_ROOT / "templates").glob("*.html"):
            for module_name, action in request_pattern.findall(template.read_text(encoding="utf-8")):
                if action not in implemented.get(module_name, set()):
                    missing.append(f"{template.name}: {module_name}/{action}")
        self.assertEqual(missing, [])

    def test_every_setting_form_key_has_a_default(self) -> None:
        template = (PROJECT_ROOT / "templates" / f"{PACKAGE_NAME}_setting_setting.html").read_text(
            encoding="utf-8"
        )
        form_keys = set(
            re.findall(r"macros\.setting_[a-zA-Z0-9_]+\(\s*['\"]([^'\"]+)", template)
        )
        with FlaskFarmImportHarness() as harness:
            setting_module = harness.setup_module.P.module_list[0]
            defaults = set(setting_module.db_default)
        self.assertTrue(form_keys)
        self.assertEqual(form_keys - defaults, set())

    def test_scan_runtime_defaults_are_declared(self) -> None:
        with FlaskFarmImportHarness() as harness:
            scan_module = harness.setup_module.P.module_list[1]
            self.assertEqual(
                set(scan_module.db_default), {"scan_last_section_ids", "scan_last_run_id"}
            )

    def test_background_db_helpers_use_flask_app_context(self) -> None:
        source_text = (PROJECT_ROOT / "scan_manager.py").read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        manager = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ScanManager"
        )
        methods = {
            node.name: node
            for node in manager.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in (
            "recover_interrupted",
            "start",
            "cancel",
            "_is_cancelled",
            "_set_run",
            "_persist_group",
        ):
            source = ast.get_source_segment(source_text, methods[name]) or ""
            self.assertIn("F.app.app_context()", source, name)

        worker_source = ast.get_source_segment(source_text, methods["_worker"]) or ""
        self.assertIn("F.db.session.remove()", worker_source)


if __name__ == "__main__":
    unittest.main()
