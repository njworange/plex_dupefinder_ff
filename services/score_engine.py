"""Deterministic, configurable scoring for Plex duplicate candidates."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Pattern, Tuple

from .domain import DuplicateGroup, MediaCandidate, ScoreResult


DEFAULT_AUDIO_CODEC_SCORES = {
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
}
DEFAULT_VIDEO_CODEC_SCORES = {
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
}
DEFAULT_RESOLUTION_SCORES = {
    "1080": 10000,
    "480": 3000,
    "4k": 20000,
    "720": 5000,
    "Unknown": 0,
    "sd": 1000,
}
DEFAULT_FILENAME_SCORES = {
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
}


@dataclass(frozen=True)
class ScoreConfig:
    """Weights retain the original project's score-family, but are injectable."""

    audio_codec_scores: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_AUDIO_CODEC_SCORES)
    )
    video_codec_scores: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_VIDEO_CODEC_SCORES)
    )
    resolution_scores: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_RESOLUTION_SCORES)
    )
    filename_scores: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_FILENAME_SCORES)
    )
    bitrate_weight: float = 2.0
    duration_divisor: float = 300.0
    dimensions_weight: float = 2.0
    audio_channels_weight: float = 1000.0
    include_size: bool = True
    size_divisor: float = 100000.0


@dataclass(frozen=True)
class RankedCandidate:
    candidate: MediaCandidate
    score: ScoreResult


@dataclass(frozen=True)
class ScoreDecision:
    group: DuplicateGroup
    ranked: Tuple[RankedCandidate, ...]

    @property
    def keep(self) -> MediaCandidate:
        if not self.ranked:
            raise ValueError("cannot select a keep candidate from an empty group")
        return self.ranked[0].candidate

    @property
    def delete_candidates(self) -> Tuple[MediaCandidate, ...]:
        return tuple(item.candidate for item in self.ranked[1:])

    @property
    def duplicates(self) -> Tuple[MediaCandidate, ...]:
        return self.delete_candidates

    @property
    def scores(self) -> Tuple[ScoreResult, ...]:
        return tuple(item.score for item in self.ranked)

    def as_dict(self):
        return {
            "keep": self.keep.as_dict(),
            "delete_candidates": [item.as_dict() for item in self.delete_candidates],
            "scores": [item.as_dict() for item in self.scores],
        }


def stable_media_id_key(media_id: object) -> tuple:
    """Numeric Plex ids sort numerically; non-numeric ids remain deterministic."""

    text = str(media_id)
    try:
        return (0, int(text), text)
    except (TypeError, ValueError):
        return (1, text.casefold(), text)


def _normalise_codec(value: object) -> str:
    text = str(value or "").casefold().replace("-", "").replace("_", "")
    aliases = {
        "h265": "hevc",
        "x265": "hevc",
        "x264": "h264",
        "avc": "h264",
        "ddp": "eac3",
        "ddplus": "eac3",
        "dtshd": "dcama",
        "dtsma": "dcama",
    }
    return aliases.get(text, text)


class ScoreEngine:
    def __init__(self, config: Optional[ScoreConfig] = None) -> None:
        self.config = config or ScoreConfig()
        self._audio_scores = {
            _normalise_codec(key): float(value)
            for key, value in self.config.audio_codec_scores.items()
        }
        self._video_scores = {
            _normalise_codec(key): float(value)
            for key, value in self.config.video_codec_scores.items()
        }
        self._resolution_scores = {
            str(key).casefold(): float(value)
            for key, value in self.config.resolution_scores.items()
        }
        self._filename_rules: Tuple[Tuple[Pattern[str], float], ...] = tuple(
            (re.compile(fnmatch.translate(pattern), re.IGNORECASE), float(value))
            for pattern, value in self.config.filename_scores.items()
        )
        if self.config.duration_divisor <= 0:
            raise ValueError("duration_divisor must be positive")
        if self.config.size_divisor <= 0:
            raise ValueError("size_divisor must be positive")

    def score(self, candidate: MediaCandidate) -> ScoreResult:
        audio_codecs = [_normalise_codec(candidate.audio_codec)]
        audio_codecs.extend(_normalise_codec(track.codec) for track in candidate.audio_tracks)
        audio_codec = max((self._audio_scores.get(item, 0.0) for item in audio_codecs), default=0.0)

        resolution_text = str(candidate.video_resolution or "").casefold()
        resolution = self._resolution_scores.get(resolution_text, 0.0)
        if not resolution:
            numeric = re.search(r"\d+", resolution_text)
            if numeric:
                resolution = self._resolution_scores.get(numeric.group(0), 0.0)

        # Each configured filename rule contributes at most once, even when a
        # Plex Media is multipart.
        filename = sum(
            value
            for pattern, value in self._filename_rules
            if any(pattern.search(os.path.basename(path)) for path in candidate.paths)
        )

        breakdown = {
            "audio_codec": audio_codec,
            "video_codec": self._video_scores.get(_normalise_codec(candidate.video_codec), 0.0),
            "resolution": resolution,
            "filename": filename,
            "bitrate": candidate.bitrate * float(self.config.bitrate_weight),
            "duration": candidate.duration / float(self.config.duration_divisor),
            "dimensions": (candidate.width + candidate.height)
            * float(self.config.dimensions_weight),
            "audio_channels": candidate.best_audio_channels
            * float(self.config.audio_channels_weight),
            "size": (
                candidate.total_size / float(self.config.size_divisor)
                if self.config.include_size
                else 0.0
            ),
        }
        total = float(sum(breakdown.values()))
        return ScoreResult(candidate.media_id, total, breakdown)

    score_candidate = score

    def rank(self, candidates: Iterable[MediaCandidate]) -> Tuple[RankedCandidate, ...]:
        ranked = [RankedCandidate(item, self.score(item)) for item in candidates]
        ranked.sort(
            key=lambda item: (
                -item.score.total,
                stable_media_id_key(item.candidate.media_id),
            )
        )
        return tuple(ranked)

    def select_keep(self, group: DuplicateGroup) -> ScoreDecision:
        if not group.candidates:
            raise ValueError("duplicate group has no media candidates")
        return ScoreDecision(group=group, ranked=self.rank(group.candidates))

    score_group = select_keep


__all__ = [
    "DEFAULT_AUDIO_CODEC_SCORES",
    "DEFAULT_FILENAME_SCORES",
    "DEFAULT_RESOLUTION_SCORES",
    "DEFAULT_VIDEO_CODEC_SCORES",
    "RankedCandidate",
    "ScoreConfig",
    "ScoreDecision",
    "ScoreEngine",
    "stable_media_id_key",
]
