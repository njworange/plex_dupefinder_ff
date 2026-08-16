from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from .safety import is_absolute_remote_path, normalize_remote_path
from .domain import (
    AudioTrack,
    LibrarySection,
    MediaPart,
    MediaVersion,
    MetadataItem,
    PlexConnection,
    PlexIdentity,
)


class PlexGatewayError(RuntimeError):
    pass


class PlexHTTPError(PlexGatewayError):
    def __init__(self, message: str, status_code: int) -> None:
        super(PlexHTTPError, self).__init__(message)
        self.status_code = int(status_code)


class PlexAuthenticationError(PlexGatewayError):
    pass


class PlexDeleteOutcomeUnknown(PlexGatewayError):
    """A timeout occurred after sending DELETE. The caller must re-read state."""


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> Optional[bool]:
    if value in (None, ""):
        return None
    return str(value).strip().lower() in ("1", "true", "yes")


def _xml_node(node: ET.Element) -> Dict[str, Any]:
    result: Dict[str, Any] = dict(node.attrib)
    for child in node:
        result.setdefault(child.tag, []).append(_xml_node(child))
    return result


def _decode_container(response: requests.Response) -> Dict[str, Any]:
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "json" in content_type:
        payload = response.json()
        return payload.get("MediaContainer", payload)

    text = response.text or ""
    try:
        payload = json.loads(text)
        return payload.get("MediaContainer", payload)
    except (TypeError, ValueError):
        pass

    try:
        return _xml_node(ET.fromstring(text))
    except ET.ParseError as exc:
        raise PlexGatewayError("Plex 응답을 JSON 또는 XML로 해석할 수 없습니다.") from exc


def _first_guid(item: Dict[str, Any]) -> str:
    direct = str(item.get("guid") or "").strip()
    if direct:
        return direct
    for guid in _as_list(item.get("Guid")):
        if isinstance(guid, dict) and guid.get("id"):
            return str(guid["id"])
    return ""


def parse_metadata(item: Dict[str, Any]) -> MetadataItem:
    media_versions: List[MediaVersion] = []
    for media in _as_list(item.get("Media")):
        if not isinstance(media, dict):
            continue
        parts: List[MediaPart] = []
        audio_tracks: List[AudioTrack] = []
        for part in _as_list(media.get("Part")):
            if not isinstance(part, dict):
                continue
            parts.append(
                MediaPart(
                    part_id=str(part.get("id") or ""),
                    file=str(part.get("file") or ""),
                    size=_as_int(part.get("size")),
                    duration=_as_int(part.get("duration")),
                    container=str(part.get("container") or ""),
                    exists=_as_bool(part.get("exists")),
                )
            )
            for stream in _as_list(part.get("Stream")):
                if not isinstance(stream, dict) or _as_int(stream.get("streamType")) != 2:
                    continue
                audio_tracks.append(
                    AudioTrack(
                        codec=str(stream.get("codec") or ""),
                        channels=_as_int(stream.get("channels")),
                        language=str(stream.get("language") or ""),
                        title=str(stream.get("title") or stream.get("displayTitle") or ""),
                    )
                )

        media_versions.append(
            MediaVersion(
                media_id=str(media.get("id") or ""),
                duration=_as_int(media.get("duration")),
                bitrate=_as_int(media.get("bitrate")),
                width=_as_int(media.get("width")),
                height=_as_int(media.get("height")),
                video_resolution=str(media.get("videoResolution") or ""),
                video_codec=str(media.get("videoCodec") or ""),
                audio_codec=str(media.get("audioCodec") or ""),
                audio_channels=_as_int(media.get("audioChannels")),
                container=str(media.get("container") or ""),
                parts=tuple(parts),
                audio_tracks=tuple(audio_tracks),
            )
        )

    return MetadataItem(
        rating_key=str(item.get("ratingKey") or item.get("rating_key") or ""),
        guid=_first_guid(item),
        media_type=str(item.get("type") or ""),
        title=str(item.get("title") or ""),
        year=_as_optional_int(item.get("year")),
        grandparent_title=str(item.get("grandparentTitle") or ""),
        grandparent_rating_key=str(item.get("grandparentRatingKey") or ""),
        parent_index=_as_optional_int(item.get("parentIndex")),
        index=_as_optional_int(item.get("index")),
        media=tuple(media_versions),
    )


