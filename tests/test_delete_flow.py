from __future__ import annotations

import contextlib
import importlib.util
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
from services.domain import MediaPart, MediaVersion, MetadataItem, PlexConnection, PlexIdentity
from services.plex_gateway import PlexDeleteOutcomeUnknown, PlexGatewayError
from services.safety import SafetyPolicy
import services as services_package
import services.plex_gateway as gateway_module
import services.plex_mate_provider as provider_module
import services.safety as safety_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _version(media_id: str, *, bitrate: int = 1_000) -> MediaVersion:
    return MediaVersion(
        media_id=media_id,
        duration=7_200_000,
        bitrate=bitrate,
        width=1920,
        height=1080,
        video_resolution="1080",
        video_codec="h264",
        audio_codec="aac",
        audio_channels=2,
        container="mkv",
        parts=(
            MediaPart(
                part_id=media_id + "1",
                file="/media/movies/%s.mkv" % media_id,
                size=1_000,
                duration=7_200_000,
                container="mkv",
                exists=True,
            ),
        ),
    )


def _item(*versions: MediaVersion) -> MetadataItem:
    return MetadataItem(
        rating_key="100",
        guid="plex://movie/delete-review",
        media_type="movie",
        title="Delete Review",
        year=2024,
        media=tuple(versions),
    )


class _Record:
    def __init__(self, **values: Any) -> None:
        for key, value in values.items():
            setattr(self, key, value)


class _Query:
    def __init__(self, values: List[Any]) -> None:
        self.values = values
        self.filters: Dict[str, Any] = {}

    def filter_by(self, **values: Any) -> "_Query":
        self.filters.update(values)
        return self

    def first(self):
        for item in self.values:
            if all(getattr(item, key, None) == value for key, value in self.filters.items()):
                return item
        return None


