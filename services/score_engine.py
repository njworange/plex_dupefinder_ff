from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

from .domain import MediaVersion, ScoreResult


DEFAULT_VIDEO_CODEC_SCORES: Dict[str, float] = {
    "av1": 5000,
    "hevc": 4000,
    "h265": 4000,
    "vp9": 3000,
    "h264": 2000,
    "mpeg4": 1000,
    "mpeg2video": 500,
}

DEFAULT_AUDIO_CODEC_SCORES: Dict[str, float] = {
    "truehd": 5000,
    "dca": 4000,
    "dts": 4000,
    "eac3": 3000,
    "ac3": 2000,
    "aac": 1000,
    "mp3": 500,
}

DEFAULT_RESOLUTION_SCORES: Dict[str, float] = {
    "4k": 40000,
    "2160": 40000,
    "1080": 20000,
    "720": 10000,
    "576": 6000,
    "480": 5000,
    "sd": 1000,
}

DEFAULT_FILENAME_RULES: Tuple[Tuple[str, float], ...] = (
    ("*remux*", 10000),
    ("*bluray*", 4000),
    ("*web-dl*", 2500),
    ("*webrip*", 1500),
)


def serialize_score_map(values: Dict[str, float]) -> str:
    return "\n".join(
        "%s=%s" % (key, int(float(value)) if float(value).is_integer() else value)
        for key, value in values.items()
    )


def serialize_filename_rules(values: Sequence[Tuple[str, float]]) -> str:
    return "\n".join(
        "%s=%s" % (pattern, int(float(score)) if float(score).is_integer() else score)
        for pattern, score in values
    )


def parse_score_map(value: str, fallback: Dict[str, float]) -> Dict[str, float]:
    raw = (value or "").strip()
    if not raw:
        return dict(fallback)
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            return {str(key).strip().lower(): float(score) for key, score in decoded.items()}
    except (TypeError, ValueError):
        pass

    result: Dict[str, float] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, score = line.rsplit("=", 1)
        try:
            result[key.strip().lower()] = float(score.strip())
        except ValueError:
            continue
    return result or dict(fallback)


def parse_filename_rules(value: str) -> Tuple[Tuple[str, float], ...]:
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_FILENAME_RULES
    result: List[Tuple[str, float]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        pattern, score = line.rsplit("=", 1)
        try:
            result.append((pattern.strip().lower(), float(score.strip())))
        except ValueError:
            continue
    return tuple(result) if result else DEFAULT_FILENAME_RULES


@dataclass(frozen=True)
class ScoreConfig:
    video_codec_scores: Dict[str, float]
    audio_codec_scores: Dict[str, float]
    resolution_scores: Dict[str, float]
    filename_rules: Tuple[Tuple[str, float], ...]
    bitrate_weight: float = 2.0
    duration_weight: float = 1.0 / 300.0
    dimension_weight: float = 2.0
    audio_channel_weight: float = 1000.0
    use_filesize: bool = False
    filesize_weight: float = 1.0 / 100000.0

    @classmethod
    def defaults(cls) -> "ScoreConfig":
        return cls(
            video_codec_scores=dict(DEFAULT_VIDEO_CODEC_SCORES),
            audio_codec_scores=dict(DEFAULT_AUDIO_CODEC_SCORES),
            resolution_scores=dict(DEFAULT_RESOLUTION_SCORES),
            filename_rules=DEFAULT_FILENAME_RULES,
        )


def _normalized_codec(codec: str) -> str:
    value = (codec or "").strip().lower().replace("_", "").replace("-", "")
    aliases = {
        "x265": "hevc",
        "h265": "h265",
        "hev1": "hevc",
        "hvc1": "hevc",
        "x264": "h264",
        "avc": "h264",
        "avc1": "h264",
        "dts": "dts",
        "dtshd": "dca",
        "dca": "dca",
        "eac3": "eac3",
        "ac3": "ac3",
    }
    return aliases.get(value, value)


def _resolution_keys(version: MediaVersion) -> List[str]:
    value = (version.video_resolution or "").strip().lower().replace("p", "")
    values = [value] if value else []
    if version.height:
        values.append(str(version.height))
    if value in ("uhd", "2160") or version.height >= 2000:
        values.extend(["4k", "2160"])
    if not values:
        values.append("sd")
    return list(dict.fromkeys(values))


class ScoreEngine:
    def __init__(self, config: ScoreConfig) -> None:
        self.config = config

    def score(self, version: MediaVersion) -> ScoreResult:
        resolution = max(
            (self.config.resolution_scores.get(key, 0.0) for key in _resolution_keys(version)),
            default=0.0,
        )
        video_codec = self.config.video_codec_scores.get(_normalized_codec(version.video_codec), 0.0)

        audio_codecs = [_normalized_codec(version.audio_codec)]
        audio_codecs.extend(_normalized_codec(track.codec) for track in version.audio_tracks)
        # Use the best audio track once. Summing tracks rewards commentary/language count.
        audio_codec = max(
            (self.config.audio_codec_scores.get(codec, 0.0) for codec in audio_codecs),
            default=0.0,
        )
        channels = max(
            [version.audio_channels] + [track.channels for track in version.audio_tracks],
            default=0,
        )

        filename = 0.0
        lowered_paths = [path.lower() for path in version.paths]
        for pattern, rule_score in self.config.filename_rules:
            # Each rule is applied once even when a multipart version has several matching parts.
            if any(fnmatch.fnmatch(path, pattern) for path in lowered_paths):
                filename += rule_score

        breakdown = {
            "resolution": resolution,
            "video_codec": video_codec,
            "audio_codec": audio_codec,
            "bitrate": max(0, version.bitrate) * self.config.bitrate_weight,
            "duration": max(0, version.duration) * self.config.duration_weight,
            "dimensions": (max(0, version.width) + max(0, version.height)) * self.config.dimension_weight,
            "audio_channels": max(0, channels) * self.config.audio_channel_weight,
            "filename": filename,
            "filesize": (
                version.total_size * self.config.filesize_weight if self.config.use_filesize else 0.0
            ),
        }
        rounded = {key: round(value, 3) for key, value in breakdown.items()}
        return ScoreResult(total=round(sum(breakdown.values()), 3), breakdown=rounded)

    def recommended_media_id(self, versions: Iterable[MediaVersion]) -> str:
        scored = [(version.media_id, self.score(version).total) for version in versions]
        if not scored:
            return ""
        highest = max(score for _, score in scored)
        winners = [media_id for media_id, score in scored if abs(score - highest) < 0.0001]
        return winners[0] if len(winners) == 1 else ""