class PlexGateway:
    PRODUCT = "Plex DupeFinder FF"
    VERSION = "1.4.1"

    def __init__(
        self,
        connection: PlexConnection,
        timeout: Tuple[int, int] = (5, 20),
        session: Optional[requests.Session] = None,
    ) -> None:
        self.connection = connection
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "X-Plex-Token": connection.token,
                "X-Plex-Product": self.PRODUCT,
                "X-Plex-Version": self.VERSION,
                "X-Plex-Client-Identifier": "plex-dupefinder-ff",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[Tuple[int, int]] = None,
    ) -> requests.Response:
        url = "%s/%s" % (self.connection.base_url.rstrip("/"), path.lstrip("/"))
        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params or {},
                timeout=timeout or self.timeout,
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            if method.upper() == "DELETE":
                raise PlexDeleteOutcomeUnknown(
                    "삭제 요청이 시간 초과되었습니다. 재시도하지 말고 Plex 상태를 다시 확인해야 합니다."
                ) from exc
            raise PlexGatewayError("Plex 요청 시간이 초과되었습니다.") from exc
        except requests.RequestException as exc:
            if method.upper() == "DELETE":
                raise PlexDeleteOutcomeUnknown(
                    "삭제 요청의 연결 결과를 확정할 수 없습니다. 재시도하지 말고 Plex 상태를 확인해야 합니다."
                ) from exc
            raise PlexGatewayError("Plex 서버에 연결할 수 없습니다: %s" % exc.__class__.__name__) from exc

        if not 200 <= response.status_code < 300:
            if method.upper() == "DELETE":
                raise PlexDeleteOutcomeUnknown(
                    "Plex가 삭제 요청에 HTTP %s를 반환했습니다. 상태 재확인이 필요합니다."
                    % response.status_code
                )
            if response.status_code in (401, 403):
                raise PlexAuthenticationError("Plex 토큰이 거부되었습니다.")
            raise PlexHTTPError(
                "Plex 요청이 실패했습니다. HTTP %s" % response.status_code,
                response.status_code,
            )
        return response

    def identity(self) -> PlexIdentity:
        container = _decode_container(self._request("GET", "/identity"))
        return PlexIdentity(
            machine_id=str(container.get("machineIdentifier") or ""),
            version=str(container.get("version") or ""),
        )

    def validate_identity(self, expected_machine_id: str, require_match: bool = True) -> PlexIdentity:
        identity = self.identity()
        if require_match and not expected_machine_id:
            raise PlexGatewayError("plex_mate의 Machine ID가 비어 있습니다.")
        if (
            require_match
            and expected_machine_id
            and identity.machine_id != expected_machine_id
        ):
            raise PlexGatewayError("Plex Machine ID가 plex_mate 설정과 일치하지 않습니다.")
        return identity

    def list_sections(self) -> List[LibrarySection]:
        container = _decode_container(self._request("GET", "/library/sections"))
        sections: List[LibrarySection] = []
        for item in _as_list(container.get("Directory")):
            if not isinstance(item, dict):
                continue
            section_type = str(item.get("type") or "")
            if section_type not in ("movie", "show"):
                continue
            key = str(item.get("key") or "")
            if key:
                locations = []
                for location in _as_list(item.get("Location")):
                    if not isinstance(location, dict):
                        continue
                    value = str(location.get("path") or "")
                    if value and value not in locations:
                        locations.append(value)
                sections.append(
                    LibrarySection(
                        key=key,
                        title=str(item.get("title") or key),
                        section_type=section_type,
                        locations=tuple(locations),
                    )
                )
        return sections

    def section_locations(self, section_key: str) -> List[str]:
        key = str(section_key or "")
        if not key.isdigit():
            raise PlexGatewayError("잘못된 Plex library section ID입니다.")
        for section in self.list_sections():
            if section.key == key:
                return list(section.locations)
        raise PlexGatewayError("Plex library section을 찾을 수 없습니다.")

    def duplicate_rating_keys(
        self,
        section: LibrarySection,
        cancel_check: Optional[Callable[[], bool]] = None,
        page_size: int = 200,
    ) -> List[str]:
        libtype = "1" if section.section_type == "movie" else "4"
        start = 0
        rating_keys: List[str] = []
        seen = set()
        while True:
            if cancel_check and cancel_check():
                break
            params = {
                "duplicate": "1",
                "type": libtype,
                "includeGuids": "1",
                "includeMedia": "1",
                "X-Plex-Container-Start": str(start),
                "X-Plex-Container-Size": str(page_size),
            }
            container = _decode_container(
                self._request("GET", "/library/sections/%s/all" % section.key, params=params)
            )
            items = _as_list(container.get("Metadata")) + _as_list(container.get("Video"))
            page_count = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("ratingKey") or "")
                if key and key not in seen:
                    seen.add(key)
                    rating_keys.append(key)
                page_count += 1

            total_value = container.get("totalSize")
            total = _as_int(total_value) if total_value not in (None, "") else None
            start += page_count
            if page_count == 0 or (total is not None and start >= total) or page_count < page_size:
                break
        return rating_keys

    def get_metadata(self, rating_key: str) -> MetadataItem:
        if not str(rating_key).isdigit():
            raise PlexGatewayError("잘못된 Plex ratingKey입니다.")
        params = {"includeGuids": "1", "includeMedia": "1"}
        container = _decode_container(
            self._request("GET", "/library/metadata/%s" % rating_key, params=params)
        )
        items = _as_list(container.get("Metadata")) + _as_list(container.get("Video"))
        if not items or not isinstance(items[0], dict):
            raise PlexGatewayError("Plex metadata를 찾을 수 없습니다.")
        return parse_metadata(items[0])

    def delete_media(self, rating_key: str, media_id: str) -> int:
        if not str(rating_key).isdigit() or not str(media_id).isdigit():
            raise PlexGatewayError("잘못된 Plex metadata 또는 media ID입니다.")
        response = self._request(
            "DELETE",
            "/library/metadata/%s/media/%s" % (rating_key, media_id),
        )
        return response.status_code

    def refresh_section_path(self, section_key: str, path: str) -> int:
        key = str(section_key or "")
        target = normalize_remote_path(str(path or ""))
        if not key.isdigit():
            raise PlexGatewayError("잘못된 Plex library section ID입니다.")
        if not target or not is_absolute_remote_path(target):
            raise PlexGatewayError("Plex 부분 스캔 경로는 절대 경로여야 합니다.")
        response = self._request(
            "GET",
            "/library/sections/%s/refresh" % key,
            params={"path": target},
        )
        return response.status_code
