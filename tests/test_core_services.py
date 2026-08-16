from __future__ import annotations

import unittest

from services.domain import AudioTrack, MediaPart, MediaVersion, MetadataItem
from services.plex_mate_provider import PlexMateProvider, PlexMateUnavailable, normalize_base_url
from services.safety import SafetyPolicy, assess_group, validate_fresh_snapshot
from services.score_engine import ScoreConfig, ScoreEngine


def movie_item(first_path: str = "/media/movies/a.mkv", second_path: str = "/media/movies/b.mkv"):
    return MetadataItem(
        rating_key="100",
        guid="plex://movie/example",
        media_type="movie",
        title="Example",
        year=2024,
        media=(
            MediaVersion(
                media_id="10",
                duration=7_200_000,
                bitrate=10_000,
                width=1920,
                height=1080,
                video_resolution="1080",
                video_codec="h264",
                audio_codec="aac",
                audio_channels=2,
                parts=(MediaPart("101", first_path, 1_000_000, 7_200_000, "mkv", True),),
                audio_tracks=(AudioTrack("aac", 2, "eng", "Main"),),
            ),
            MediaVersion(
                media_id="20",
                duration=7_200_000,
                bitrate=20_000,
                width=3840,
                height=2160,
                video_resolution="4k",
                video_codec="hevc",
                audio_codec="truehd",
                audio_channels=8,
                parts=(MediaPart("201", second_path, 5_000_000, 7_200_000, "mkv", True),),
                audio_tracks=(
                    AudioTrack("truehd", 8, "eng", "Main"),
                    AudioTrack("aac", 2, "kor", "Commentary"),
                ),
            ),
        ),
    )


class ProviderTests(unittest.TestCase):
    def test_provider_reads_plex_mate_lazily_and_repr_hides_token(self):
        class Settings:
            values = {
                "base_url": "http://plex.local:32400/",
                "base_token": "first-secret",
                "base_machine": "machine-1",
            }

            @classmethod
            def get(cls, key):
                return cls.values.get(key)

        class Plugin:
            ModelSetting = Settings

        class Manager:
            calls = 0

            @classmethod
            def get_plugin_instance(cls, name):
                cls.calls += 1
                self.assertEqual(name, "plex_mate")
                return Plugin()

        provider = PlexMateProvider(Manager)
        first = provider.resolve()
        Settings.values["base_token"] = "rotated-secret"
        second = provider.resolve()
        self.assertEqual(Manager.calls, 2)
        self.assertEqual(first.token, "first-secret")
        self.assertEqual(second.token, "rotated-secret")
        self.assertNotIn("first-secret", repr(first))
        self.assertEqual(first.base_url, "http://plex.local:32400")

    def test_provider_rejects_credentials_or_query_in_url(self):
        with self.assertRaises(PlexMateUnavailable):
            normalize_base_url("http://user:pass@plex.local:32400")
        with self.assertRaises(PlexMateUnavailable):
            normalize_base_url("http://plex.local:32400?X-Plex-Token=leak")

    def test_missing_plugin_has_non_secret_error(self):
        class Manager:
            @staticmethod
            def get_plugin_instance(name):
                return None

        with self.assertRaises(PlexMateUnavailable) as caught:
            PlexMateProvider(Manager).resolve()
        self.assertNotIn("base_token", str(caught.exception))


