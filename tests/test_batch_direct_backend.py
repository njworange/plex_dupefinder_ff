from __future__ import annotations

import json
import sys
import types
import unittest

from test_batch_backend import _Record, _Session
from test_flaskfarm_compat import FlaskFarmImportHarness, PACKAGE_NAME


class BatchDirectBackendSafetyTest(unittest.TestCase):
    @staticmethod
    def _module():
        return sys.modules[PACKAGE_NAME + ".batch_delete_manager"]

    @staticmethod
    def _cleanup():
        return {
            "enabled": True,
            "backend": "direct",
            "status": "planned",
            "video": {"path": "C:/media/Movie.mkv", "size": 10},
            "eligible": [
                {
                    "path": "C:/media/Movie.ko.srt",
                    "reason": "exclusive_to_deleted_video",
                }
            ],
            "excluded": [],
            "protected": [
                {
                    "path": "C:/media/Movie.keep.ko.srt",
                    "reason": "protected_for_surviving_video",
                }
            ],
            "executable": True,
            "counts": {
                "eligible": 1,
                "excluded": 0,
                "protected": 0,
                "deleted": 0,
            },
        }

    def test_fresh_direct_preview_requires_exact_digest_and_runtime_binding(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module()
            harness.setup_module.P.ModelSetting._data.update(
                {
                    "setting_delete_backend": "direct",
                    "setting_post_delete_scan_mode": "web",
                    # A stale quarantine path must not bind a direct plan.
                    "setting_quarantine_root": "C:/unused-quarantine",
                }
            )
            digest = "a" * 64
            item = _Record(group_id=1, delete_candidate_id=2, keep_candidate_id=3)
            journal = _Record(
                plan_digest=digest,
                manifest_json=json.dumps(
                    {
                        "batch_binding": {
                            "backend": "direct",
                            "post_delete_scan_mode": "web",
                            "quarantine_root": "",
                        }
                    }
                ),
            )
            manager = module.BatchDeleteManager(
                types.SimpleNamespace(
                    preview=lambda **kwargs: {
                        "backend": "direct",
                        "plan_digest": digest,
                        "confirmation": "approved",
                        "subtitle_cleanup": self._cleanup(),
                    }
                )
            )

            self.assertEqual(
                manager._fresh_direct_preview(item, journal)["plan_digest"], digest
            )
            journal.plan_digest = "b" * 64
            with self.assertRaisesRegex(RuntimeError, "계획"):
                manager._fresh_direct_preview(item, journal)
            journal.plan_digest = digest
            harness.setup_module.P.ModelSetting._data["setting_post_delete_scan_mode"] = (
                "binary"
            )
            with self.assertRaisesRegex(RuntimeError, "설정"):
                manager._fresh_direct_preview(item, journal)

    def test_blocking_direct_preview_cannot_be_reused_for_batch_approval(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module()
            harness.setup_module.P.ModelSetting._data.update(
                {
                    "setting_delete_backend": "direct",
                    "setting_post_delete_scan_mode": "web",
                }
            )
            digest = "a" * 64
            item = _Record(group_id=1, delete_candidate_id=2, keep_candidate_id=3)
            journal = _Record(
                plan_digest=digest,
                manifest_json=json.dumps(
                    {
                        "batch_binding": {
                            "backend": "direct",
                            "post_delete_scan_mode": "web",
                            "quarantine_root": "",
                        }
                    }
                ),
            )
            cleanup = self._cleanup()
            cleanup["executable"] = False
            manager = module.BatchDeleteManager(
                types.SimpleNamespace(
                    preview=lambda **kwargs: {
                        "backend": "direct",
                        "plan_digest": digest,
                        "subtitle_cleanup": cleanup,
                    }
                )
            )

            with self.assertRaisesRegex(RuntimeError, "보호"):
                manager._fresh_direct_preview(item, journal)

    def test_batch_manifest_keeps_public_protected_paths_only(self) -> None:
        with FlaskFarmImportHarness() as _harness:
            module = self._module()
            cleanup = self._cleanup()
            cleanup["protected"][0]["backup_path"] = "C:/private/op-secret"
            manifest = module.BatchDeleteManager._preview_manifest(
                cleanup,
                {
                    "backend": "direct",
                    "post_delete_scan_mode": "web",
                    "quarantine_root": "",
                },
            )

            self.assertEqual(
                manifest["protected"][0]["path"],
                "C:/media/Movie.keep.ko.srt",
            )
            # Production cleanup serializers omit this key; the batch layer
            # must never invent backup internals of its own.
            self.assertNotIn("operation_paths", manifest)
            self.assertNotIn("backup_snapshot", manifest)
            self.assertNotIn("backup_path", json.dumps(manifest))

    def test_cross_item_target_and_protected_path_conflict_is_blocked(self) -> None:
        with FlaskFarmImportHarness() as _harness:
            module = self._module()
            shared = "C:/media/Shared.ko.srt"
            first = {
                "subtitle_cleanup": {
                    "video": {"path": "C:/media/One.mkv"},
                    "eligible": [{"path": shared}],
                    "excluded": [],
                    "protected": [],
                }
            }
            second = {
                "subtitle_cleanup": {
                    "video": {"path": "C:/media/Two.mkv"},
                    "eligible": [],
                    "excluded": [],
                    "protected": [{"path": shared}],
                }
            }

            with self.assertRaisesRegex(RuntimeError, "공유"):
                module.BatchDeleteManager._assert_source_sets_disjoint(
                    [first, second]
                )

    def test_legacy_file_delete_confirmation_is_never_treated_as_hybrid(self) -> None:
        with FlaskFarmImportHarness() as _harness:
            module = self._module()
            batch = _Record(
                confirmation=(
                    "BATCH DELETE FILES 7 ITEMS 2 SUBTITLES 1 aaaaaaaaaaaa"
                )
            )
            self.assertEqual(
                module.BatchDeleteManager._planned_backend(batch),
                "legacy_direct",
            )

    def _assert_backend_drift_blocked(self, selected_backend: str) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module()
            harness.setup_module.P.ModelSetting._data.update(
                {
                    "setting_delete_backend": selected_backend,
                    "setting_post_delete_scan_mode": "web",
                }
            )
            item = _Record(
                delete_candidate_id=2,
                group_id=4,
                keep_candidate_id=3,
                keep_media_id="30",
                delete_media_id="20",
            )
            batch = _Record(
                id=1,
                scan_run_id=9,
                total_items=1,
                confirmation=(
                    "BATCH DELETE MEDIA 1 ITEMS 1 SUBTITLES 1 aaaaaaaaaaaa"
                ),
            )
            journal = _Record(plan_digest="a" * 64)
            original = module.ModelDirectDeleteJournal

            class Journals:
                @classmethod
                def for_batch_candidate(cls, batch_id, candidate_id, status=""):
                    return journal

            module.ModelDirectDeleteJournal = Journals
            manager = module.BatchDeleteManager(types.SimpleNamespace())
            manager._assert_settings_snapshot = lambda run: None
            manager._cross_group_path_conflicts = lambda run_id: set()
            manager._eligible_pair = lambda group: (
                _Record(id=3, media_id="30"),
                _Record(id=2, media_id="20"),
            )
            module.ModelScanRun = types.SimpleNamespace(
                get=lambda run_id: _Record(id=9, status="completed", deletion_attempts=0)
            )
            module.ModelBatchItem = types.SimpleNamespace(by_batch=lambda batch_id: [item])
            module.ModelDuplicateGroup = types.SimpleNamespace(
                get=lambda group_id: _Record(id=group_id, run_id=9)
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "파일 처리 방식"):
                    manager._validate_plan_unchanged(batch)
            finally:
                module.ModelDirectDeleteJournal = original

    def test_direct_preview_never_drifts_to_plex(self) -> None:
        self._assert_backend_drift_blocked("plex")

    def test_direct_preview_never_drifts_to_quarantine(self) -> None:
        self._assert_backend_drift_blocked("quarantine")

    def test_worker_keeps_direct_item_pending_until_scan_verification(self) -> None:
        with FlaskFarmImportHarness() as harness:
            module = self._module()
            harness.setup_module.P.ModelSetting._data.update(
                {
                    "setting_delete_enabled": "True",
                    "setting_batch_delete_enabled": "True",
                    "setting_delete_backend": "direct",
                    "setting_post_delete_scan_mode": "web",
                }
            )
            batch = _Record(
                id=1,
                scan_run_id=9,
                status="queued",
                total_items=1,
                processed_items=0,
                succeeded_items=0,
                failed_items=0,
                skipped_items=0,
                lease_key="global",
                nonce_hash="",
                current_message="",
                error_summary="",
                finished_at=None,
                deletion_lease_token="batch-lease",
                confirmation=(
                    "BATCH DELETE MEDIA 1 ITEMS 1 SUBTITLES 1 aaaaaaaaaaaa"
                ),
            )
            item = _Record(
                id=2,
                batch_run_id=1,
                scan_run_id=9,
                group_id=3,
                delete_candidate_id=4,
                keep_candidate_id=5,
                delete_media_id="40",
                status="planned",
                message="",
                action_log_id=None,
                started_at=None,
                finished_at=None,
            )

            class Batches:
                @classmethod
                def claim_for_worker(cls, batch_id, now):
                    batch.status = "running"
                    return True

                @classmethod
                def get(cls, batch_id):
                    return batch

            class Items:
                @classmethod
                def by_batch(cls, batch_id):
                    return [item]

                @classmethod
                def get(cls, item_id):
                    return item

                @classmethod
                def claim_for_worker(cls, item_id, now):
                    item.status = "running"
                    item.started_at = now
                    return True

            class Deletes:
                def __init__(self):
                    self.calls = []

                def delete(self, **kwargs):
                    self.calls.append(kwargs)
                    return {
                        "action_id": 77,
                        "verification": "deleted_pending_scan",
                    }

            module.ModelBatchRun = Batches
            module.ModelBatchItem = Items
            module.F.db.session = _Session()
            deletes = Deletes()
            manager = module.BatchDeleteManager(deletes)
            manager.lease_service = types.SimpleNamespace(
                renew=lambda *args: None,
                release=lambda *args: True,
            )
            manager._worker_should_stop = lambda batch_id: None
            journal = _Record(plan_digest="a" * 64)
            manager._direct_preview_journal = lambda *args: journal
            manager._fresh_direct_preview = lambda *args: {
                "confirmation": "DELETE MEDIA 40 SUBTITLES 1 aaaaaaaaaaaa"
            }

            manager._worker(1)

            self.assertEqual(len(deletes.calls), 1)
            self.assertEqual(deletes.calls[0]["plan_digest"], "a" * 64)
            self.assertEqual(item.status, "scan_pending")
            self.assertIsNone(item.finished_at)
            self.assertEqual(batch.status, "scan_pending")
            self.assertEqual(batch.succeeded_items, 0)
            self.assertEqual(batch.processed_items, 0)

    def test_startup_recovery_cannot_skip_direct_journal_before_scan_outbox(self) -> None:
        """Cover the crash gap after unlink commit and before scan-job commit."""

        with FlaskFarmImportHarness() as harness:
            module = self._module()
            module.F.db.session = _Session()
            direct_pending = _Record(id=71, status="deleted_pending_scan")
            module.ModelBatchRun = types.SimpleNamespace(unfinished=lambda: [])
            module.ModelActionLog = types.SimpleNamespace(interrupted=lambda: [])
            module.ModelQuarantineJournal = types.SimpleNamespace(unfinished=lambda: [])
            module.ModelDirectDeleteJournal = types.SimpleNamespace(
                unfinished=lambda: [direct_pending]
            )
            recovery_calls = []

            class DeleteService:
                def recover_interrupted(self, **kwargs):
                    recovery_calls.append(kwargs)
                    return {"blocked": 0, "unknown": 1}

            manager = module.BatchDeleteManager(DeleteService())
            released = []
            manager.lease_service = types.SimpleNamespace(
                recovery_state=lambda: "free",
                acquire_for_recovery=lambda: _Record(token="recovery-token"),
                release=lambda token: released.append(token) or True,
            )

            self.assertEqual(manager.recover_interrupted(), 0)
            self.assertEqual(
                recovery_calls,
                [
                    {
                        "recovery_lease_token": "recovery-token",
                        "recovery_lease_owner_ref": "plugin_load",
                    }
                ],
            )
            self.assertEqual(released, ["recovery-token"])
            self.assertEqual(
                manager.last_delete_recovery_counts,
                {"blocked": 0, "unknown": 1},
            )


if __name__ == "__main__":
    unittest.main()
