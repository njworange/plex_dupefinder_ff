from __future__ import annotations

import os
import shutil
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from services import MediaCandidate, MediaPart, SubtitleFinder, delete_sidecars, find_sidecars


@contextmanager
def temporary_directory():
    # tempfile applies an ACL that the Windows desktop sandbox cannot enter.
    # A uniquely named workspace directory avoids that platform-specific ACL.
    path = Path(__file__).resolve().parent / (".subtitle-test-" + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield str(path)
    finally:
        shutil.rmtree(str(path), ignore_errors=True)


class SubtitleFinderTests(unittest.TestCase):
    def test_exact_stem_whitelist_and_search_depth(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            video = root / "Movie.mkv"
            video.touch()
            (root / "Movie.srt").touch()
            (root / "Movie.ko.ass").touch()
            (root / "Movie2.srt").touch()
            (root / "Movie.txt").touch()
            (root / "Subs").mkdir()
            (root / "Subs" / "Movie.en.forced.vtt").touch()
            (root / "Subtitles").mkdir()
            (root / "Subtitles" / "Movie.idx").touch()
            (root / "Subtitles" / "Movie.sup").touch()
            (root / "Subs" / "nested").mkdir()
            (root / "Subs" / "nested" / "Movie.smi").touch()

            found = {Path(item).relative_to(root).as_posix() for item in find_sidecars(video)}

            self.assertEqual(
                found,
                {
                    "Movie.srt",
                    "Movie.ko.ass",
                    "Subs/Movie.en.forced.vtt",
                    "Subtitles/Movie.idx",
                    "Subtitles/Movie.sup",
                },
            )

    def test_multipart_candidate_finds_each_exact_stem_once(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            first = root / "Movie.CD1.mkv"
            second = root / "Movie.CD2.mkv"
            first.touch()
            second.touch()
            (root / "Movie.CD1.srt").touch()
            (root / "Movie.CD2.ko.smi").touch()
            candidate = MediaCandidate(
                "8", (MediaPart("1", str(first)), MediaPart("2", str(second)))
            )

            found = SubtitleFinder().snapshot_for_candidate(candidate)

            self.assertEqual(
                {Path(item).name for item in found},
                {"Movie.CD1.srt", "Movie.CD2.ko.smi"},
            )

    def test_dry_run_never_calls_unlink(self):
        with temporary_directory() as temporary:
            subtitle = Path(temporary) / "Movie.srt"
            subtitle.touch()
            calls = []

            result = delete_sidecars(
                (subtitle,), dry_run=True, unlink=lambda path: calls.append(path)
            )

            self.assertTrue(result.dry_run)
            self.assertEqual(calls, [])
            self.assertTrue(subtitle.exists())
            self.assertEqual(result.planned, (str(subtitle.absolute()),))

    def test_delete_returns_exact_deleted_missing_and_failed_paths(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            deleted = root / "Movie.srt"
            missing = root / "Movie.ko.srt"
            failed = root / "Movie.en.srt"
            deleted.touch()
            failed.touch()

            def unlink(path):
                if os.path.abspath(path) == str(failed.absolute()):
                    raise PermissionError("blocked")
                os.unlink(path)

            result = delete_sidecars(
                (deleted, missing, failed), dry_run=False, unlink=unlink
            )

            self.assertEqual(result.deleted, (str(deleted.absolute()),))
            self.assertEqual(result.missing, (str(missing.absolute()),))
            self.assertEqual(result.failed[0][0], str(failed.absolute()))
            self.assertFalse(deleted.exists())
            self.assertTrue(failed.exists())


if __name__ == "__main__":
    unittest.main()
