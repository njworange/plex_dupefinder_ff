"""A small Plex HTTP boundary with deterministic request semantics.

Authentication is sent only in headers, redirects are never followed, and a
DELETE is issued at most once.  The module can be imported without requests;
tests or alternative runtimes may inject a session implementing ``request``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Set, Tuple, Union
from urllib.parse import quote

from .domain import (
    AudioTrack,
    DuplicateGroup,
    LibrarySection,
    MediaCandidate,
    MediaPart,
    PlexConnection,
    PlexIdentity,
)

try:  # Optional at import time: FlaskFarm normally supplies requests.
    import requests as _requests
except ImportError:  # pragma: no cover - exercised in minimal deployments
    _requests = None


class PlexGatewayError(RuntimeError):
    pass


class PlexTransportError(PlexGatewayError):
    pass


class PlexDeleteUncertainError(PlexTransportError):
    """The server may have received a DELETE whose response was not observed."""


class PlexHttpError(PlexGatewayError):
    def __init__(self, status_code: int, url: str, body: str = "") -> None:
        self.status_code = int(status_code)
        self.url = url
        self.body = body
        super().__init__(f"Plex returned HTTP {self.status_code} for {url}")


class PlexProtocolError(PlexGatewayError):
    pass


class PlexIdentityMismatch(PlexGatewayError):
    pass


@dataclass(frozen=True)
class DeleteReceipt:
    rating_key: str
    media_id: str
    status_code: Optional[int]
    dry_run: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rating_key": self.rating_key,
            "media_id": self.media_id,
            "status_code": self.status_code,
            "dry_run": self.dry_run,
        }


def _list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _number(value: Any, cast: Callable[[Any], Any], default: Any = 0) -> Any:
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _xml_mapping(element: ET.Element) -> Dict[str, Any]:
    data: Dict[str, Any] = dict(element.attrib)
    for child in element:
        value = _xml_mapping(child)
        existing = data.get(child.tag)
        if existing is None:
            data[child.tag] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            data[child.tag] = [existing, value]
    if element.text and element.text.strip() and not data:
        data["text"] = element.text.strip()
    return data


def _container(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise PlexProtocolError("Plex response is not an object")
    value = payload.get("MediaContainer", payload)
    if not isinstance(value, Mapping):
        raise PlexProtocolError("Plex MediaContainer is not an object")
    return value


def _metadata_rows(container: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    rows: List[Any] = []
    for key in ("Metadata", "Video", "Track", "Photo"):
        rows.extend(_list(container.get(key)))
    return [item for item in rows if isinstance(item, Mapping)]


class PlexGateway:
    def __init__(
        self,
        connection: PlexConnection,
        *,
        timeout: Tuple[float, float] = (5.0, 30.0),
        session: Any = None,
        product: str = "FlaskFarm Plex Dupefinder",
        version: str = "2.1.0",
        client_identifier: str = "flaskfarm-plex-dupefinder-v2",
    ) -> None:
        if not isinstance(connection, PlexConnection):
            raise TypeError("connection must be a PlexConnection")
        if len(timeout) != 2 or timeout[0] <= 0 or timeout[1] <= 0:
            raise ValueError("timeout must contain positive connect/read values")
        self.connection = connection
        self.base_url = connection.base_url.rstrip("/")
        self.timeout = (float(timeout[0]), float(timeout[1]))
        if session is None:
            if _requests is None:
                raise RuntimeError("requests is unavailable; inject an HTTP session")
            session = _requests.Session()
        self.session = session
        self.headers = {
            "Accept": "application/json",
            "X-Plex-Token": connection.token,
            "X-Plex-Product": product,
            "X-Plex-Version": version,
            "X-Plex-Client-Identifier": client_identifier,
        }
        session_headers = getattr(self.session, "headers", None)
        if session_headers is not None and hasattr(session_headers, "update"):
            session_headers.update(self.headers)

    @staticmethod
    def _id(value: object, label: str) -> str:
        text = str(value)
        if not text.isdigit():
            raise ValueError(f"{label} must be a numeric Plex id")
        return text

    def _url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")

    def _request(
        self, method: str, path: str, *, params: Optional[Mapping[str, Any]] = None
    ) -> Any:
        url = self._url(path)
        try:
            response = self.session.request(
                method.upper(),
                url,
                params=dict(params or {}),
                headers=dict(self.headers),
                timeout=self.timeout,
                allow_redirects=False,
            )
        except Exception as exc:
            if method.upper() == "DELETE":
                raise PlexDeleteUncertainError(
                    "Plex DELETE response was not observed; request was not retried"
                ) from exc
            raise PlexTransportError(f"Plex request failed: {method.upper()} {url}") from exc

        status = int(getattr(response, "status_code", 0))
        if status < 200 or status >= 300:
            body = str(getattr(response, "text", ""))[:1000]
            raise PlexHttpError(status, url, body)
        return response

    @staticmethod
    def _payload(response: Any) -> Mapping[str, Any]:
        try:
            payload = response.json()
            if isinstance(payload, Mapping):
                return payload
        except (AttributeError, TypeError, ValueError):
            pass
        raw = getattr(response, "content", None)
        if raw is None:
            raw = getattr(response, "text", "")
        try:
            root = ET.fromstring(raw)
        except (ET.ParseError, TypeError, ValueError) as exc:
            raise PlexProtocolError("Plex response is neither JSON nor XML") from exc
        return {root.tag: _xml_mapping(root)}

    def identity(self) -> PlexIdentity:
        container = _container(self._payload(self._request("GET", "/identity")))
        return PlexIdentity(
            machine_id=str(container.get("machineIdentifier") or ""),
            version=str(container.get("version") or ""),
            allow_media_deletion=_optional_bool(container.get("allowMediaDeletion")),
        )

    def validate_identity(
        self, expected: object = None, *, require_match: bool = False
    ) -> PlexIdentity:
        actual = self.identity()
        if isinstance(expected, PlexConnection):
            expected_id = expected.machine_id
        elif expected is None:
            expected_id = self.connection.machine_id
        else:
            expected_id = str(expected)
        if require_match and expected_id and actual.machine_id != expected_id:
            raise PlexIdentityMismatch(
                f"expected Plex machine {expected_id!r}, got {actual.machine_id!r}"
            )
        return actual

    def list_sections(self) -> Tuple[LibrarySection, ...]:
        container = _container(
            self._payload(self._request("GET", "/library/sections"))
        )
        rows = [item for item in _list(container.get("Directory")) if isinstance(item, Mapping)]
        sections: List[LibrarySection] = []
        for row in rows:
            locations = tuple(
                str(item.get("path"))
                for item in _list(row.get("Location"))
                if isinstance(item, Mapping) and item.get("path")
            )
            sections.append(
                LibrarySection(
                    key=str(row.get("key") or ""),
                    title=str(row.get("title") or ""),
                    section_type=str(row.get("type") or ""),
                    locations=locations,
                )
            )
        return tuple(sections)

    def resolve_section(
        self, section: Union[LibrarySection, str, int]
    ) -> LibrarySection:
        """Resolve a library id to its Plex type before duplicate lookup.

        Plex requires ``type=1`` for movie items and ``type=4`` for episodes.
        Querying ``/all?duplicate=1`` without that type is ambiguous, so a raw
        section id is looked up rather than silently sent as an untyped query.
        """

        if isinstance(section, LibrarySection):
            resolved = section
        else:
            section_key = self._id(section, "section key")
            resolved = next(
                (item for item in self.list_sections() if item.key == section_key),
                None,
            )
            if resolved is None:
                raise PlexProtocolError(
                    f"Plex library section {section_key} was not found"
                )
        if resolved.plex_item_type is None:
            raise PlexProtocolError(
                f"unsupported Plex library type {resolved.section_type!r} "
                f"for section {resolved.key}"
            )
        return resolved

    def duplicate_rating_keys(
        self,
        section: Union[LibrarySection, str, int],
        cancel_check: Optional[Callable[[], bool]] = None,
        *,
        page_size: int = 200,
    ) -> Tuple[str, ...]:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        resolved_section = self.resolve_section(section)
        section_key = self._id(resolved_section.key, "section key")
        item_type = resolved_section.plex_item_type

        start = 0
        keys: List[str] = []
        seen: Set[str] = set()
        while True:
            if cancel_check is not None and cancel_check():
                break
            params: Dict[str, Any] = {
                "duplicate": 1,
                "includeGuids": 1,
                "includeMedia": 1,
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": page_size,
            }
            params["type"] = item_type
            response = self._request(
                "GET", f"/library/sections/{quote(section_key)}/all", params=params
            )
            container = _container(self._payload(response))
            rows = _metadata_rows(container)
            for row in rows:
                key = str(row.get("ratingKey") or row.get("rating_key") or "")
                if key and key not in seen:
                    seen.add(key)
                    keys.append(key)
            returned = _number(container.get("size"), int, len(rows))
            total = _number(container.get("totalSize"), int, start + returned)
            if returned <= 0 or start + returned >= total:
                break
            start += returned
        return tuple(keys)

    @staticmethod
    def _parse_group(row: Mapping[str, Any]) -> DuplicateGroup:
        candidates: List[MediaCandidate] = []
        media_rows = [item for item in _list(row.get("Media")) if isinstance(item, Mapping)]
        for media in media_rows:
            parts: List[MediaPart] = []
            tracks: List[AudioTrack] = []
            for part in [item for item in _list(media.get("Part")) if isinstance(item, Mapping)]:
                parts.append(
                    MediaPart(
                        part_id=str(part.get("id") or ""),
                        path=str(part.get("file") or ""),
                        size=_number(part.get("size"), int),
                        duration=_number(part.get("duration"), int),
                        container=str(part.get("container") or ""),
                        exists=_optional_bool(part.get("exists")),
                    )
                )
                for stream in [
                    item for item in _list(part.get("Stream")) if isinstance(item, Mapping)
                ]:
                    if _number(stream.get("streamType"), int) != 2:
                        continue
                    tracks.append(
                        AudioTrack(
                            codec=str(stream.get("codec") or ""),
                            channels=_number(stream.get("channels"), float, 0.0),
                            language=str(stream.get("language") or ""),
                            title=str(stream.get("title") or ""),
                        )
                    )
            candidates.append(
                MediaCandidate(
                    media_id=str(media.get("id") or ""),
                    parts=tuple(parts),
                    duration=_number(media.get("duration"), int),
                    bitrate=_number(media.get("bitrate"), int),
                    width=_number(media.get("width"), int),
                    height=_number(media.get("height"), int),
                    video_resolution=str(media.get("videoResolution") or ""),
                    video_codec=str(media.get("videoCodec") or ""),
                    audio_codec=str(media.get("audioCodec") or ""),
                    audio_channels=_number(media.get("audioChannels"), float, 0.0),
                    container=str(media.get("container") or ""),
                    audio_tracks=tuple(tracks),
                )
            )
        return DuplicateGroup(
            rating_key=str(row.get("ratingKey") or row.get("rating_key") or ""),
            candidates=tuple(candidates),
            title=str(row.get("title") or ""),
            media_type=str(row.get("type") or ""),
            guid=str(row.get("guid") or ""),
            year=_number(row.get("year"), int, None),
            parent_title=str(row.get("parentTitle") or ""),
            grandparent_title=str(row.get("grandparentTitle") or ""),
        )

    def get_metadata(self, rating_key: object) -> DuplicateGroup:
        key = self._id(rating_key, "rating key")
        response = self._request(
            "GET",
            f"/library/metadata/{quote(key)}",
            params={"includeGuids": 1, "includeMedia": 1},
        )
        rows = _metadata_rows(_container(self._payload(response)))
        if not rows:
            raise PlexProtocolError(f"metadata {key} was absent from the Plex response")
        return self._parse_group(rows[0])

    get_group = get_metadata
    reload_group = get_metadata

    def duplicate_groups(
        self,
        section: Union[LibrarySection, str, int],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[DuplicateGroup, ...]:
        return tuple(self.iter_duplicate_groups(section, cancel_check))

    def iter_duplicate_groups(
        self,
        section: Union[LibrarySection, str, int],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Iterator[DuplicateGroup]:
        # Fetching the rating-key list is paginated, but each full metadata
        # record is loaded and yielded independently so the worker can decide
        # and delete before the next group is read.
        for rating_key in self.duplicate_rating_keys(section, cancel_check):
            if cancel_check is not None and cancel_check():
                break
            group = self.get_metadata(rating_key)
            if group.is_duplicate:
                yield group

    def media_exists(self, rating_key: object, media_id: object) -> bool:
        wanted = str(media_id)
        try:
            group = self.get_metadata(rating_key)
        except PlexHttpError as exc:
            if exc.status_code == 404:
                return False
            raise
        return group.candidate(wanted) is not None

    def delete_media(
        self, rating_key: object, media_id: object, *, dry_run: bool = False
    ) -> DeleteReceipt:
        key = self._id(rating_key, "rating key")
        candidate = self._id(media_id, "media id")
        if dry_run:
            return DeleteReceipt(key, candidate, None, dry_run=True)
        # Deliberately one request and no retry: a timeout has unknown outcome.
        response = self._request(
            "DELETE", f"/library/metadata/{quote(key)}/media/{quote(candidate)}"
        )
        return DeleteReceipt(key, candidate, int(response.status_code), dry_run=False)


__all__ = [
    "DeleteReceipt",
    "PlexDeleteUncertainError",
    "PlexGateway",
    "PlexGatewayError",
    "PlexHttpError",
    "PlexIdentityMismatch",
    "PlexProtocolError",
    "PlexTransportError",
]
