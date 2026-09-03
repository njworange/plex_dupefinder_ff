"""Pure domain objects shared by the dupefinder service layer.

The module intentionally has no FlaskFarm, requests, or filesystem imports.  It
is therefore safe to import from command-line tools and unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple


def _as_tuple(value: Any) -> tuple:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    return tuple(value)


@dataclass(frozen=True)
class PlexConnection:
    """Connection information obtained from Plex Mate or explicit settings."""

    base_url: str
    token: str = field(repr=False, compare=False)
    machine_id: str = ""

    def public_dict(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "base_url": self.base_url,
                "machine_id": self.machine_id,
                "token": "***" if self.token else "",
            }
        )

    def as_dict(self) -> Dict[str, str]:
        return dict(self.public_dict())


@dataclass(frozen=True)
class PlexIdentity:
    machine_id: str
    version: str = ""
    allow_media_deletion: Optional[bool] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "version": self.version,
            "allow_media_deletion": self.allow_media_deletion,
        }


@dataclass(frozen=True)
class LibrarySection:
    key: str
    title: str
    section_type: str
    locations: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", str(self.key))
        object.__setattr__(self, "locations", _as_tuple(self.locations))

    @property
    def plex_item_type(self) -> Optional[int]:
        kind = self.section_type.casefold()
        if kind == "movie":
            return 1
        if kind in {"show", "tv", "episode"}:
            return 4
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "section_type": self.section_type,
            "locations": list(self.locations),
        }


@dataclass(frozen=True)
class AudioTrack:
    codec: str = ""
    channels: float = 0.0
    language: str = ""
    title: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "codec": self.codec,
            "channels": self.channels,
            "language": self.language,
            "title": self.title,
        }


@dataclass(frozen=True)
class MediaPart:
    """One Plex ``Part``.  One media candidate may contain several parts."""

    part_id: str
    path: str
    size: int = 0
    duration: int = 0
    container: str = ""
    exists: Optional[bool] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "part_id", str(self.part_id))
        object.__setattr__(self, "size", max(0, int(self.size or 0)))
        object.__setattr__(self, "duration", max(0, int(self.duration or 0)))

    @property
    def id(self) -> str:
        return self.part_id

    @property
    def file(self) -> str:
        return self.path

    def as_dict(self) -> Dict[str, Any]:
        return {
            "part_id": self.part_id,
            "path": self.path,
            "size": self.size,
            "duration": self.duration,
            "container": self.container,
            "exists": self.exists,
        }


@dataclass(frozen=True)
class MediaCandidate:
    """A Plex ``Media`` element competing inside one duplicate group."""

    media_id: str
    parts: Tuple[MediaPart, ...] = ()
    duration: int = 0
    bitrate: int = 0
    width: int = 0
    height: int = 0
    video_resolution: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    audio_channels: float = 0.0
    container: str = ""
    audio_tracks: Tuple[AudioTrack, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "media_id", str(self.media_id))
        object.__setattr__(self, "parts", _as_tuple(self.parts))
        object.__setattr__(self, "audio_tracks", _as_tuple(self.audio_tracks))
        for name in ("duration", "bitrate", "width", "height"):
            object.__setattr__(self, name, max(0, int(getattr(self, name) or 0)))
        object.__setattr__(self, "audio_channels", max(0.0, float(self.audio_channels or 0)))

    @property
    def id(self) -> str:
        return self.media_id

    @property
    def paths(self) -> Tuple[str, ...]:
        return tuple(part.path for part in self.parts if part.path)

    @property
    def total_size(self) -> int:
        return sum(part.size for part in self.parts)

    @property
    def multipart(self) -> bool:
        return len(self.parts) > 1

    @property
    def best_audio_channels(self) -> float:
        values = [self.audio_channels]
        values.extend(track.channels for track in self.audio_tracks)
        return max(values, default=0.0)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "media_id": self.media_id,
            "parts": [item.as_dict() for item in self.parts],
            "duration": self.duration,
            "bitrate": self.bitrate,
            "width": self.width,
            "height": self.height,
            "video_resolution": self.video_resolution,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "audio_channels": self.audio_channels,
            "container": self.container,
            "audio_tracks": [item.as_dict() for item in self.audio_tracks],
            "paths": list(self.paths),
            "total_size": self.total_size,
            "multipart": self.multipart,
        }


@dataclass(frozen=True)
class DuplicateGroup:
    """One Plex metadata item containing two or more ``Media`` candidates."""

    rating_key: str
    candidates: Tuple[MediaCandidate, ...]
    title: str = ""
    media_type: str = ""
    guid: str = ""
    year: Optional[int] = None
    parent_title: str = ""
    grandparent_title: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "rating_key", str(self.rating_key))
        object.__setattr__(self, "candidates", _as_tuple(self.candidates))
        if self.year is not None:
            object.__setattr__(self, "year", int(self.year))

    @property
    def media(self) -> Tuple[MediaCandidate, ...]:
        return self.candidates

    @property
    def is_duplicate(self) -> bool:
        return len(self.candidates) >= 2

    def candidate(self, media_id: object) -> Optional[MediaCandidate]:
        wanted = str(media_id)
        return next((item for item in self.candidates if item.media_id == wanted), None)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rating_key": self.rating_key,
            "title": self.title,
            "media_type": self.media_type,
            "guid": self.guid,
            "year": self.year,
            "parent_title": self.parent_title,
            "grandparent_title": self.grandparent_title,
            "candidates": [item.as_dict() for item in self.candidates],
            "is_duplicate": self.is_duplicate,
        }


@dataclass(frozen=True)
class ScoreResult:
    media_id: str
    total: float
    breakdown: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "media_id", str(self.media_id))
        object.__setattr__(self, "breakdown", MappingProxyType(dict(self.breakdown)))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "media_id": self.media_id,
            "total": self.total,
            "breakdown": dict(self.breakdown),
        }


# Small compatibility aliases make orchestration code read naturally without
# introducing a second representation of a Plex Media element.
MediaVersion = MediaCandidate
MetadataItem = DuplicateGroup


__all__ = [
    "AudioTrack",
    "DuplicateGroup",
    "LibrarySection",
    "MediaCandidate",
    "MediaPart",
    "MediaVersion",
    "MetadataItem",
    "PlexConnection",
    "PlexIdentity",
    "ScoreResult",
]
