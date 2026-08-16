from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PlexConnection:
    base_url: str
    machine_id: str
    token: str = field(repr=False)

    def public_dict(self) -> Dict[str, str]:
        return {
            "base_url": self.base_url,
            "machine_id": self.machine_id,
            "token": "***",
        }


@dataclass(frozen=True)
class PlexIdentity:
    machine_id: str
    version: str


@dataclass(frozen=True)
class LibrarySection:
    key: str
    title: str
    section_type: str

    def as_dict(self) -> Dict[str, str]:
        return {"key": self.key, "title": self.title, "type": self.section_type}


@dataclass(frozen=True)
class MediaPart:
    part_id: str
    file: str
    size: int = 0
    duration: int = 0
    container: str = ""
    exists: Optional[bool] = None

    def fingerprint_dict(self) -> Dict[str, Any]:
        return {
            "id": self.part_id,
            "file": self.file,
            "size": self.size,
            "duration": self.duration,
            "container": self.container,
            "exists": self.exists,
        }

    def as_dict(self) -> Dict[str, Any]:
        return self.fingerprint_dict()


@dataclass(frozen=True)
class AudioTrack:
    codec: str = ""
    channels: int = 0
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
class MediaVersion:
    media_id: str
    duration: int = 0
    bitrate: int = 0
    width: int = 0
    height: int = 0
    video_resolution: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    audio_channels: int = 0
    container: str = ""
    parts: Tuple[MediaPart, ...] = ()
    audio_tracks: Tuple[AudioTrack, ...] = ()

    @property
    def total_size(self) -> int:
        return sum(max(0, part.size) for part in self.parts)

    @property
    def paths(self) -> List[str]:
        return [part.file for part in self.parts if part.file]

    def fingerprint_dict(self) -> Dict[str, Any]:
        return {
            "media_id": self.media_id,
            "duration": self.duration,
            "bitrate": self.bitrate,
            "width": self.width,
            "height": self.height,
            "video_resolution": self.video_resolution,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "audio_channels": self.audio_channels,
            "container": self.container,
            "parts": [part.fingerprint_dict() for part in self.parts],
            "audio_tracks": [track.as_dict() for track in self.audio_tracks],
        }

    def fingerprint(self) -> str:
        payload = _stable_json(self.fingerprint_dict()).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def as_dict(self) -> Dict[str, Any]:
        value = self.fingerprint_dict()
        value["total_size"] = self.total_size
        value["fingerprint"] = self.fingerprint()
        return value


@dataclass(frozen=True)
class MetadataItem:
    rating_key: str
    guid: str
    media_type: str
    title: str
    year: Optional[int] = None
    grandparent_title: str = ""
    grandparent_rating_key: str = ""
    parent_index: Optional[int] = None
    index: Optional[int] = None
    media: Tuple[MediaVersion, ...] = ()

    def identity_dict(self) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "rating_key": self.rating_key,
            "guid": self.guid,
            "media_type": self.media_type,
        }
        if self.media_type == "episode":
            base.update(
                {
                    "grandparent_rating_key": self.grandparent_rating_key,
                    "grandparent_title": self.grandparent_title,
                    "parent_index": self.parent_index,
                    "index": self.index,
                }
            )
        else:
            base.update({"title": self.title, "year": self.year})
        return base

    def identity_fingerprint(self) -> str:
        payload = _stable_json(self.identity_dict()).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rating_key": self.rating_key,
            "guid": self.guid,
            "media_type": self.media_type,
            "title": self.title,
            "year": self.year,
            "grandparent_title": self.grandparent_title,
            "grandparent_rating_key": self.grandparent_rating_key,
            "parent_index": self.parent_index,
            "index": self.index,
            "identity": self.identity_dict(),
            "identity_fingerprint": self.identity_fingerprint(),
            "media": [version.as_dict() for version in self.media],
        }


@dataclass(frozen=True)
class ScoreResult:
    total: float
    breakdown: Dict[str, float]


@dataclass(frozen=True)
class SafetyResult:
    safe: bool
    flags: Tuple[str, ...]
    details: Dict[str, Any] = field(default_factory=dict)
