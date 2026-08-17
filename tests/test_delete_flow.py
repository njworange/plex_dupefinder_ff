from __future__ import annotations

import contextlib
import importlib.util
import sys
import threading
import types
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

try:
    from ._requests_compat import requests as requests
except ImportError:
    from _requests_compat import requests as requests
from services.domain import (
    LibrarySection,
    MediaPart,
    MediaVersion,
    MetadataItem,
    PlexConnection,
    PlexIdentity,
)
from services.plex_gateway import PlexDeleteOutcomeUnknown, PlexGatewayError
from services.safety import SafetyPolicy
import services as services_package
import services.plex_gateway as gateway_module
import services.plex_mate_provider as provider_module
import services.safety as safety_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _version(
    media_id: str,
    *,
    bitrate: int = 1_000,
    path: str = "",
) -> MediaVersion:
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
                file=path or "/media/movies/%s.mkv" % media_id,
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
        self.quarantine_plan = None
        self.quarantine_preview_calls = []
        self.quarantine_stage_calls = []
        self.quarantine_journal = None
        self.direct_plan = None
        self.direct_preview_calls = []
        self.direct_execute_calls = []
        self.direct_journal = None
        self.lose_lease_after_stage = False
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
            section_key="7",
            media_type=before.media_type,
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
            def claim_deletion_slot(cls, run_id, limit=None):
                run = cls.get(run_id)
                if (
                    run is None
                    or run.status not in ("completed", "completed_with_warnings")
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

        class ModelQuarantineJournal:
            @classmethod
            def get(cls, journal_id):
                journal = harness.quarantine_journal
                if journal is not None and int(journal_id) == int(journal.id):
                    return journal
                return None

        class ModelDirectDeleteJournal:
            @classmethod
            def get(cls, journal_id):
                journal = harness.direct_journal
                if journal is not None and int(journal_id) == int(journal.id):
                    return journal
                return None

            @classmethod
            def for_action(cls, action_id):
                return None

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
                "setting_delete_backend": "plex",
                "setting_post_delete_scan_mode": "none",
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
        models.ModelQuarantineJournal = ModelQuarantineJournal
        models.ModelDirectDeleteJournal = ModelDirectDeleteJournal
        scan_manager = types.ModuleType("delete_review.scan_manager")
        scan_manager.current_safety_policy = lambda: SafetyPolicy(
            allowed_roots=("/media/movies",),
            require_guid=True,
            block_multipart=True,
            require_allowed_roots=True,
        )
        setup = types.ModuleType("delete_review.setup")
        setup.P = fake_p
        self.path_conflict = False
        self.lose_lease_on_renew = False
        self.lease_events = []

        class LeaseLost(RuntimeError):
            pass

        class LeaseService:
            def acquire(inner_self, owner_kind, owner_ref):
                harness.lease_events.append(("acquire", owner_kind, owner_ref))
                return "manual-lease-token"

            def renew(inner_self, token, owner_kind, owner_ref):
                harness.lease_events.append(("renew", owner_kind, owner_ref))
                if harness.lose_lease_on_renew:
                    raise LeaseLost("lease lost")

            def release(inner_self, token):
                harness.lease_events.append(("release", token))
                return True

        deletion_lease = types.ModuleType("delete_review.deletion_lease")
        deletion_lease.DeletionLeaseLost = LeaseLost
        deletion_lease.DeletionLeaseService = LeaseService
        path_conflicts = types.ModuleType("delete_review.path_conflicts")
        path_conflicts.group_has_cross_path_conflict = (
            lambda run_id, group_id: harness.path_conflict
        )
        quarantine_manager = types.ModuleType("delete_review.quarantine_manager")

        class QuarantineManager:
            def preview(self, *args, **kwargs):
                harness.quarantine_preview_calls.append((args, kwargs))
                if harness.quarantine_plan is None:
                    raise AssertionError("plex backend must not create a quarantine plan")
                return harness.quarantine_plan

            def stage(self, *args, **kwargs):
                harness.quarantine_stage_calls.append((args, kwargs))
                if harness.quarantine_plan is None:
                    raise AssertionError("plex backend must not stage filesystem files")
                journal = _Record(
                    id=901,
                    status="quarantined_pending_scan",
                    last_error=None,
                    updated_at=None,
                )

                def cleanup_api(include_paths=True):
                    return {
                        "enabled": True,
                        "backend": "quarantine",
                        "status": journal.status,
                        "eligible": (
                            [{"source_path": "/media/movies/Delete Review/10.ko.srt"}]
                            if include_paths
                            else []
                        ),
                        "excluded": [],
                        "counts": {
                            "eligible": 1,
                            "excluded": 0,
                            "protected": 1,
                            "quarantined": 1,
                        },
                    }

                journal.cleanup_api = cleanup_api
                harness.quarantine_journal = journal
                if harness.lose_lease_after_stage:
                    harness.lose_lease_on_renew = True
                return journal

        quarantine_manager.QuarantineManager = QuarantineManager
        direct_delete_manager = types.ModuleType("delete_review.direct_delete_manager")

        class DirectDeleteManager:
            def preview(self, *args, **kwargs):
                harness.direct_preview_calls.append((args, kwargs))
                if harness.direct_plan is None:
                    raise AssertionError("non-direct harness must not create a direct plan")
                return harness.direct_plan

            def execute(self, *args, **kwargs):
                harness.direct_execute_calls.append((args, kwargs))
                if harness.direct_plan is None:
                    raise AssertionError("non-direct harness must not unlink files")
                journal = _Record(
                    id=902,
                    status="deleted_pending_scan",
                    last_error=None,
                    updated_at=None,
                )

                def cleanup_api(include_paths=True):
                    return {
                        "enabled": True,
                        "backend": "direct",
                        "status": journal.status,
                        "eligible": (
                            [{"source_path": "/media/movies/Delete Review/10.ko.srt"}]
                            if include_paths
                            else []
                        ),
                        "excluded": [],
                        "counts": {
                            "eligible": 1,
                            "excluded": 0,
                            "protected": 1,
                            "deleted": 1,
                        },
                    }

                journal.cleanup_api = cleanup_api
                harness.direct_journal = journal
                return journal

            def recover_interrupted(self):
                return 0

        direct_delete_manager.DirectDeleteManager = DirectDeleteManager

        replacements = {
            "framework": framework,
            "delete_review": review_package,
            "delete_review.models": models,
            "delete_review.scan_manager": scan_manager,
            "delete_review.setup": setup,
            "delete_review.deletion_lease": deletion_lease,
            "delete_review.path_conflicts": path_conflicts,
            "delete_review.quarantine_manager": quarantine_manager,
            "delete_review.direct_delete_manager": direct_delete_manager,
            "delete_review.services": services_package,
            "delete_review.services.plex_gateway": gateway_module,
            "delete_review.services.plex_mate_provider": provider_module,
            "delete_review.services.safety": safety_module,
        }
        sentinel = object()
        saved = {name: sys.modules.get(name, sentinel) for name in replacements}
        # delete_service imports this helper lazily through the synthetic
        # package. Never let a helper bound to one harness's Settings double
        # leak into the next test case.
        budget_module_name = "delete_review.delete_budget"
        saved_budget_module = sys.modules.pop(budget_module_name, sentinel)
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
            sys.modules.pop(budget_module_name, None)
            if saved_budget_module is not sentinel:
                sys.modules[budget_module_name] = saved_budget_module
            for name, previous in saved.items():
                if previous is sentinel:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

    def service_for(self, gateway, post_delete_scan_manager=None):
        harness = self

        class Provider:
            def resolve(self, require_machine_id=False):
                harness.require_machine_id = require_machine_id
                return PlexConnection("http://plex.local:32400", "machine-1", "secret")

        self.module.PlexMateProvider = Provider
        self.module.PlexGateway = lambda *args, **kwargs: gateway
        return self.module.DeleteService(post_delete_scan_manager)


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

    def list_sections(self):
        return [
            LibrarySection(
                "7",
                "Movies",
                "movie",
                locations=("/media/movies",),
            )
        ]


class DeleteFlowTest(unittest.TestCase):
    @staticmethod
    def quarantine_plan(digest="a" * 64, subtitle_count=1):
        plan = _Record(
            plan_digest=digest,
            eligible=tuple(object() for _ in range(subtitle_count)),
            blocking=(),
        )

        def public_dict():
            return {
                "enabled": True,
                "backend": "quarantine",
                "status": "planned",
                "eligible": [
                    {
                        "source_path": "/media/movies/Delete Review/10.ko.srt",
                        "reason": "exclusive_to_deleted_video",
                    }
                ][:subtitle_count],
                "excluded": [],
                "counts": {
                    "eligible": subtitle_count,
                    "excluded": 0,
                    "protected": 1,
                    "quarantined": 0,
                },
                "plan_digest": digest,
            }

        plan.public_dict = public_dict
        return plan

    @staticmethod
    def direct_plan(digest="d" * 64, subtitle_count=1):
        plan = _Record(
            plan_digest=digest,
            eligible=tuple(object() for _ in range(subtitle_count)),
            blocking=(),
        )

        def public_dict():
            return {
                "enabled": True,
                "backend": "direct",
                "status": "planned",
                "video": {"path": "/media/movies/Delete Review/10.mkv"},
                "eligible": [
                    {
                        "source_path": "/media/movies/Delete Review/10.ko.srt",
                        "reason": "exclusive_to_deleted_video",
                    }
                ][:subtitle_count],
                "excluded": [],
                "counts": {
                    "eligible": subtitle_count,
                    "excluded": 0,
                    "protected": 1,
                    "deleted": 0,
                },
                "plan_digest": digest,
            }

        plan.public_dict = public_dict
        return plan

    def test_direct_preview_binds_exact_confirmation_without_mutation(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version("20", bitrate=2_000, path="/media/movies/Delete Review/20.mkv"),
        )
        harness = DeleteServiceHarness(before)
        harness.module.P.ModelSetting.values["setting_delete_backend"] = "direct"
        harness.module.P.ModelSetting.values["setting_post_delete_scan_mode"] = "web"
        harness.direct_plan = self.direct_plan()
        gateway = _Gateway([before])

        result = harness.service_for(gateway).preview(10, 1, 2)

        self.assertEqual(result["backend"], "direct")
        self.assertEqual(result["plan_digest"], "d" * 64)
        self.assertEqual(
            result["confirmation"],
            "DELETE MEDIA 10 SUBTITLES 1 %s" % ("d" * 12),
        )
        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(harness.direct_execute_calls, [])
        self.assertEqual(harness.run.deletion_attempts, 0)

    def test_direct_execution_delegates_hybrid_delete_and_requires_scan(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version("20", bitrate=2_000, path="/media/movies/Delete Review/20.mkv"),
        )
        harness = DeleteServiceHarness(before)
        harness.module.P.ModelSetting.values["setting_delete_backend"] = "direct"
        harness.module.P.ModelSetting.values["setting_post_delete_scan_mode"] = "web"
        harness.direct_plan = self.direct_plan()
        gateway = _Gateway([before])

        class ScanManager:
            def __init__(self):
                self.calls = []
                self.wake_count = 0

            def enqueue_confirmed(self, **kwargs):
                self.calls.append(kwargs)
                return [types.SimpleNamespace(id=702)]

            def wake(self):
                self.wake_count += 1

        manager = ScanManager()
        result = harness.service_for(gateway, manager).delete(
            10,
            1,
            2,
            "DELETE MEDIA 10 SUBTITLES 1 %s" % ("d" * 12),
            plan_digest="d" * 64,
        )

        self.assertEqual(result["verification"], "deleted_pending_scan")
        self.assertEqual(result["post_delete_scan"]["job_ids"], [702])
        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(len(harness.direct_execute_calls), 1)
        self.assertEqual(
            harness.direct_execute_calls[0][1]["expected_digest"], "d" * 64
        )
        self.assertIs(harness.direct_execute_calls[0][1]["gateway"], gateway)
        self.assertIs(harness.direct_execute_calls[0][1]["current_item"], before)
        self.assertEqual(len(manager.calls), 1)
        self.assertEqual(manager.wake_count, 1)
        self.assertEqual(harness.run.deletion_attempts, 1)
        self.assertFalse(harness.candidates[1].deleted)

    def test_direct_digest_drift_blocks_before_unlink_and_pms_delete(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version("20", bitrate=2_000, path="/media/movies/Delete Review/20.mkv"),
        )
        harness = DeleteServiceHarness(before)
        harness.module.P.ModelSetting.values["setting_delete_backend"] = "direct"
        harness.module.P.ModelSetting.values["setting_post_delete_scan_mode"] = "web"
        harness.direct_plan = self.direct_plan(digest="e" * 64)
        gateway = _Gateway([before])

        with self.assertRaisesRegex(ValueError, "사전확인"):
            harness.service_for(gateway, object()).delete(
                10,
                1,
                2,
                "DELETE MEDIA 10 SUBTITLES 1 %s" % ("d" * 12),
                plan_digest="d" * 64,
            )

        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(harness.direct_execute_calls, [])
        self.assertEqual(harness.run.deletion_attempts, 0)

    def test_quarantine_preview_is_read_only_and_binds_confirmation_to_digest(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version("20", bitrate=2_000, path="/media/movies/Delete Review/20.mkv"),
        )
        harness = DeleteServiceHarness(before)
        harness.module.P.ModelSetting.values["setting_delete_backend"] = "quarantine"
        harness.module.P.ModelSetting.values["setting_post_delete_scan_mode"] = "web"
        harness.quarantine_plan = self.quarantine_plan()
        gateway = _Gateway([before])

        result = harness.service_for(gateway).preview(10, 1, 2)

        self.assertEqual(result["backend"], "quarantine")
        self.assertEqual(result["plan_digest"], "a" * 64)
        self.assertEqual(
            result["confirmation"],
            "QUARANTINE 10 SUBTITLES 1 %s" % ("a" * 12),
        )
        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(harness.quarantine_stage_calls, [])
        self.assertEqual(harness.session.logs, [])
        self.assertEqual(harness.run.deletion_attempts, 0)

    def test_quarantine_execution_never_calls_plex_delete_and_queues_partial_scan(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version("20", bitrate=2_000, path="/media/movies/Delete Review/20.mkv"),
        )
        harness = DeleteServiceHarness(before)
        harness.module.P.ModelSetting.values["setting_delete_backend"] = "quarantine"
        harness.module.P.ModelSetting.values["setting_post_delete_scan_mode"] = "web"
        harness.quarantine_plan = self.quarantine_plan()
        gateway = _Gateway([before])

        class ScanManager:
            def __init__(self):
                self.calls = []
                self.wake_count = 0

            def enqueue_confirmed(self, **kwargs):
                self.calls.append(kwargs)
                return [types.SimpleNamespace(id=701)]

            def wake(self):
                self.wake_count += 1

        manager = ScanManager()
        result = harness.service_for(gateway, manager).delete(
            10,
            1,
            2,
            "QUARANTINE 10 SUBTITLES 1 %s" % ("a" * 12),
            plan_digest="a" * 64,
        )

        self.assertEqual(result["verification"], "quarantined_pending_scan")
        self.assertEqual(result["post_delete_scan"], {
            "mode": "web",
            "status": "queued",
            "job_ids": [701],
        })
        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(len(harness.quarantine_stage_calls), 1)
        self.assertEqual(
            harness.quarantine_stage_calls[0][1]["expected_digest"],
            "a" * 64,
        )
        self.assertEqual(len(manager.calls), 1)
        self.assertEqual(manager.wake_count, 1)
        self.assertEqual(harness.run.deletion_attempts, 1)
        self.assertFalse(harness.candidates[1].deleted)
        self.assertEqual(harness.group.resolution_status, "delete_in_progress")

    def test_quarantine_digest_drift_blocks_before_stage_and_never_calls_plex_delete(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version("20", bitrate=2_000, path="/media/movies/Delete Review/20.mkv"),
        )
        harness = DeleteServiceHarness(before)
        harness.module.P.ModelSetting.values["setting_delete_backend"] = "quarantine"
        harness.module.P.ModelSetting.values["setting_post_delete_scan_mode"] = "web"
        harness.quarantine_plan = self.quarantine_plan(digest="b" * 64)
        gateway = _Gateway([before])

        with self.assertRaisesRegex(ValueError, "사전확인"):
            harness.service_for(gateway, object()).delete(
                10,
                1,
                2,
                "QUARANTINE 10 SUBTITLES 1 %s" % ("a" * 12),
                plan_digest="a" * 64,
            )

        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(harness.quarantine_stage_calls, [])
        self.assertEqual(harness.run.deletion_attempts, 0)

    def test_lease_loss_after_quarantine_stage_never_enqueues_or_calls_plex_delete(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version("20", bitrate=2_000, path="/media/movies/Delete Review/20.mkv"),
        )
        harness = DeleteServiceHarness(before)
        harness.module.P.ModelSetting.values["setting_delete_backend"] = "quarantine"
        harness.module.P.ModelSetting.values["setting_post_delete_scan_mode"] = "web"
        harness.quarantine_plan = self.quarantine_plan()
        harness.lose_lease_after_stage = True
        gateway = _Gateway([before])

        class ScanManager:
            def __init__(self):
                self.calls = []

            def enqueue_confirmed(self, **kwargs):
                self.calls.append(kwargs)
                return [types.SimpleNamespace(id=701)]

            def wake(self):
                pass

        manager = ScanManager()
        with self.assertRaises(Exception):
            harness.service_for(gateway, manager).delete(
                10,
                1,
                2,
                "QUARANTINE 10 SUBTITLES 1 %s" % ("a" * 12),
                plan_digest="a" * 64,
            )

        self.assertEqual(len(harness.quarantine_stage_calls), 1)
        self.assertEqual(manager.calls, [])
        self.assertEqual(gateway.delete_calls, [])

    def test_quarantine_postscan_classifies_verified_trash_pending_and_unsafe_drift(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version("20", bitrate=2_000, path="/media/movies/Delete Review/20.mkv"),
        )
        harness = DeleteServiceHarness(before)
        retry_module = types.ModuleType("delete_review.post_delete_scan")

        class Retryable(RuntimeError):
            pass

        retry_module.PostDeleteScanRetryable = Retryable
        missing_target = replace(
            before.media[0],
            parts=tuple(replace(part, exists=False) for part in before.media[0].parts),
        )
        with mock.patch.dict(
            sys.modules,
            {"delete_review.post_delete_scan": retry_module},
        ):
            self.assertEqual(
                harness.module.DeleteService._quarantine_snapshot_state(
                    before.as_dict(), _item(before.media[1]), "10"
                ),
                "verified",
            )
            self.assertEqual(
                harness.module.DeleteService._quarantine_snapshot_state(
                    before.as_dict(), _item(missing_target, before.media[1]), "10"
                ),
                "trash_pending",
            )
            with self.assertRaises(Retryable):
                harness.module.DeleteService._quarantine_snapshot_state(
                    before.as_dict(), before, "10"
                )
            with self.assertRaisesRegex(RuntimeError, "유지 Media"):
                harness.module.DeleteService._quarantine_snapshot_state(
                    before.as_dict(),
                    _item(_version("20", bitrate=2_001)),
                    "10",
                )
            with self.assertRaisesRegex(RuntimeError, "Media 집합"):
                harness.module.DeleteService._quarantine_snapshot_state(
                    before.as_dict(),
                    _item(before.media[1], _version("30")),
                    "10",
                )

    def test_confirmation_is_byte_exact_and_rejects_surrounding_space(self) -> None:
        before = _item(_version("10"), _version("20"))
        harness = DeleteServiceHarness(before)
        gateway = _Gateway([])

        with self.assertRaises(ValueError):
            harness.service_for(gateway).delete(10, 1, 2, " DELETE 10")

        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(harness.session.logs, [])
        self.assertEqual(harness.run.deletion_attempts, 0)
        self.assertTrue(harness.group.safe_to_delete)
        self.assertEqual(harness.group.resolution_status, "open")
        self.assertEqual(harness.lease_events[0][0], "acquire")
        self.assertEqual(harness.lease_events[-1][0], "release")

    def test_legacy_limit_is_ignored_and_attempt_counter_remains_auditable(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version(
                "20",
                bitrate=2_000,
                path="/media/movies/Delete Review/20.mkv",
            ),
        )
        after = _item(before.media[1])
        harness = DeleteServiceHarness(before)
        harness.run.deletion_attempts = 1
        preview_gateway = _Gateway([before])
        service = harness.service_for(preview_gateway)

        preview = service.preview(10, 1, 2)
        self.assertTrue(harness.require_machine_id)
        self.assertEqual(preview_gateway.metadata_results, [])
        self.assertEqual(harness.quarantine_preview_calls, [])
        self.assertEqual(harness.session.logs, [])
        self.assertEqual(
            preview["delete_budget"],
            {
                "unlimited": True,
                "attempted": 1,
                "limit": None,
                "remaining": None,
                "exhausted": False,
            },
        )

        result = harness.service_for(_Gateway([before, after])).delete(
            10, 1, 2, "DELETE 10"
        )
        self.assertEqual(result["verification"], "confirmed")
        self.assertEqual(harness.run.deletion_attempts, 2)
        self.assertEqual(harness.run.successful_deletions, 1)

    def test_confirmed_delete_updates_audit_and_requires_rescan(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version(
                "20",
                bitrate=2_000,
                path="/media/movies/Delete Review/20.mkv",
            ),
        )
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
        self.assertEqual(harness.lease_events[0][0], "acquire")
        self.assertEqual(harness.lease_events[-1][0], "release")

    def test_confirmed_success_enqueues_before_success_commit_and_wakes_after_release(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version(
                "20",
                bitrate=2_000,
                path="/media/movies/Delete Review/20.mkv",
            ),
        )
        after = _item(before.media[1])
        harness = DeleteServiceHarness(before)
        harness.module.P.ModelSetting.values["setting_post_delete_scan_mode"] = "web"

        class ScanManager:
            def __init__(self):
                self.calls = []
                self.commit_counts = []
                self.wake_count = 0

            def enqueue_confirmed(self, **kwargs):
                self.calls.append(kwargs)
                self.commit_counts.append(harness.session.commits)
                self.asserted_success_state = (
                    kwargs["action_log"].status,
                    kwargs["candidate"].deleted,
                )
                return [types.SimpleNamespace(id=None)]

            def wake(self):
                self.wake_count += 1

        manager = ScanManager()
        commits_before = harness.session.commits
        result = harness.service_for(
            _Gateway([before, after]), manager
        ).delete(10, 1, 2, "DELETE 10")

        self.assertEqual(result["verification"], "confirmed")
        self.assertEqual(result["post_delete_scan"]["status"], "queued")
        self.assertEqual(len(manager.calls), 1)
        self.assertEqual(manager.asserted_success_state, ("success", True))
        self.assertEqual(manager.commit_counts, [harness.session.commits - 1])
        self.assertGreater(harness.session.commits, commits_before)
        self.assertEqual(manager.wake_count, 1)
        self.assertEqual(harness.lease_events[-1][0], "release")

    def test_none_mode_never_enqueues_post_delete_scan(self) -> None:
        before = _item(_version("10"), _version("20", bitrate=2_000))
        after = _item(before.media[1])
        harness = DeleteServiceHarness(before)

        class ScanManager:
            def __init__(self):
                self.wake_count = 0

            def enqueue_confirmed(self, **kwargs):
                raise AssertionError("none mode must not enqueue")

            def wake(self):
                self.wake_count += 1

        manager = ScanManager()
        result = harness.service_for(
            _Gateway([before, after]), manager
        ).delete(10, 1, 2, "DELETE 10")

        self.assertEqual(result["verification"], "confirmed")
        self.assertEqual(
            result["post_delete_scan"],
            {"mode": "none", "status": "disabled", "job_ids": []},
        )

    def test_unconfirmed_delete_never_enqueues_post_delete_scan(self) -> None:
        before = _item(
            _version("10", path="/media/movies/Delete Review/10.mkv"),
            _version("20", path="/media/movies/Delete Review/20.mkv"),
        )
        harness = DeleteServiceHarness(before)
        harness.module.P.ModelSetting.values["setting_post_delete_scan_mode"] = "web"

        class ScanManager:
            def __init__(self):
                self.enqueue_count = 0
                self.wake_count = 0

            def enqueue_confirmed(self, **kwargs):
                self.enqueue_count += 1

            def wake(self):
                self.wake_count += 1

        manager = ScanManager()
        gateway = _Gateway(
            [before, PlexGatewayError("post-read unavailable")],
            delete_result=PlexDeleteOutcomeUnknown("unknown"),
        )

        with self.assertRaises(RuntimeError):
            harness.service_for(gateway, manager).delete(10, 1, 2, "DELETE 10")

        self.assertEqual(manager.enqueue_count, 0)
        self.assertEqual(manager.wake_count, 0)

    def test_episode_scan_target_must_remain_inside_delete_allowed_root(self) -> None:
        before = MetadataItem(
            rating_key="100",
            guid="plex://episode/delete-review",
            media_type="episode",
            title="Episode",
            grandparent_title="Example Show",
            grandparent_rating_key="50",
            parent_index=1,
            index=2,
            media=(
                _version(
                    "10",
                    path="/media/tv/Example Show/Season 01/10.mkv",
                ),
                _version(
                    "20",
                    bitrate=2_000,
                    path="/media/tv/Example Show/Season 01/20.mkv",
                ),
            ),
        )
        harness = DeleteServiceHarness(before)
        harness.module.P.ModelSetting.values["setting_post_delete_scan_mode"] = "web"
        harness.module.current_safety_policy = lambda: SafetyPolicy(
            allowed_roots=("/media/tv/Example Show/Season 01",),
            require_guid=True,
            block_multipart=True,
            require_allowed_roots=True,
        )

        class EpisodeGateway(_Gateway):
            def list_sections(self):
                return [
                    LibrarySection(
                        "7",
                        "TV",
                        "show",
                        locations=("/media/tv",),
                    )
                ]

        gateway = EpisodeGateway([before])
        with self.assertRaisesRegex(RuntimeError, "부분 스캔 폴더"):
            harness.service_for(gateway, object()).delete(
                10, 1, 2, "DELETE 10"
            )

        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(harness.run.deletion_attempts, 0)
        self.assertEqual(harness.session.logs[-1].status, "blocked")

    def test_batch_call_reuses_global_lease_without_releasing_it_per_item(self) -> None:
        before = _item(_version("10"), _version("20", bitrate=2_000))
        after = _item(before.media[1])
        harness = DeleteServiceHarness(before)
        service = harness.service_for(_Gateway([before, after]))

        result = service.delete(
            10,
            1,
            2,
            "DELETE 10",
            lease_owner_token="batch-lease",
            lease_owner_kind="batch",
            lease_owner_ref="77",
        )

        self.assertEqual(result["verification"], "confirmed")
        self.assertTrue(harness.lease_events)
        self.assertTrue(all(event[0] == "renew" for event in harness.lease_events))

    def test_cross_group_part_path_conflict_blocks_manual_delete_under_lease(self) -> None:
        before = _item(_version("10"), _version("20"))
        harness = DeleteServiceHarness(before)
        harness.path_conflict = True
        gateway = _Gateway([])

        with self.assertRaisesRegex(RuntimeError, "Part 파일 경로"):
            harness.service_for(gateway).delete(10, 1, 2, "DELETE 10")

        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(harness.session.logs[-1].status, "blocked")
        self.assertEqual(harness.group.resolution_status, "manual_check_required")
        self.assertEqual(harness.run.deletion_attempts, 0)
        self.assertEqual(harness.lease_events[0][0], "acquire")
        self.assertEqual(harness.lease_events[-1][0], "release")

    def test_lost_lease_leaves_audit_for_recovery_cas_owner(self) -> None:
        before = _item(_version("10"), _version("20"))
        harness = DeleteServiceHarness(before)
        harness.lose_lease_on_renew = True
        gateway = _Gateway([before])

        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            harness.service_for(gateway).delete(10, 1, 2, "DELETE 10")

        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(harness.session.logs[-1].status, "validating")
        self.assertEqual(harness.session.logs[-1].message, "삭제 전 재검증 중")
        self.assertEqual(harness.group.resolution_status, "delete_in_progress")
        self.assertEqual(harness.run.deletion_attempts, 0)

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

    def test_high_attempt_counter_does_not_block_another_delete(self) -> None:
        before = _item(_version("10"), _version("20"))
        after = _item(before.media[1])
        harness = DeleteServiceHarness(before)
        harness.module.P.ModelSetting.values["setting_max_delete_per_run"] = "1"
        harness.run.deletion_attempts = 999
        gateway = _Gateway([before, after])

        result = harness.service_for(gateway).delete(10, 1, 2, "DELETE 10")
        self.assertEqual(result["verification"], "confirmed")
        self.assertEqual(gateway.delete_calls, [("100", "10")])
        self.assertEqual(harness.run.deletion_attempts, 1000)

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

    def test_hot_reload_does_not_recover_audit_owned_by_live_batch_worker(self) -> None:
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
            status="deleting",
            message="deleting",
        )
        harness.session.add(log)

        counts = harness.service_for(_Gateway([])).recover_interrupted({(1, 10, 1)})

        self.assertEqual(counts, {"blocked": 0, "unknown": 0})
        self.assertEqual(log.status, "deleting")
        self.assertEqual(harness.group.resolution_status, "delete_in_progress")

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
