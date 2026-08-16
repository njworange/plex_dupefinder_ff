from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from .domain import PlexConnection


class PlexMateUnavailable(RuntimeError):
    pass


def normalize_base_url(value: str) -> str:
    raw = (value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PlexMateUnavailable("plex_mate의 Plex URL이 올바르지 않습니다.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PlexMateUnavailable("Plex URL에는 인증정보, query, fragment를 넣을 수 없습니다.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


class PlexMateProvider:
    """Resolve PlexMate only when an action starts, never at plugin import time."""

    def __init__(self, plugin_manager: Optional[Any] = None) -> None:
        self._plugin_manager = plugin_manager

    def _manager(self) -> Any:
        if self._plugin_manager is not None:
            return self._plugin_manager
        from framework import F

        return F.PluginManager

    def resolve(self, require_machine_id: bool = False) -> PlexConnection:
        manager = self._manager()
        plex_mate = manager.get_plugin_instance("plex_mate")
        if plex_mate is None or getattr(plex_mate, "ModelSetting", None) is None:
            raise PlexMateUnavailable("plex_mate가 설치되어 로드된 상태여야 합니다.")

        setting = plex_mate.ModelSetting
        base_url = normalize_base_url(setting.get("base_url") or "")
        token = (setting.get("base_token") or "").strip()
        machine_id = (setting.get("base_machine") or "").strip()

        if not token:
            raise PlexMateUnavailable("plex_mate에 Plex 토큰이 설정되어 있지 않습니다.")
        if require_machine_id and not machine_id:
            raise PlexMateUnavailable("삭제를 사용하려면 plex_mate의 Machine ID가 필요합니다.")

        return PlexConnection(base_url=base_url, machine_id=machine_id, token=token)


def redact_secret(message: str, secret: str) -> str:
    value = str(message or "")
    if secret:
        value = value.replace(secret, "***")
    return value
