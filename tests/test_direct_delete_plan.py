from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path

from services.direct_delete import DirectDeletePlanError, build_direct_delete_plan
from services.quarantine_delete import QuarantinePlanError
from test_flaskfarm_compat import FlaskFarmImportHarness


def _write(path: Path, value: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


class DirectDeletePlanSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pdff-direct-plan-")
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.folder = self.media / "Movie"
        self.delete_video = _write(
            self.folder / "Film.1080p.mkv", b"delete-video"
        )
        self.keep_video = _write(self.folder / "Film.2160p.mkv", b"keep-video")
        self.delete_subtitle = _write(
            self.folder / "Film.1080p.ko.srt", b"delete-subtitle"
        )
        self.keep_subtitle = _write(
            self.folder / "Film.2160p.ko.srt", b"keep-subtitle"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def plan(self, scan_mode: str = "web"):
        return build_direct_delete_plan(
            (str(self.delete_video),),
            (str(self.keep_video),),
            (str(self.media),),
            (str(self.media),),
            scan_mode,
        )

    def test_only_exclusive_target_subtitle_is_eligible(self) -> None:
        plan = self.plan()

        self.assertEqual([value.path for value in plan.eligible], [str(self.delete_subtitle)])
        self.assertIn(str(self.keep_subtitle), {value.path for value in plan.protected})
        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertTrue(self.keep_video.exists())
        self.assertTrue(self.keep_subtitle.exists())

    def test_plan_digest_binds_scan_mode_and_protected_snapshot(self) -> None:
        web = self.plan("web")
        binary = self.plan("binary")
        self.assertNotEqual(web.plan_digest, binary.plan_digest)

        self.keep_subtitle.write_bytes(b"changed-protected-subtitle")
        changed = self.plan("web")
        self.assertNotEqual(web.plan_digest, changed.plan_digest)

    def test_target_and_survivor_same_path_is_blocked(self) -> None:
        with self.assertRaisesRegex(DirectDeletePlanError, "같은 영상 파일"):
            build_direct_delete_plan(
                (str(self.delete_video),),
                (str(self.delete_video),),
                (str(self.media),),
                (str(self.media),),
                "web",
            )

    def test_target_and_survivor_same_inode_is_blocked(self) -> None:
        alias = self.folder / "Film.alias.mkv"
        try:
            os.link(str(self.delete_video), str(alias))
        except (NotImplementedError, OSError):
            self.skipTest("hard links are unavailable")

        with self.assertRaises((DirectDeletePlanError, QuarantinePlanError)):
            build_direct_delete_plan(
                (str(self.delete_video),),
                (str(alias),),
                (str(self.media),),
                (str(self.media),),
                "web",
            )

        self.assertTrue(self.delete_video.exists())
        self.assertTrue(alias.exists())

    def test_symlink_subtitle_is_excluded_and_never_eligible(self) -> None:
        self.delete_subtitle.unlink()
        outside = _write(self.root / "outside.srt", b"outside")
        try:
            os.symlink(str(outside), str(self.delete_subtitle))
        except (NotImplementedError, OSError):
            self.skipTest("symlinks are unavailable")

        plan = self.plan()
        self.assertNotIn(str(self.delete_subtitle), {value.path for value in plan.eligible})
        excluded = {value.path: value.reason for value in plan.excluded}
        self.assertEqual(excluded[str(self.delete_subtitle)], "symlink_or_reparse_not_safe")
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_unsupported_paired_subtitle_is_reported_but_not_deleted(self) -> None:
        paired = _write(self.folder / "Film.1080p.ko.sub", b"paired-subtitle")
        plan = self.plan()

        self.assertNotIn(str(paired), {value.path for value in plan.eligible})
        excluded = {value.path: value.reason for value in plan.excluded}
        self.assertEqual(
            excluded[str(paired)], "unsupported_or_paired_subtitle_format"
        )
        self.assertTrue(paired.exists())

    def test_same_stem_subtitle_shared_by_delete_and_keep_videos_is_protected(self) -> None:
        shared_folder = self.media / "Shared"
        target = _write(shared_folder / "Same.mkv", b"target")
        survivor = _write(shared_folder / "Same.mp4", b"survivor")
        shared_subtitle = _write(shared_folder / "Same.ko.srt", b"shared-subtitle")

        plan = build_direct_delete_plan(
            (str(target),),
            (str(survivor),),
            (str(self.media),),
            (str(self.media),),
            "web",
        )

        self.assertNotIn(str(shared_subtitle), {item.path for item in plan.eligible})
        excluded = {item.path: item.reason for item in plan.excluded}
        self.assertEqual(
            excluded[str(shared_subtitle)],
            "shared_with_surviving_or_sibling_video",
        )
        self.assertIn(str(shared_subtitle), {item.path for item in plan.protected})
        self.assertEqual(shared_subtitle.read_bytes(), b"shared-subtitle")

    def test_path_outside_plex_location_is_blocked_read_only(self) -> None:
        other_location = self.root / "other-library"
        other_location.mkdir()
        with self.assertRaisesRegex(DirectDeletePlanError, "Plex library Location"):
            build_direct_delete_plan(
                (str(self.delete_video),),
                (str(self.keep_video),),
                (str(self.media),),
                (str(other_location),),
                "web",
            )

        self.assertTrue(self.delete_video.exists())
        self.assertTrue(self.delete_subtitle.exists())
        self.assertTrue(self.keep_video.exists())
        self.assertTrue(self.keep_subtitle.exists())


class DirectDeleteJournalApiSafetyTest(unittest.TestCase):
    def test_public_cleanup_hides_snapshots_hashes_and_operation_paths(self) -> None:
        with FlaskFarmImportHarness() as harness:
            model = harness.setup_module.P.ModelDirectDeleteJournal
            journal = model()
            journal.operation_key = "public-operation-id"
            journal.status = "deleted_pending_scan"
            journal.plan_digest = "a" * 64
            journal.eligible_count = 1
            journal.excluded_count = 1
            journal.protected_count = 1
            journal.deleted_count = 2
            journal.last_error = ""
            journal.operation_paths_json = json.dumps(
                ["/media/.pdff-delete-private-tombstone"]
            )
            journal.unlink_json = json.dumps(
                [
                    {
                        "source_path": "/media/Film.1080p.ko.srt",
                        "operation_path": "/media/.pdff-delete-private-tombstone",
                        "kind": "subtitle",
                        "state": "deleted",
                        "inode": 456,
                        "sha256": "private-unlink-hash",
                    }
                ]
            )
            journal.manifest_json = json.dumps(
                {
                    "eligible": [
                        {
                            "path": "/media/Film.1080p.ko.srt",
                            "reason": "exclusive_to_deleted_video",
                            "snapshot": {
                                "inode": 123,
                                "sha256": "private-plan-hash",
                            },
                        }
                    ],
                    "excluded": [
                        {
                            "path": "/media/Film.shared.srt",
                            "reason": "shared_with_surviving_or_sibling_video",
                            "snapshot": {"inode": 789},
                        }
                    ],
                }
            )

            detail = journal.cleanup_api(include_paths=True)
            summary = journal.cleanup_api(include_paths=False)
            serialized = json.dumps(detail, ensure_ascii=False).lower()

            self.assertEqual(detail["backend"], "direct")
            self.assertEqual(detail["counts"]["deleted"], 2)
            self.assertTrue(detail["eligible"][0]["deleted"])
            self.assertEqual(summary["eligible"], [])
            self.assertEqual(summary["excluded"], [])
            for forbidden in (
                "operation_path",
                "tombstone",
                "snapshot",
                "inode",
                "sha256",
                "private-plan-hash",
                "private-unlink-hash",
            ):
                self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
