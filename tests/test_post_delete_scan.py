from __future__ import annotations

import json
import types
import unittest

from services.domain import MediaPart, MediaVersion, MetadataItem
from services.post_delete_scan_targets import build_scan_targets


def _candidate(*paths: str):
    return types.SimpleNamespace(
        media_id="10",
        parts_json=json.dumps(
            [{"id": str(index), "file": path} for index, path in enumerate(paths, 1)]
        )
    )


def _item(media_type: str, *paths: str) -> MetadataItem:
    return MetadataItem(
        rating_key="100",
        guid="plex://%s/post-delete-test" % media_type,
        media_type=media_type,
        title="Episode" if media_type == "episode" else "Movie",
        grandparent_title="Example Show" if media_type == "episode" else "",
        grandparent_rating_key="50" if media_type == "episode" else "",
        parent_index=1 if media_type == "episode" else None,
        index=2 if media_type == "episode" else None,
        media=(
            MediaVersion(
                media_id="10",
                duration=1,
                parts=tuple(
                    MediaPart(str(index), path)
                    for index, path in enumerate(paths or ("/unused/current-item.mkv",), 1)
                ),
            ),
        ),
    )


class PostDeleteScanTargetTests(unittest.TestCase):
    def test_movie_uses_deleted_part_parent_folder(self) -> None:
        group = types.SimpleNamespace(media_type="movie")

        targets = build_scan_targets(
            group,
            _candidate("/library/movies/Example Movie/movie.mkv"),
            _item("movie", "/library/movies/Example Movie/movie.mkv"),
            ["/library/movies"],
        )

        self.assertEqual(targets, ["/library/movies/Example Movie"])

    def test_episode_uses_show_root_below_matching_section_location(self) -> None:
        group = types.SimpleNamespace(media_type="episode")

        targets = build_scan_targets(
            group,
            _candidate("/library/tv/Example Show/Season 01/episode.mkv"),
            _item("episode", "/library/tv/Example Show/Season 01/episode.mkv"),
            ["/library/tv"],
        )

        self.assertEqual(targets, ["/library/tv/Example Show"])

    def test_movie_can_bind_scan_target_to_selected_surviving_media(self) -> None:
        current = MetadataItem(
            rating_key="100",
            guid="plex://movie/post-delete-keep-target",
            media_type="movie",
            title="Example",
            media=(
                MediaVersion(
                    media_id="10",
                    duration=1,
                    parts=(
                        MediaPart(
                            "101",
                            "/library/movies/Deleted Folder/deleted.mkv",
                        ),
                    ),
                ),
                MediaVersion(
                    media_id="20",
                    duration=1,
                    parts=(
                        MediaPart(
                            "201",
                            "/library/movies/Retained Folder/retained.mkv",
                        ),
                    ),
                ),
            ),
        )

        targets = build_scan_targets(
            types.SimpleNamespace(media_type="movie"),
            types.SimpleNamespace(media_id="20"),
            current,
            ["/library/movies"],
        )

        self.assertEqual(targets, ["/library/movies/Retained Folder"])

    def test_episode_can_bind_scan_target_to_selected_surviving_show_root(self) -> None:
        current = MetadataItem(
            rating_key="100",
            guid="plex://episode/post-delete-keep-target",
            media_type="episode",
            title="Episode",
            grandparent_title="Example Show",
            grandparent_rating_key="50",
            parent_index=1,
            index=2,
            media=(
                MediaVersion(
                    media_id="10",
                    duration=1,
                    parts=(
                        MediaPart(
                            "101",
                            "/library/tv-old/Example Show/Season 01/deleted.mkv",
                        ),
                    ),
                ),
                MediaVersion(
                    media_id="20",
                    duration=1,
                    parts=(
                        MediaPart(
                            "201",
                            "/library/tv-new/Example Show/Season 01/retained.mkv",
                        ),
                    ),
                ),
            ),
        )

        targets = build_scan_targets(
            types.SimpleNamespace(media_type="episode"),
            types.SimpleNamespace(media_id="20"),
            current,
            ["/library/tv-old", "/library/tv-new"],
        )

        self.assertEqual(targets, ["/library/tv-new/Example Show"])

    def test_surviving_movie_directly_under_section_root_is_not_widened(self) -> None:
        current = MetadataItem(
            rating_key="100",
            guid="plex://movie/post-delete-no-wide-target",
            media_type="movie",
            title="Example",
            media=(
                MediaVersion(
                    media_id="10",
                    duration=1,
                    parts=(
                        MediaPart(
                            "101",
                            "/library/movies/Deleted Folder/deleted.mkv",
                        ),
                    ),
                ),
                MediaVersion(
                    media_id="20",
                    duration=1,
                    parts=(
                        MediaPart("201", "/library/movies/retained.mkv"),
                    ),
                ),
            ),
        )

        targets = build_scan_targets(
            types.SimpleNamespace(media_type="movie"),
            types.SimpleNamespace(media_id="20"),
            current,
            ["/library/movies"],
        )

        self.assertEqual(targets, [])

    def test_multiple_parts_are_deduplicated_without_widening_movie_target(self) -> None:
        group = types.SimpleNamespace(media_type="movie")

        targets = build_scan_targets(
            group,
            _candidate(
                "/library/movies/Example Movie/disc1.mkv",
                "/library/movies/Example Movie/disc2.mkv",
            ),
            _item(
                "movie",
                "/library/movies/Example Movie/disc1.mkv",
                "/library/movies/Example Movie/disc2.mkv",
            ),
            ["/library/movies"],
        )

        self.assertEqual(targets, ["/library/movies/Example Movie"])

    def test_empty_relative_or_outside_paths_are_blocked(self) -> None:
        group = types.SimpleNamespace(media_type="movie")
        invalid_paths = ("", "relative/movie.mkv", "/other-root/movie.mkv")

        for path in invalid_paths:
            with self.subTest(path=path):
                self.assertEqual(
                    build_scan_targets(
                        group,
                        _candidate(path),
                        _item("movie", path),
                        ["/library/movies"],
                    ),
                    [],
                )

    def test_missing_section_locations_blocks_even_an_absolute_path(self) -> None:
        self.assertEqual(
            build_scan_targets(
                types.SimpleNamespace(media_type="episode"),
                _candidate("/library/tv/Example Show/Season 01/episode.mkv"),
                _item("episode", "/library/tv/Example Show/Season 01/episode.mkv"),
                [],
            ),
            [],
        )

    def test_windows_drive_paths_are_case_insensitive_and_keep_target_scope(self) -> None:
        movie = build_scan_targets(
            types.SimpleNamespace(media_type="movie"),
            _candidate(r"D:\Movies\Example Movie\movie.mkv"),
            _item("movie", r"D:\Movies\Example Movie\movie.mkv"),
            [r"d:\movies"],
        )
        episode = build_scan_targets(
            types.SimpleNamespace(media_type="episode"),
            _candidate(r"D:\TV\Example Show\Season 01\episode.mkv"),
            _item("episode", r"D:\TV\Example Show\Season 01\episode.mkv"),
            [r"d:\tv"],
        )

        self.assertEqual(movie, ["d:/movies/example movie"])
        self.assertEqual(episode, ["d:/tv/example show"])

    def test_unc_paths_use_movie_parent_and_show_root(self) -> None:
        movie_path = r"\\NAS\Media\Movies\Example Movie\movie.mkv"
        episode_path = r"\\NAS\Media\TV\Example Show\Season 01\episode.mkv"

        movie = build_scan_targets(
            types.SimpleNamespace(media_type="movie"),
            _candidate(movie_path),
            _item("movie", movie_path),
            [r"\\nas\media\movies"],
        )
        episode = build_scan_targets(
            types.SimpleNamespace(media_type="episode"),
            _candidate(episode_path),
            _item("episode", episode_path),
            [r"\\nas\media\tv"],
        )

        self.assertEqual(movie, ["//nas/media/movies/example movie"])
        self.assertEqual(episode, ["//nas/media/tv/example show"])

    def test_linux_location_matching_remains_case_sensitive(self) -> None:
        self.assertEqual(
            build_scan_targets(
                types.SimpleNamespace(media_type="movie"),
                _candidate("/Media/Movies/Example/movie.mkv"),
                _item("movie", "/Media/Movies/Example/movie.mkv"),
                ["/media/movies"],
            ),
            [],
        )

    def test_longest_matching_section_location_drives_episode_show_root(self) -> None:
        path = "/library/tv/Example Show/Season 01/episode.mkv"
        self.assertEqual(
            build_scan_targets(
                types.SimpleNamespace(media_type="episode"),
                _candidate(path),
                _item("episode", path),
                ["/library", "/library/tv"],
            ),
            ["/library/tv/Example Show"],
        )

    def test_movie_directly_under_section_root_is_not_widened(self) -> None:
        path = "/library/movies/movie.mkv"
        self.assertEqual(
            build_scan_targets(
                types.SimpleNamespace(media_type="movie"),
                _candidate(path),
                _item("movie", path),
                ["/library/movies"],
            ),
            [],
        )

    def test_multipart_with_any_path_outside_section_rejects_all_targets(self) -> None:
        inside = "/library/movies/Example/disc1.mkv"
        outside = "/other/Example/disc2.mkv"
        self.assertEqual(
            build_scan_targets(
                types.SimpleNamespace(media_type="movie"),
                _candidate(inside, outside),
                _item("movie", inside, outside),
                ["/library/movies"],
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
