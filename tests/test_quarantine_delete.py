from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence
from unittest import mock

from services.domain import MediaPart, MediaVersion, MetadataItem


def _version(media_id: str, paths: Sequence[Path]) -> MediaVersion:
    return MediaVersion(
        media_id=str(media_id),
        duration=7_200_000,
        bitrate=1_000,
        width=1920,
        height=1080,
        video_resolution="1080",
        video_codec="h264",
        audio_codec="aac",
        audio_channels=2,
        container="mkv",
        parts=tuple(
            MediaPart(
                part_id="%s-%s" % (media_id, index),
                file=str(path),
                size=path.stat().st_size if path.exists() and path.is_file() else 0,
                duration=7_200_000,
                container=path.suffix.lstrip("."),
                exists=path.exists(),
            )
            for index, path in enumerate(paths, start=1)
        ),
    )


def _item(delete_paths: Sequence[Path], keep_paths: Sequence[Path]) -> MetadataItem:
    return MetadataItem(
        rating_key="100",
        guid="plex://movie/subtitle-quarantine-test",
        media_type="movie",
        title="Subtitle Quarantine Test",
        year=2026,
        media=(_version("10", delete_paths), _version("20", keep_paths)),
    )


def _entry_path(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("source_path") or entry.get("path") or entry.get("file") or "")
    return ""


def _entry_reason(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("reason_code") or entry.get("reason") or "")
    return ""


def _payload_paths(payload: Dict[str, Any], key: str) -> List[str]:
    return [_entry_path(entry) for entry in payload.get(key, [])]


def _write(path: Path, value: bytes = b"test") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


class QuarantinePlannerTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from services.quarantine_delete import QuarantinePlanner
        except ImportError as exc:  # pragma: no cover - makes a missing product module explicit
            self.fail("quarantine planner module is not available: %s" % exc)
        self.planner_type = QuarantinePlanner
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.quarantine = self.root / "quarantine"
        self.media.mkdir()
        self.quarantine.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def plan(
        self,
        item: MetadataItem,
        *,
        allowed_roots: Iterable[Path] = (),
        quarantine_root: Path = None,
        section_locations: Iterable[Path] = (),
    ):
        roots = tuple(str(path) for path in (allowed_roots or (self.media,)))
        locations = tuple(str(path) for path in (section_locations or (self.media,)))
        target = quarantine_root or self.quarantine
        planner = self.planner_type()
        return planner.plan(
            item,
            "10",
            roots,
            str(target),
            section_locations=locations,
        )

    def require_symlink(self, link: Path, target: Path, target_is_directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except (OSError, NotImplementedError) as exc:
            raise unittest.SkipTest("symlink creation is unavailable: %s" % exc)

    def test_common_language_and_property_suffixes_are_included_exclusively(self) -> None:
        folder = self.media / "Movie"
        delete_video = _write(folder / "Movie.1080p.mkv", b"delete-video")
        keep_video = _write(folder / "Movie.2160p.mkv", b"keep-video")
        expected = {
            str(_write(folder / "Movie.1080p.srt")),
            str(_write(folder / "Movie.1080p.ko.smi")),
            str(_write(folder / "Movie.1080p.en.forced.srt")),
            str(_write(folder / "Movie.1080p.sdh.ass")),
            str(_write(folder / "Movie.1080p.ko.cc.vtt")),
            str(_write(folder / "Movie.1080p.ja.ssa")),
        }
        keep_subtitle = _write(folder / "Movie.2160p.ko.srt")

        payload = self.plan(_item((delete_video,), (keep_video,))).public_dict()

        included = set(_payload_paths(payload, "included_subtitles"))
        self.assertEqual(included, expected)
        self.assertNotIn(str(keep_subtitle), included)
        self.assertTrue(payload["plan_digest"])

    def test_same_stem_different_video_extension_is_never_claimed(self) -> None:
        folder = self.media / "SameStem"
        delete_video = _write(folder / "Film.mkv", b"delete-video")
        keep_video = _write(folder / "Film.mp4", b"keep-video")
        shared_subtitle = _write(folder / "Film.ko.srt")

        payload = self.plan(_item((delete_video,), (keep_video,))).public_dict()

        self.assertNotIn(
            str(shared_subtitle),
            _payload_paths(payload, "included_subtitles"),
        )
        excluded = {
            _entry_path(entry): _entry_reason(entry)
            for entry in payload.get("excluded_subtitles", [])
        }
        self.assertIn(str(shared_subtitle), excluded)
        self.assertIn(
            excluded[str(shared_subtitle)],
            ("ambiguous_owner", "survivor_owned"),
        )

    def test_delete_and_survivor_same_part_path_is_rejected(self) -> None:
        folder = self.media / "SharedPart"
        shared_video = _write(folder / "Film.mkv", b"shared-video")
        _write(folder / "Film.ko.srt", b"shared-subtitle")

        with self.assertRaises(Exception):
            self.plan(_item((shared_video,), (shared_video,)))

        self.assertTrue(shared_video.exists())

    def test_sibling_video_symlink_cannot_make_same_stem_subtitle_exclusive(self) -> None:
        folder = self.media / "SiblingSymlink"
        delete_video = _write(folder / "Film.mkv", b"delete-video")
        keep_video = _write(folder / "Keep.mkv", b"keep-video")
        external_target = _write(self.media / "targets" / "Other.mp4", b"target")
        sibling_link = folder / "Film.mp4"
        self.require_symlink(sibling_link, external_target)
        shared_subtitle = _write(folder / "Film.ko.srt", b"shared-subtitle")

        try:
            plan = self.plan(_item((delete_video,), (keep_video,)))
        except Exception:
            # Failing the whole plan is a valid fail-closed response to an
            # unsafe sibling video entry.
            return

        payload = plan.public_dict()
        self.assertNotIn(
            str(shared_subtitle),
            _payload_paths(payload, "included_subtitles"),
        )

    def test_longer_survivor_stem_wins_over_delete_prefix(self) -> None:
        folder = self.media / "Prefix"
        delete_video = _write(folder / "Film.mkv", b"delete-video")
        keep_video = _write(folder / "Film.extended.mkv", b"keep-video")
        delete_subtitle = _write(folder / "Film.ko.srt")
        keep_subtitle = _write(folder / "Film.extended.ko.srt")

        payload = self.plan(_item((delete_video,), (keep_video,))).public_dict()
        included = _payload_paths(payload, "included_subtitles")

        self.assertIn(str(delete_subtitle), included)
        self.assertNotIn(str(keep_subtitle), included)
        excluded = {
            _entry_path(entry): _entry_reason(entry)
            for entry in payload.get("excluded_subtitles", [])
        }
        self.assertEqual(excluded.get(str(keep_subtitle)), "survivor_owned")

    def test_same_stem_in_separate_version_directories_is_exclusive(self) -> None:
        delete_folder = self.media / "1080p"
        keep_folder = self.media / "2160p"
        delete_video = _write(delete_folder / "Film.mkv", b"delete-video")
        keep_video = _write(keep_folder / "Film.mp4", b"keep-video")
        delete_subtitle = _write(delete_folder / "Film.ko.srt")
        keep_subtitle = _write(keep_folder / "Film.ko.srt")

        payload = self.plan(_item((delete_video,), (keep_video,))).public_dict()
        included = _payload_paths(payload, "included_subtitles")

        self.assertIn(str(delete_subtitle), included)
        self.assertNotIn(str(keep_subtitle), included)

    def test_subs_and_subtitles_directories_are_discovered_without_recursive_claim(self) -> None:
        folder = self.media / "SubtitleDirs"
        delete_video = _write(folder / "Film.1080p.mkv", b"delete-video")
        keep_video = _write(folder / "Film.2160p.mkv", b"keep-video")
        first = _write(folder / "Subs" / "Film.1080p.ko.srt")
        second = _write(folder / "Subtitles" / "Film.1080p.en.forced.ass")
        nested = _write(folder / "Subs" / "nested" / "Film.1080p.ja.srt")

        payload = self.plan(_item((delete_video,), (keep_video,))).public_dict()
        included = _payload_paths(payload, "included_subtitles")

        self.assertIn(str(first), included)
        self.assertIn(str(second), included)
        self.assertNotIn(str(nested), included)

    def test_subtitle_on_different_filesystem_is_excluded_not_moved(self) -> None:
        from services import quarantine_delete as module

        folder = self.media / "OtherDevice"
        delete_video = _write(folder / "Film.1080p.mkv", b"delete-video")
        keep_video = _write(folder / "Film.2160p.mkv", b"keep-video")
        subtitle = _write(folder / "Film.1080p.ko.srt", b"subtitle")
        original = module._decision_snapshot

        def other_device(path: str):
            snapshot, reason = original(path)
            if os.path.normcase(path) == os.path.normcase(str(subtitle)):
                self.assertIsNotNone(snapshot)
                snapshot = replace(snapshot, device=snapshot.device + 1)
            return snapshot, reason

        with mock.patch.object(module, "_decision_snapshot", side_effect=other_device):
            payload = self.plan(_item((delete_video,), (keep_video,))).public_dict()

        self.assertNotIn(str(subtitle), _payload_paths(payload, "included_subtitles"))
        excluded = {
            _entry_path(entry): _entry_reason(entry)
            for entry in payload.get("excluded_subtitles", [])
        }
        self.assertEqual(excluded.get(str(subtitle)), "different_filesystem")

    def test_uppercase_supported_extension_is_recognized(self) -> None:
        folder = self.media / "Case"
        delete_video = _write(folder / "Film.1080p.mkv", b"delete-video")
        keep_video = _write(folder / "Film.2160p.mkv", b"keep-video")
        subtitle = _write(folder / "Film.1080p.KO.FORCED.SRT")

        payload = self.plan(_item((delete_video,), (keep_video,))).public_dict()

        self.assertIn(str(subtitle), _payload_paths(payload, "included_subtitles"))

    def test_video_stem_case_follows_the_local_filesystem_rule(self) -> None:
        folder = self.media / "StemCase"
        delete_video = _write(folder / "Film.1080p.mkv", b"delete-video")
        keep_video = _write(folder / "Film.2160p.mkv", b"keep-video")
        subtitle = _write(folder / "film.1080p.ko.srt")

        payload = self.plan(_item((delete_video,), (keep_video,))).public_dict()
        included = _payload_paths(payload, "included_subtitles")

        if os.path.normcase("Film") == os.path.normcase("film"):
            self.assertIn(str(subtitle), included)
        else:
            self.assertNotIn(str(subtitle), included)

    def test_snapshot_identity_detects_content_and_stat_drift(self) -> None:
        from services.quarantine_delete import snapshot_matches

        folder = self.media / "Drift"
        delete_video = _write(folder / "Film.1080p.mkv", b"delete-video")
        keep_video = _write(folder / "Film.2160p.mkv", b"keep-video")
        subtitle = _write(folder / "Film.1080p.ko.srt", b"before")
        plan = self.plan(_item((delete_video,), (keep_video,)))
        snapshot = plan.eligible[0].snapshot
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot_matches(snapshot, verify_hash=True))

        subtitle.write_bytes(b"after-content-is-different")

        self.assertFalse(snapshot_matches(snapshot, verify_hash=True))

    def test_paired_and_unsupported_formats_are_never_included(self) -> None:
        folder = self.media / "Formats"
        delete_video = _write(folder / "Film.1080p.mkv", b"delete-video")
        keep_video = _write(folder / "Film.2160p.mkv", b"keep-video")
        paired_idx = _write(folder / "Film.1080p.ko.idx")
        paired_sub = _write(folder / "Film.1080p.ko.sub")
        unsupported = _write(folder / "Film.1080p.ko.sup")

        payload = self.plan(_item((delete_video,), (keep_video,))).public_dict()
        included = _payload_paths(payload, "included_subtitles")

        self.assertNotIn(str(paired_idx), included)
        self.assertNotIn(str(paired_sub), included)
        self.assertNotIn(str(unsupported), included)

    def test_symlink_and_hardlink_subtitles_are_excluded(self) -> None:
        folder = self.media / "Links"
        delete_video = _write(folder / "Film.1080p.mkv", b"delete-video")
        keep_video = _write(folder / "Film.2160p.mkv", b"keep-video")
        real = _write(folder / "real.srt")
        symlink = folder / "Film.1080p.ko.srt"
        try:
            symlink.symlink_to(real.name)
        except (OSError, NotImplementedError):
            symlink = None
        hardlink = folder / "Film.1080p.en.srt"
        try:
            os.link(str(real), str(hardlink))
        except (OSError, NotImplementedError):
            hardlink = None

        payload = self.plan(_item((delete_video,), (keep_video,))).public_dict()
        included = _payload_paths(payload, "included_subtitles")
        excluded = {
            _entry_path(entry): _entry_reason(entry)
            for entry in payload.get("excluded_subtitles", [])
        }

        if symlink is not None:
            self.assertNotIn(str(symlink), included)
            self.assertEqual(excluded.get(str(symlink)), "symlink")
        if hardlink is not None:
            self.assertNotIn(str(hardlink), included)
            self.assertEqual(excluded.get(str(hardlink)), "hardlink")

    def test_symlink_video_is_rejected_before_realpath_can_hide_the_link(self) -> None:
        folder = self.media / "SymlinkVideo"
        real_delete = _write(folder / "real-delete.mkv", b"delete-video")
        delete_link = folder / "Film.1080p.mkv"
        self.require_symlink(delete_link, real_delete.name)
        keep_video = _write(folder / "Film.2160p.mkv", b"keep-video")

        with self.assertRaises(Exception):
            self.plan(_item((delete_link,), (keep_video,)))

    def test_symlink_parent_directory_is_rejected(self) -> None:
        real_folder = self.media / "real-folder"
        delete_video = _write(real_folder / "Film.1080p.mkv", b"delete-video")
        linked_folder = self.media / "linked-folder"
        self.require_symlink(linked_folder, real_folder, target_is_directory=True)
        keep_video = _write(self.media / "keep" / "Film.2160p.mkv", b"keep-video")
        linked_video = linked_folder / delete_video.name

        with self.assertRaises(Exception):
            self.plan(_item((linked_video,), (keep_video,)))

    def test_symlink_quarantine_root_is_rejected(self) -> None:
        folder = self.media / "SymlinkQuarantine"
        delete_video = _write(folder / "Delete.mkv", b"delete-video")
        keep_video = _write(folder / "Keep.mkv", b"keep-video")
        actual_root = self.root / "actual-quarantine"
        actual_root.mkdir()
        linked_root = self.root / "linked-quarantine"
        self.require_symlink(linked_root, actual_root, target_is_directory=True)

        with self.assertRaises(Exception):
            self.plan(
                _item((delete_video,), (keep_video,)),
                quarantine_root=linked_root,
            )

    def test_multipart_delete_version_is_rejected(self) -> None:
        folder = self.media / "Multipart"
        first = _write(folder / "Film.cd1.mkv", b"part-one")
        second = _write(folder / "Film.cd2.mkv", b"part-two")
        keep_video = _write(folder / "Film.complete.mkv", b"keep-video")

        with self.assertRaises(Exception):
            self.plan(_item((first, second), (keep_video,)))

    def test_resolved_path_traversal_outside_allowed_root_is_rejected(self) -> None:
        allowed = self.media / "allowed"
        allowed.mkdir()
        outside = _write(self.root / "outside" / "Film.mkv", b"delete-video")
        keep_video = _write(allowed / "Keep.mkv", b"keep-video")
        traversal = allowed / "child" / ".." / ".." / "outside" / outside.name

        with self.assertRaises(Exception):
            self.plan(
                _item((traversal,), (keep_video,)),
                allowed_roots=(allowed,),
                section_locations=(self.media,),
            )

    def test_quarantine_root_inside_library_is_rejected(self) -> None:
        folder = self.media / "RootCollision"
        delete_video = _write(folder / "Delete.mkv", b"delete-video")
        keep_video = _write(folder / "Keep.mkv", b"keep-video")
        bad_quarantine = self.media / ".quarantine"
        bad_quarantine.mkdir()

        with self.assertRaises(Exception):
            self.plan(
                _item((delete_video,), (keep_video,)),
                quarantine_root=bad_quarantine,
            )

    def test_missing_quarantine_root_is_not_created_by_preview(self) -> None:
        folder = self.media / "NoRoot"
        delete_video = _write(folder / "Delete.mkv", b"delete-video")
        keep_video = _write(folder / "Keep.mkv", b"keep-video")
        missing = self.root / "does-not-exist"

        with self.assertRaises(Exception):
            self.plan(
                _item((delete_video,), (keep_video,)),
                quarantine_root=missing,
            )
        self.assertFalse(missing.exists())

    def test_public_payload_has_no_stat_identity_or_secret_material(self) -> None:
        folder = self.media / "Public"
        delete_video = _write(folder / "Delete.mkv", b"delete-video")
        keep_video = _write(folder / "Keep.mkv", b"keep-video")
        _write(folder / "Delete.ko.srt")

        payload = self.plan(_item((delete_video,), (keep_video,))).public_dict()
        serialized = repr(payload).lower()

        self.assertEqual(payload.get("backend"), "quarantine")
        self.assertTrue(payload.get("plan_digest"))
        for forbidden in (
            "lease_token",
            "owner_token",
            "plex_token",
            "authorization",
            "cookie",
            "st_ino",
            "st_dev",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