class ScoringTests(unittest.TestCase):
    def test_breakdown_is_auditable_and_better_version_wins(self):
        engine = ScoreEngine(ScoreConfig.defaults())
        item = movie_item()
        lower = engine.score(item.media[0])
        higher = engine.score(item.media[1])
        self.assertAlmostEqual(lower.total, sum(lower.breakdown.values()), places=2)
        self.assertGreater(higher.total, lower.total)
        self.assertEqual(engine.recommended_media_id(item.media), "20")

    def test_audio_uses_best_track_instead_of_summing_all_tracks(self):
        config = ScoreConfig(
            video_codec_scores={},
            audio_codec_scores={"truehd": 100, "aac": 50},
            resolution_scores={},
            filename_rules=(),
            bitrate_weight=0,
            duration_weight=0,
            dimension_weight=0,
            audio_channel_weight=0,
        )
        result = ScoreEngine(config).score(movie_item().media[1])
        self.assertEqual(result.breakdown["audio_codec"], 100)

    def test_filename_rule_is_applied_once_for_multipart(self):
        version = MediaVersion(
            media_id="1",
            parts=(
                MediaPart("1", "/media/My.Remux.CD1.mkv"),
                MediaPart("2", "/media/My.Remux.CD2.mkv"),
            ),
        )
        config = ScoreConfig(
            video_codec_scores={}, audio_codec_scores={}, resolution_scores={},
            filename_rules=(("*remux*", 50),), bitrate_weight=0, duration_weight=0,
            dimension_weight=0, audio_channel_weight=0,
        )
        self.assertEqual(ScoreEngine(config).score(version).breakdown["filename"], 50)

    def test_tie_has_no_recommendation(self):
        versions = (MediaVersion(media_id="1"), MediaVersion(media_id="2"))
        config = ScoreConfig(
            video_codec_scores={}, audio_codec_scores={}, resolution_scores={},
            filename_rules=(), bitrate_weight=0, duration_weight=0,
            dimension_weight=0, audio_channel_weight=0,
        )
        self.assertEqual(ScoreEngine(config).recommended_media_id(versions), "")


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.policy = SafetyPolicy(allowed_roots=("/media/movies",))

    def test_normal_group_is_safe(self):
        result = assess_group(movie_item(), self.policy)
        self.assertTrue(result.safe)
        self.assertEqual(result.flags, ())

    def test_shared_path_and_multipart_are_blocked(self):
        shared = assess_group(movie_item(second_path="/media/movies/a.mkv"), self.policy)
        self.assertIn("shared_file_path", shared.flags)

        item = movie_item()
        multipart = MediaVersion(
            **dict(
                item.media[0].__dict__,
                parts=item.media[0].parts + (MediaPart("102", "/media/movies/a-cd2.mkv"),),
            )
        )
        changed = MetadataItem(**dict(item.__dict__, media=(multipart, item.media[1])))
        self.assertIn("multipart_version", assess_group(changed, self.policy).flags)

    def test_outside_root_and_missing_tv_identity_are_blocked(self):
        outside = assess_group(movie_item(first_path="/other/a.mkv"), self.policy)
        self.assertIn("path_outside_allowed_roots", outside.flags)
        episode = MetadataItem(
            rating_key="2", guid="plex://episode/test", media_type="episode", title="Episode",
            grandparent_title="Show", media=movie_item().media,
        )
        self.assertIn("missing_episode_identity", assess_group(episode, self.policy).flags)

    def test_posix_roots_are_case_sensitive(self):
        result = assess_group(
            movie_item(first_path="/MEDIA/MOVIES/a.mkv", second_path="/media/movies/b.mkv"),
            self.policy,
        )
        self.assertIn("path_outside_allowed_roots", result.flags)

    def test_relative_media_path_or_allow_root_is_blocked(self):
        relative_path = assess_group(
            movie_item(first_path="movies/a.mkv"),
            SafetyPolicy(allowed_roots=("movies",)),
        )
        self.assertIn("invalid_allowed_root", relative_path.flags)
        self.assertIn("non_absolute_file_path", relative_path.flags)

        relative_path_with_absolute_root = assess_group(
            movie_item(first_path="media/movies/a.mkv"),
            self.policy,
        )
        self.assertIn("non_absolute_file_path", relative_path_with_absolute_root.flags)

    def test_snapshot_detects_media_or_part_change(self):
        item = movie_item()
        expected = {version.media_id: version.fingerprint() for version in item.media}
        self.assertTrue(validate_fresh_snapshot(item, item.identity_fingerprint(), expected).safe)
        changed_version = MediaVersion(**dict(item.media[0].__dict__, bitrate=10_001))
        changed = MetadataItem(**dict(item.__dict__, media=(changed_version, item.media[1])))
        result = validate_fresh_snapshot(changed, item.identity_fingerprint(), expected)
        self.assertIn("media_snapshot_changed", result.flags)


if __name__ == "__main__":
    unittest.main()
