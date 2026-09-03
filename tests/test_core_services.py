from __future__ import annotations

import unittest

from services import (
    AudioTrack,
    DuplicateGroup,
    MediaCandidate,
    MediaPart,
    PlexMateConfigurationError,
    PlexMateProvider,
    ScoreConfig,
    ScoreEngine,
)
from services.score_engine import (
    DEFAULT_AUDIO_CODEC_SCORES,
    DEFAULT_FILENAME_SCORES,
    DEFAULT_RESOLUTION_SCORES,
    DEFAULT_VIDEO_CODEC_SCORES,
)


def zero_config(**changes):
    values = {
        "audio_codec_scores": {},
        "video_codec_scores": {},
        "resolution_scores": {},
        "filename_scores": {},
        "bitrate_weight": 0.0,
        "duration_divisor": 1.0,
        "dimensions_weight": 0.0,
        "audio_channels_weight": 0.0,
        "include_size": False,
        "size_divisor": 1.0,
    }
    values.update(changes)
    return ScoreConfig(**values)


class DomainAndScoreTests(unittest.TestCase):
    def test_default_scores_match_upstream_config_sample(self):
        self.assertEqual(
            DEFAULT_AUDIO_CODEC_SCORES,
            {
                "Unknown": 0,
                "aac": 1000,
                "ac3": 1000,
                "dca": 2000,
                "dca-ma": 4000,
                "eac3": 1250,
                "flac": 2500,
                "mp2": 500,
                "mp3": 1000,
                "pcm": 2500,
                "truehd": 4500,
                "wmapro": 200,
            },
        )
        self.assertEqual(
            DEFAULT_VIDEO_CODEC_SCORES,
            {
                "Unknown": 0,
                "h264": 10000,
                "h265": 5000,
                "hevc": 5000,
                "mpeg1video": 250,
                "mpeg2video": 250,
                "mpeg4": 500,
                "msmpeg4": 100,
                "msmpeg4v2": 100,
                "msmpeg4v3": 100,
                "vc1": 3000,
                "vp9": 1000,
                "wmv2": 250,
                "wmv3": 250,
            },
        )
        self.assertEqual(
            DEFAULT_RESOLUTION_SCORES,
            {"1080": 10000, "480": 3000, "4k": 20000, "720": 5000, "Unknown": 0, "sd": 1000},
        )
        self.assertEqual(
            DEFAULT_FILENAME_SCORES,
            {
                "*.avi": -1000,
                "*.ts": -1000,
                "*.vob": -5000,
                "*1080p*BluRay*": 15000,
                "*720p*BluRay*": 10000,
                "*HDTV*": -1000,
                "*PROPER*": 1500,
                "*REPACK*": 1500,
                "*Remux*": 20000,
                "*WEB*CasStudio*": 5000,
                "*WEB*KINGS*": 5000,
                "*WEB*NTB*": 5000,
                "*WEB*QOQ*": 5000,
                "*WEB*SiGMA*": 5000,
                "*WEB*TBS*": -1000,
                "*WEB*TROLLHD*": 2500,
                "*WEB*VISUM*": 5000,
                "*dvd*": -1000,
            },
        )
        self.assertTrue(ScoreConfig().include_size)

    def test_media_candidate_supports_multipart_and_serialization(self):
        candidate = MediaCandidate(
            media_id="7",
            parts=(
                MediaPart("11", "/media/Movie.CD1.mkv", size=100),
                MediaPart("12", "/media/Movie.CD2.mkv", size=250),
            ),
        )
        group = DuplicateGroup("99", (candidate,), title="Movie")

        self.assertTrue(candidate.multipart)
        self.assertEqual(candidate.paths, ("/media/Movie.CD1.mkv", "/media/Movie.CD2.mkv"))
        self.assertEqual(candidate.total_size, 350)
        self.assertEqual(group.as_dict()["candidates"][0]["parts"][1]["part_id"], "12")

    def test_original_score_family_is_injectable_and_filename_glob_counts_once(self):
        config = zero_config(
            audio_codec_scores={"flac": 10},
            filename_scores={"*remux*": 7},
        )
        candidate = MediaCandidate(
            media_id="4",
            parts=(
                MediaPart("1", "/media/Movie.REMUX.CD1.mkv"),
                MediaPart("2", "/media/Movie.REMUX.CD2.mkv"),
            ),
            audio_codec="aac",
            audio_tracks=(AudioTrack(codec="flac", channels=2),),
        )

        result = ScoreEngine(config).score(candidate)

        self.assertEqual(result.breakdown["audio_codec"], 10)
        self.assertEqual(result.breakdown["filename"], 7)
        self.assertEqual(result.total, 17)

    def test_filename_score_does_not_match_parent_directory(self):
        config = zero_config(filename_scores={"*remux*": 7})
        engine = ScoreEngine(config)

        parent_only = MediaCandidate(
            media_id="1", parts=(MediaPart("1", "/media/REMUX/Movie.mkv"),)
        )
        filename_match = MediaCandidate(
            media_id="2", parts=(MediaPart("2", "/media/Movie.REMUX.mkv"),)
        )

        self.assertEqual(engine.score(parent_only).breakdown["filename"], 0)
        self.assertEqual(engine.score(filename_match).breakdown["filename"], 7)

    def test_best_audio_track_is_used_instead_of_summing_tracks(self):
        config = zero_config(audio_codec_scores={"aac": 2, "flac": 9})
        candidate = MediaCandidate(
            media_id="3",
            audio_codec="aac",
            audio_tracks=(AudioTrack(codec="aac"), AudioTrack(codec="flac")),
        )
        self.assertEqual(ScoreEngine(config).score(candidate).breakdown["audio_codec"], 9)

    def test_ties_use_numeric_media_id_ascending(self):
        group = DuplicateGroup(
            "100",
            (
                MediaCandidate("10"),
                MediaCandidate("abc"),
                MediaCandidate("2"),
            ),
        )

        decision = ScoreEngine(zero_config()).select_keep(group)

        self.assertEqual(decision.keep.media_id, "2")
        self.assertEqual([item.media_id for item in decision.delete_candidates], ["10", "abc"])
        self.assertEqual(decision.as_dict()["keep"]["media_id"], "2")


class PlexMateProviderTests(unittest.TestCase):
    def test_provider_is_injected_and_token_is_masked(self):
        class Settings:
            values = {
                "base_url": "http://plex.local:32400/",
                "base_token": "secret-token",
                "base_machine": "machine-a",
            }

            @classmethod
            def get(cls, key):
                return cls.values.get(key)

        class Plugin:
            ModelSetting = Settings

        class Manager:
            @staticmethod
            def get_plugin_instance(name):
                return Plugin() if name == "plex_mate" else None

        connection = PlexMateProvider(Manager()).resolve()

        self.assertEqual(connection.base_url, "http://plex.local:32400")
        self.assertEqual(connection.machine_id, "machine-a")
        self.assertEqual(connection.token, "secret-token")
        self.assertEqual(connection.as_dict()["token"], "***")

    def test_provider_rejects_token_in_url(self):
        class Plugin:
            settings = {
                "base_url": "http://user:pass@plex.local:32400",
                "base_token": "token",
            }

        class Manager:
            @staticmethod
            def get_plugin_instance(_name):
                return Plugin()

        with self.assertRaises(PlexMateConfigurationError):
            PlexMateProvider(Manager()).resolve()


if __name__ == "__main__":
    unittest.main()
