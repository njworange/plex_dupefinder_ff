from __future__ import annotations

import types
import unittest

from test_delete_flow import DeleteServiceHarness, _Gateway, _Record, _item, _version


class _DirectManager:
    def __init__(self, digest="a" * 64, subtitle_count=1) -> None:
        self.digest = digest
        self.subtitle_count = subtitle_count
        self.preview_calls = []
        self.execute_calls = []
        self.recovery_calls = []
        self.journal = _Record(
            id=902,
            status="deleted_pending_scan",
            last_error="",
            updated_at=None,
        )

        def cleanup_api(include_paths=True):
            return {
                "enabled": True,
                "backend": "direct",
                "status": self.journal.status,
                "eligible": (
                    [
                        {
                            "source_path": (
                                "/media/movies/Delete Review/10.ko.srt"
                            ),
                            "reason": "exclusive_to_deleted_video",
                            "deleted": True,
                        }
                    ][: self.subtitle_count]
                    if include_paths
                    else []
                ),
                "excluded": [],
                "counts": {
                    "eligible": self.subtitle_count,
                    "excluded": 0,
                    "protected": 1,
                    "deleted": self.subtitle_count,
                },
            }

        self.journal.cleanup_api = cleanup_api

    def preview(self, *args, **kwargs):
        self.preview_calls.append((args, kwargs))
        plan = _Record(
            plan_digest=self.digest,
            eligible=tuple(object() for _value in range(self.subtitle_count)),
            blocking=(),
        )

        def public_dict():
            value = self.journal.cleanup_api(True)
            value.update(
                {
                    "status": "planned",
                    "video": {
                        "path": "/media/movies/Delete Review/10.mkv",
                        "size": 10,
                    },
                    "plan_digest": self.digest,
                }
            )
            value["counts"]["deleted"] = 0
            for item in value["eligible"]:
                item.pop("deleted", None)
            return value

        plan.public_dict = public_dict
        return plan

    def execute(self, *args, **kwargs):
        self.execute_calls.append((args, kwargs))
        return self.journal

    def recover_interrupted(self, **kwargs):
        self.recovery_calls.append(kwargs)
        heartbeat = kwargs.get("heartbeat")
        if callable(heartbeat):
            heartbeat()
        return 0


class _BackupBlockedManager(_DirectManager):
    def execute(self, *args, **kwargs):
        self.execute_calls.append((args, kwargs))
        action_log = kwargs["action_log"]
        group = kwargs["group"]
        action_log.status = "blocked"
        action_log.message = (
            "stage=backup_sidecars; error=OSError; errno=none; "
            "journal=902; action=%s" % action_log.id
        )
        group.safe_to_delete = False
        group.resolution_status = "open"
        group.safety_flags_json = '["direct_delete_repreview_required"]'
        raise RuntimeError(
            "Plex Media DELETE 전 안전 검증에 실패했습니다. 원본 삭제는 시작되지 않았습니다."
        )


class _ScanManager:
    def __init__(self, jobs=None) -> None:
        self.jobs = [types.SimpleNamespace(id=701)] if jobs is None else jobs
        self.calls = []
        self.wake_count = 0

    def enqueue_confirmed(self, **kwargs):
        self.calls.append(kwargs)
        return self.jobs

    def wake(self):
        self.wake_count += 1


