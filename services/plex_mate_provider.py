"""Lazy Plex Mate settings adapter.

Nothing from FlaskFarm is imported until ``resolve`` is called, keeping the
core services importable in unit tests and standalone scripts.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from .domain import PlexConnection


class PlexMateProviderError(RuntimeError):
    pass


class PlexMateUnavailable(PlexMateProviderError):
    pass


class PlexMateConfigurationError(PlexMateProviderError):
    pass


def _normalise_url(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PlexMateConfigurationError("Plex Mate base_url must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PlexMateConfigurationError(
            "Plex Mate base_url may not contain credentials, query, or fragment"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


class PlexMateProvider:
    def __init__(self, plugin_manager: Any = None, *, plugin_name: str = "plex_mate") -> None:
        self._plugin_manager = plugin_manager
        self.plugin_name = plugin_name

    def _manager(self) -> Any:
        if self._plugin_manager is not None:
            return self._plugin_manager
        try:
            from framework import F  # type: ignore
        except (ImportError, AttributeError) as exc:
            raise PlexMateUnavailable("FlaskFarm framework is unavailable") from exc
        manager = getattr(F, "PluginManager", None)
        if manager is None:
            raise PlexMateUnavailable("FlaskFarm PluginManager is unavailable")
        return manager

    def _plugin(self) -> Any:
        manager = self._manager()
        for method_name in ("get_plugin_instance", "get_plugin", "get"):
            method = getattr(manager, method_name, None)
            if callable(method):
                plugin = method(self.plugin_name)
                if plugin is not None:
                    return plugin
        plugins = getattr(manager, "plugins", None)
        if isinstance(plugins, dict) and self.plugin_name in plugins:
            return plugins[self.plugin_name]
        raise PlexMateUnavailable(f"{self.plugin_name!r} plugin is not installed or enabled")

    @staticmethod
    def _setting(plugin: Any, key: str) -> Any:
        candidates = (
            getattr(plugin, "ModelSetting", None),
            getattr(plugin, "model_setting", None),
            getattr(plugin, "settings", None),
            plugin,
        )
        for source in candidates:
            if source is None:
                continue
            if isinstance(source, dict) and key in source:
                return source[key]
            getter = getattr(source, "get", None)
            if callable(getter):
                value = getter(key)
                if value is not None:
                    return value
        return None

    def resolve(self, require_machine_id: bool = False) -> PlexConnection:
        plugin = self._plugin()
        base_url = _normalise_url(self._setting(plugin, "base_url"))
        token = str(self._setting(plugin, "base_token") or "").strip()
        machine_id = str(self._setting(plugin, "base_machine") or "").strip()
        if not token:
            raise PlexMateConfigurationError("Plex Mate base_token is empty")
        if require_machine_id and not machine_id:
            raise PlexMateConfigurationError("Plex Mate base_machine is empty")
        return PlexConnection(base_url=base_url, token=token, machine_id=machine_id)

    get_connection = resolve


PlexMateConnectionProvider = PlexMateProvider


__all__ = [
    "PlexMateConfigurationError",
    "PlexMateConnectionProvider",
    "PlexMateProvider",
    "PlexMateProviderError",
    "PlexMateUnavailable",
]