class _Session:
    def __init__(self, action_log_type: type) -> None:
        self.action_log_type = action_log_type
        self.logs: List[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self._id_lock = threading.Lock()

    def add(self, item: Any) -> None:
        if isinstance(item, self.action_log_type):
            with self._id_lock:
                if getattr(item, "id", None) is None:
                    item.id = len(self.logs) + 1
                self.logs.append(item)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def query(self, model: type) -> _Query:
        if model is self.action_log_type:
            return _Query(self.logs)
        return _Query([])


class _App:
    @contextlib.contextmanager
    def app_context(self):
        yield


class _Logger:
    def __getattr__(self, name: str):
        return lambda *args, **kwargs: None


class DeleteServiceHarness:
    """Load delete_service.py against deterministic in-memory FlaskFarm doubles."""

    def __init__(self, before: MetadataItem) -> None:
        self.before = before
        self.run = _Record(
            id=1,
            status="completed",
            successful_deletions=0,
            deletion_attempts=0,
            server_machine_id="machine-1",
        )
        self.group = _Record(
            id=10,
            run_id=1,
            rating_key=before.rating_key,
            identity_fingerprint=before.identity_fingerprint(),
            safe_to_delete=True,
            resolution_status="open",
            safety_flags_json="[]",
        )
        self.candidates = {
            index: _Record(
                id=index,
                group_id=10,
                media_id=version.media_id,
                fingerprint=version.fingerprint(),
                deleted=False,
                deleted_at=None,
            )
            for index, version in enumerate(before.media, start=1)
        }

        harness = self

        class ModelScanRun:
            @classmethod
            def get(cls, run_id):
                return harness.run if int(run_id) == harness.run.id else None

            @classmethod
            def claim_deletion_slot(cls, run_id, limit):
                run = cls.get(run_id)
                if (
                    run is None
                    or run.status not in ("completed", "completed_with_warnings")
                    or run.deletion_attempts >= int(limit)
                ):
                    return False
                run.deletion_attempts += 1
                return True

        class ModelDuplicateGroup:
            @classmethod
            def get(cls, group_id):
                return harness.group if int(group_id) == harness.group.id else None

            @classmethod
            def claim_for_delete(cls, group_id):
                group = cls.get(group_id)
                if (
                    group is None
                    or not group.safe_to_delete
                    or group.resolution_status != "open"
                ):
                    return False
                group.safe_to_delete = False
                group.resolution_status = "delete_in_progress"
                return True

        class ModelMediaCandidate:
            @classmethod
            def get(cls, candidate_id):
                return harness.candidates.get(int(candidate_id))

            @classmethod
            def by_group(cls, group_id, include_deleted=True):
                values = [
                    item
                    for item in harness.candidates.values()
                    if item.group_id == int(group_id)
                ]
                return values if include_deleted else [item for item in values if not item.deleted]

        class ModelActionLog(_Record):
            @classmethod
            def interrupted(cls):
                return [
                    log
                    for log in harness.session.logs
                    if log.status in ("validating", "deleting")
                ]

        self.ModelActionLog = ModelActionLog
        self.session = _Session(ModelActionLog)
        fake_f = types.SimpleNamespace(
            app=_App(),
            db=types.SimpleNamespace(session=self.session),
        )

        class Settings:
            values = {
                "setting_delete_enabled": "True",
                "setting_max_delete_per_run": "1",
                "setting_request_timeout": "20",
            }

            @classmethod
            def get(cls, key):
                return cls.values.get(key)

        fake_p = types.SimpleNamespace(ModelSetting=Settings, logger=_Logger())

        framework = types.ModuleType("framework")
        framework.F = fake_f
        review_package = types.ModuleType("delete_review")
        review_package.__path__ = [str(PROJECT_ROOT)]
        models = types.ModuleType("delete_review.models")
        models.ModelScanRun = ModelScanRun
        models.ModelDuplicateGroup = ModelDuplicateGroup
        models.ModelMediaCandidate = ModelMediaCandidate
        models.ModelActionLog = ModelActionLog
        scan_manager = types.ModuleType("delete_review.scan_manager")
        scan_manager.current_safety_policy = lambda: SafetyPolicy(
            allowed_roots=("/media/movies",),
            require_guid=True,
            block_multipart=True,
            require_allowed_roots=True,
        )
        setup = types.ModuleType("delete_review.setup")
        setup.P = fake_p

        replacements = {
            "framework": framework,
            "delete_review": review_package,
            "delete_review.models": models,
            "delete_review.scan_manager": scan_manager,
            "delete_review.setup": setup,
            "delete_review.services": services_package,
            "delete_review.services.plex_gateway": gateway_module,
            "delete_review.services.plex_mate_provider": provider_module,
            "delete_review.services.safety": safety_module,
        }
        sentinel = object()
        saved = {name: sys.modules.get(name, sentinel) for name in replacements}
        sys.modules.update(replacements)
        module_name = "delete_review.delete_service"
        try:
            spec = importlib.util.spec_from_file_location(
                module_name, PROJECT_ROOT / "delete_service.py"
            )
            if spec is None or spec.loader is None:
                raise RuntimeError("delete_service.py import spec could not be created")
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

    def service_for(self, gateway):
        harness = self

        class Provider:
            def resolve(self, require_machine_id=False):
                harness.require_machine_id = require_machine_id
                return PlexConnection("http://plex.local:32400", "machine-1", "secret")

        self.module.PlexMateProvider = Provider
        self.module.PlexGateway = lambda *args, **kwargs: gateway
        return self.module.DeleteService()


class _Gateway:
    def __init__(self, metadata_results, delete_result=200) -> None:
        self.metadata_results = list(metadata_results)
        self.delete_result = delete_result
        self.delete_calls = []

    def validate_identity(self, expected_machine_id, require_match=True):
        return PlexIdentity("machine-1", "1.40")

    def get_metadata(self, rating_key):
        if not self.metadata_results:
            raise AssertionError("unexpected metadata read")
        value = self.metadata_results.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def delete_media(self, rating_key, media_id):
        self.delete_calls.append((str(rating_key), str(media_id)))
        if isinstance(self.delete_result, BaseException):
            raise self.delete_result
        return self.delete_result


class DeleteFlowTest(unittest.TestCase):
    def test_confirmed_delete_updates_audit_and_requires_rescan(self) -> None:
        before = _item(_version("10"), _version("20", bitrate=2_000))
        after = _item(before.media[1])
        harness = DeleteServiceHarness(before)
        gateway = _Gateway([before, after])

        result = harness.service_for(gateway).delete(10, 1, 2, "DELETE 10")

        self.assertEqual(result["verification"], "confirmed")
        self.assertEqual(gateway.delete_calls, [("100", "10")])
        self.assertTrue(harness.candidates[1].deleted)
        self.assertEqual(harness.group.resolution_status, "rescan_required")
        self.assertFalse(harness.group.safe_to_delete)
        self.assertEqual(harness.run.successful_deletions, 1)
        self.assertEqual(harness.run.deletion_attempts, 1)
        self.assertEqual(harness.session.logs[-1].status, "success")
        self.assertTrue(harness.require_machine_id)

    def test_unknown_delete_and_failed_reread_are_locked_for_manual_check(self) -> None:
        before = _item(_version("10"), _version("20"))
        harness = DeleteServiceHarness(before)
        gateway = _Gateway(
            [before, PlexGatewayError("post-read unavailable")],
            delete_result=PlexDeleteOutcomeUnknown("unknown"),
        )

        with self.assertRaises(RuntimeError):
            harness.service_for(gateway).delete(10, 1, 2, "DELETE 10")

        self.assertEqual(gateway.delete_calls, [("100", "10")])
        self.assertEqual(harness.session.logs[-1].status, "unknown")
        self.assertEqual(harness.group.resolution_status, "manual_check_required")
        self.assertFalse(harness.group.safe_to_delete)
        self.assertFalse(harness.candidates[1].deleted)
        self.assertEqual(harness.run.deletion_attempts, 1)

    def test_postcheck_rejects_any_unexpected_media_set_change(self) -> None:
        before = _item(_version("10"), _version("20"), _version("30"))
        # Target 10 is gone, but unrelated version 30 also vanished concurrently.
        after = _item(before.media[1])
        harness = DeleteServiceHarness(before)
        gateway = _Gateway([before, after])

        with self.assertRaises(RuntimeError):
            harness.service_for(gateway).delete(10, 1, 2, "DELETE 10")

        self.assertNotEqual(harness.session.logs[-1].status, "success")
        self.assertEqual(harness.group.resolution_status, "manual_check_required")
        self.assertFalse(harness.candidates[1].deleted)
        self.assertEqual(harness.run.deletion_attempts, 1)

    def test_postcheck_rejects_change_to_remaining_media_snapshot(self) -> None:
        before = _item(_version("10"), _version("20", bitrate=2_000))
        after = _item(_version("20", bitrate=2_001))
        harness = DeleteServiceHarness(before)
        gateway = _Gateway([before, after])

        with self.assertRaises(RuntimeError):
            harness.service_for(gateway).delete(10, 1, 2, "DELETE 10")

        self.assertEqual(harness.session.logs[-1].status, "critical")
        self.assertEqual(harness.group.resolution_status, "manual_check_required")
        self.assertFalse(harness.candidates[1].deleted)
        self.assertEqual(harness.run.deletion_attempts, 1)

    def test_stale_fingerprint_blocks_delete_before_network_mutation(self) -> None:
        scanned = _item(_version("10"), _version("20"))
        changed = _item(_version("10", bitrate=9_999), scanned.media[1])
        harness = DeleteServiceHarness(scanned)
        gateway = _Gateway([changed])

        with self.assertRaises(RuntimeError):
            harness.service_for(gateway).delete(10, 1, 2, "DELETE 10")

        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(harness.session.logs[-1].status, "blocked")
        self.assertEqual(harness.group.resolution_status, "manual_check_required")
        self.assertFalse(harness.group.safe_to_delete)
        self.assertFalse(harness.candidates[1].deleted)
        self.assertEqual(harness.run.deletion_attempts, 0)

    def test_consumed_attempt_slot_blocks_another_delete_before_group_claim(self) -> None:
        before = _item(_version("10"), _version("20"))
        harness = DeleteServiceHarness(before)
        harness.run.deletion_attempts = 1
        gateway = _Gateway([before])

        with self.assertRaises(RuntimeError):
            harness.service_for(gateway).delete(10, 1, 2, "DELETE 10")

        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(harness.session.logs, [])
        self.assertTrue(harness.group.safe_to_delete)
        self.assertEqual(harness.group.resolution_status, "open")

    def test_restart_recovery_blocks_validating_group_without_consuming_slot(self) -> None:
        before = _item(_version("10"), _version("20"))
        harness = DeleteServiceHarness(before)
        harness.group.safe_to_delete = False
        harness.group.resolution_status = "delete_in_progress"
        log = harness.ModelActionLog(
            id=None,
            run_id=1,
            group_id=10,
            candidate_id=1,
            keep_candidate_id=2,
            action="delete_media",
            status="validating",
            message="validating",
        )
        harness.session.add(log)
        gateway = _Gateway([])

        counts = harness.service_for(gateway).recover_interrupted()

        self.assertEqual(counts, {"blocked": 1, "unknown": 0})
        self.assertEqual(log.status, "blocked")
        self.assertEqual(harness.group.resolution_status, "manual_check_required")
        self.assertEqual(harness.run.deletion_attempts, 0)

    def test_restart_recovery_marks_deleting_as_unknown_and_keeps_slot_consumed(self) -> None:
        before = _item(_version("10"), _version("20"))
        harness = DeleteServiceHarness(before)
        harness.run.deletion_attempts = 1
        harness.group.safe_to_delete = False
        harness.group.resolution_status = "delete_in_progress"
        log = harness.ModelActionLog(
            id=None,
            run_id=1,
            group_id=10,
            candidate_id=1,
            keep_candidate_id=2,
            action="delete_media",
            status="deleting",
            message="deleting",
        )
        harness.session.add(log)
        gateway = _Gateway([])

        counts = harness.service_for(gateway).recover_interrupted()

        self.assertEqual(counts, {"blocked": 0, "unknown": 1})
        self.assertEqual(log.status, "unknown")
        self.assertEqual(harness.group.resolution_status, "manual_check_required")
        self.assertEqual(harness.run.deletion_attempts, 1)


if __name__ == "__main__":
    unittest.main()