class DirectDeleteServiceOrchestrationSafetyTest(unittest.TestCase):
    @staticmethod
    def item():
        return _item(
            _version(
                "10", path="/media/movies/Delete Review/10.mkv"
            ),
            _version(
                "20",
                bitrate=2_000,
                path="/media/movies/Delete Review/20.mkv",
            ),
        )

    @staticmethod
    def configure(harness: DeleteServiceHarness, scan_mode="web") -> None:
        harness.module.P.ModelSetting.values["setting_delete_backend"] = "direct"
        harness.module.P.ModelSetting.values["setting_post_delete_scan_mode"] = scan_mode

    def test_preview_is_read_only_and_binds_exact_direct_confirmation(self) -> None:
        before = self.item()
        harness = DeleteServiceHarness(before)
        self.configure(harness)
        gateway = _Gateway([before])
        direct = _DirectManager()
        service = harness.service_for(gateway)
        service.direct_delete_manager = direct

        result = service.preview(10, 1, 2)

        self.assertEqual(result["backend"], "direct")
        self.assertEqual(result["plan_digest"], "a" * 64)
        self.assertEqual(
            result["confirmation"],
            "DELETE MEDIA 10 SUBTITLES 1 %s" % ("a" * 12),
        )
        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(direct.execute_calls, [])
        self.assertEqual(harness.session.logs, [])
        self.assertEqual(harness.run.deletion_attempts, 0)

    def test_recovery_renews_singleton_lease_during_direct_restore(self) -> None:
        harness = DeleteServiceHarness(self.item())
        service = harness.service_for(_Gateway([]))
        direct = _DirectManager()
        renewals = []
        service.direct_delete_manager = direct
        service.lease_service = types.SimpleNamespace(
            renew=lambda token, kind, ref: renewals.append((token, kind, ref))
        )

        service.recover_interrupted(
            recovery_lease_token="recovery-token",
            recovery_lease_owner_ref="batch:77",
        )

        self.assertEqual(len(direct.recovery_calls), 1)
        self.assertEqual(
            renewals,
            [("recovery-token", "recovery", "batch:77")],
        )

    def test_execution_delegates_hybrid_delete_and_requires_durable_scan_enqueue(self) -> None:
        before = self.item()
        harness = DeleteServiceHarness(before)
        self.configure(harness)
        gateway = _Gateway([before])
        direct = _DirectManager()
        scans = _ScanManager()
        service = harness.service_for(gateway, scans)
        service.direct_delete_manager = direct

        result = service.delete(
            10,
            1,
            2,
            "DELETE MEDIA 10 SUBTITLES 1 %s" % ("a" * 12),
            plan_digest="a" * 64,
        )

        self.assertEqual(result["verification"], "deleted_pending_scan")
        self.assertEqual(result["post_delete_scan"]["job_ids"], [701])
        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(len(direct.execute_calls), 1)
        self.assertEqual(
            direct.execute_calls[0][1]["expected_digest"], "a" * 64
        )
        self.assertIs(direct.execute_calls[0][1]["gateway"], gateway)
        self.assertIs(direct.execute_calls[0][1]["current_item"], before)
        self.assertEqual(len(scans.calls), 1)
        self.assertEqual(scans.wake_count, 1)
        self.assertEqual(harness.run.deletion_attempts, 1)
        self.assertFalse(harness.candidates[1].deleted)
        self.assertEqual(harness.group.resolution_status, "delete_in_progress")

    def test_backup_blocked_state_survives_outer_catch_without_pms_delete(self) -> None:
        before = self.item()
        harness = DeleteServiceHarness(before)
        self.configure(harness)
        gateway = _Gateway([before])
        direct = _BackupBlockedManager()
        scans = _ScanManager()
        service = harness.service_for(gateway, scans)
        service.direct_delete_manager = direct

        with self.assertRaisesRegex(RuntimeError, "원본 삭제는 시작되지"):
            service.delete(
                10,
                1,
                2,
                "DELETE MEDIA 10 SUBTITLES 1 %s" % ("a" * 12),
                plan_digest="a" * 64,
            )

        self.assertEqual(len(direct.execute_calls), 1)
        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(scans.calls, [])
        self.assertEqual(harness.session.logs[-1].status, "blocked")
        self.assertIn("stage=backup_sidecars", harness.session.logs[-1].message)
        self.assertEqual(harness.group.resolution_status, "open")
        self.assertFalse(harness.group.safe_to_delete)
        self.assertIn("direct_delete_repreview_required", harness.group.safety_flags_json)

    def test_digest_or_confirmation_drift_blocks_before_filesystem_execution(self) -> None:
        for confirmation, expected_digest, fresh_digest in (
            (
                "DELETE MEDIA 10 SUBTITLES 1 %s" % ("a" * 12),
                "a" * 64,
                "b" * 64,
            ),
            (
                "DELETE MEDIA 10 SUBTITLES 0 %s" % ("a" * 12),
                "a" * 64,
                "a" * 64,
            ),
        ):
            with self.subTest(confirmation=confirmation, fresh=fresh_digest[:1]):
                before = self.item()
                harness = DeleteServiceHarness(before)
                self.configure(harness)
                gateway = _Gateway([before])
                direct = _DirectManager(digest=fresh_digest)
                service = harness.service_for(gateway, _ScanManager())
                service.direct_delete_manager = direct

                with self.assertRaisesRegex(ValueError, "사전확인"):
                    service.delete(
                        10,
                        1,
                        2,
                        confirmation,
                        plan_digest=expected_digest,
                    )

                self.assertEqual(gateway.delete_calls, [])
                self.assertEqual(direct.execute_calls, [])
                self.assertEqual(harness.run.deletion_attempts, 0)

    def test_none_scan_mode_and_missing_scan_manager_both_block_before_execution(self) -> None:
        cases = (("none", object()), ("web", None))
        for mode, scan_manager in cases:
            with self.subTest(mode=mode, manager=scan_manager is not None):
                before = self.item()
                harness = DeleteServiceHarness(before)
                self.configure(harness, mode)
                gateway = _Gateway([before])
                direct = _DirectManager()
                service = harness.service_for(gateway, scan_manager)
                service.direct_delete_manager = direct

                with self.assertRaises(RuntimeError):
                    service.delete(
                        10,
                        1,
                        2,
                        "DELETE MEDIA 10 SUBTITLES 1 %s" % ("a" * 12),
                        plan_digest="a" * 64,
                    )

                self.assertEqual(gateway.delete_calls, [])
                self.assertEqual(direct.execute_calls, [])
                self.assertEqual(harness.run.deletion_attempts, 0)

    def test_empty_scan_outbox_after_hybrid_delete_is_durably_manual_not_success(self) -> None:
        before = self.item()
        harness = DeleteServiceHarness(before)
        self.configure(harness)
        gateway = _Gateway([before])
        direct = _DirectManager()
        scans = _ScanManager(jobs=[])
        service = harness.service_for(gateway, scans)
        service.direct_delete_manager = direct
        harness.module.ModelDirectDeleteJournal = types.SimpleNamespace(
            get=lambda journal_id: direct.journal
            if int(journal_id) == direct.journal.id
            else None,
            for_action=lambda action_id: None,
        )
        harness.module.ModelActionLog.get = classmethod(
            lambda cls, action_id: next(
                (
                    value
                    for value in harness.session.logs
                    if int(value.id) == int(action_id)
                ),
                None,
            )
        )

        with self.assertRaisesRegex(RuntimeError, "영구 삭제되었지만"):
            service.delete(
                10,
                1,
                2,
                "DELETE MEDIA 10 SUBTITLES 1 %s" % ("a" * 12),
                plan_digest="a" * 64,
            )

        self.assertEqual(gateway.delete_calls, [])
        self.assertEqual(len(direct.execute_calls), 1)
        self.assertEqual(direct.journal.status, "recovery_required")
        self.assertEqual(harness.session.logs[-1].status, "unknown")
        self.assertEqual(harness.group.resolution_status, "manual_check_required")
        self.assertIn("direct_delete_scan_enqueue_failed", harness.group.safety_flags_json)


if __name__ == "__main__":
    unittest.main()
