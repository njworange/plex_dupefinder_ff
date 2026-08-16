from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List

try:
    from ._requests_compat import requests as requests
except ImportError:
    from _requests_compat import requests as requests

import services as services_package
import services.domain as domain_module
import services.plex_gateway as gateway_module
import services.plex_mate_provider as provider_module
import services.safety as safety_module
import services.score_engine as scoring_module
from services.domain import PlexConnection


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Record:
    def __init__(self, **values: Any) -> None:
        for key, value in values.items():
            setattr(self, key, value)


class _Session:
    def __init__(self, harness: "ScanManagerHarness") -> None:
        self.harness = harness
        self.commits = 0
        self.removes = 0

    def add(self, item: Any) -> None:
        if isinstance(item, self.harness.ModelScanRun):
            if getattr(item, "id", None) is None:
                item.id = len(self.harness.runs) + 1
            self.harness.runs.append(item)
        elif isinstance(item, self.harness.ModelDuplicateGroup):
            if getattr(item, "id", None) is None:
                item.id = len(self.harness.groups) + 1
            self.harness.groups.append(item)
        elif isinstance(item, self.harness.ModelMediaCandidate):
            if getattr(item, "id", None) is None:
                item.id = len(self.harness.candidates) + 1
            self.harness.candidates.append(item)

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass

    def remove(self) -> None:
        self.removes += 1


class _App:
    @contextlib.contextmanager
    def app_context(self):
        yield


class _Logger:
    def __init__(self) -> None:
        self.messages: List[str] = []

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.messages.append(" ".join(str(value) for value in args))

        return record


class _Thread:
    def __init__(self, target, args, name, daemon) -> None:
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self) -> None:
        self.started = True


class ScanManagerHarness:
    def __init__(self) -> None:
        self.runs: List[Any] = []
        self.groups: List[Any] = []
        self.candidates: List[Any] = []
        harness = self

        class ModelScanRun(_Record):
            def __init__(self, **values: Any) -> None:
                defaults = {
                    "id": None,
                    "total_groups": 0,
                    "safe_groups": 0,
                    "unsafe_groups": 0,
                    "successful_deletions": 0,
                    "cancellation_requested": False,
                    "error_summary": "",
                }
                defaults.update(values)
                super().__init__(**defaults)

            @classmethod
            def active(cls):
                return next(
                    (
                        item
                        for item in reversed(harness.runs)
                        if item.status in ("queued", "running", "cancelling")
                    ),
                    None,
                )

            @classmethod
            def get(cls, run_id):
                return next((item for item in harness.runs if item.id == int(run_id)), None)

        class ModelDuplicateGroup(_Record):
            pass

        class ModelMediaCandidate(_Record):
            pass

        self.ModelScanRun = ModelScanRun
        self.ModelDuplicateGroup = ModelDuplicateGroup
        self.ModelMediaCandidate = ModelMediaCandidate
        self.session = _Session(self)
        self.logger = _Logger()

        class Settings:
            values: Dict[str, str] = {
                "setting_allowed_roots": "/media/movies",
                "setting_require_guid": "True",
                "setting_block_multipart": "True",
                "setting_request_timeout": "20",
            }

            @classmethod
            def get(cls, key: str):
                return cls.values.get(key)

        fake_p = types.SimpleNamespace(ModelSetting=Settings, logger=self.logger)
        framework = types.ModuleType("framework")
        framework.F = types.SimpleNamespace(
            app=_App(),
            db=types.SimpleNamespace(session=self.session),
        )
        review_package = types.ModuleType("scan_review")
        review_package.__path__ = [str(PROJECT_ROOT)]
        models = types.ModuleType("scan_review.models")
        models.ModelScanRun = ModelScanRun
        models.ModelDuplicateGroup = ModelDuplicateGroup
        models.ModelMediaCandidate = ModelMediaCandidate
        setup = types.ModuleType("scan_review.setup")
        setup.P = fake_p

        replacements = {
            "framework": framework,
            "scan_review": review_package,
            "scan_review.models": models,
            "scan_review.setup": setup,
            "scan_review.services": services_package,
            "scan_review.services.domain": domain_module,
            "scan_review.services.plex_gateway": gateway_module,
            "scan_review.services.plex_mate_provider": provider_module,
            "scan_review.services.safety": safety_module,
            "scan_review.services.score_engine": scoring_module,
        }
        sentinel = object()
        saved = {name: sys.modules.get(name, sentinel) for name in replacements}
        sys.modules.update(replacements)
        module_name = "scan_review.scan_manager"
        try:
            spec = importlib.util.spec_from_file_location(
                module_name, PROJECT_ROOT / "scan_manager.py"
            )
            if spec is None or spec.loader is None:
                raise RuntimeError("scan_manager.py import spec could not be created")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            self.module = module
        finally:
            sys.modules.pop(module_name, None)
            for name, previous in saved.items():
                if previous is sentinel:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

        self.module.threading = types.SimpleNamespace(
            Lock=threading.Lock,
            Event=threading.Event,
            Thread=_Thread,
        )


class ScanFlowTest(unittest.TestCase):
    def test_start_snapshots_config_deduplicates_sections_and_is_single_flight(self) -> None:
        harness = ScanManagerHarness()
        manager = harness.module.ScanManager()

        run = manager.start(["1", " 1 ", "2"])

        self.assertEqual(run.status, "queued")
        self.assertEqual(json.loads(run.section_ids_json), ["1", "2"])
        self.assertEqual(run.total_sections, 2)
        snapshot = json.loads(run.settings_snapshot_json)
        self.assertEqual(snapshot["safety"]["allowed_roots"], ["/media/movies"])
        self.assertTrue(manager._thread.started)
        with self.assertRaises(RuntimeError):
            manager.start(["1"])

    def test_cancel_sets_persistent_and_in_process_flags(self) -> None:
        harness = ScanManagerHarness()
        manager = harness.module.ScanManager()
        run = manager.start(["1"])

        cancelled = manager.cancel(run.id)

        self.assertIs(cancelled, run)
        self.assertTrue(run.cancellation_requested)
        self.assertEqual(run.status, "cancelling")
        self.assertTrue(manager._cancel.is_set())

    def test_worker_failure_redacts_current_plex_token(self) -> None:
        harness = ScanManagerHarness()
        manager = harness.module.ScanManager()
        run = manager.start(["1"])
        secret = "never-log-this-token"

        class Provider:
            def resolve(self, require_machine_id=False):
                return PlexConnection("http://plex.local:32400", "machine-1", secret)

        class Gateway:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def validate_identity(self, *args, **kwargs):
                raise RuntimeError("connection failed: %s" % secret)

        harness.module.PlexMateProvider = Provider
        harness.module.PlexGateway = Gateway
        manager._worker(run.id, ["1"], harness.module.current_score_config(), harness.module.current_safety_policy())

        self.assertEqual(run.status, "failed")
        self.assertNotIn(secret, run.error_summary)
        self.assertIn("***", run.error_summary)
        self.assertNotIn(secret, "\n".join(harness.logger.messages))
        self.assertEqual(harness.session.removes, 1)


if __name__ == "__main__":
    unittest.main()
