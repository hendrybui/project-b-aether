"""Registry of AudioMass processing plugins (named capabilities).

Plugins register an instance under a name; the pipeline and API endpoints
look them up and drive them through the uniform PluginContext contract.
Built-in plugins self-register when ``plugins/__init__.py`` imports them, so
adding a capability = drop a module in ``backend/plugins/`` and import it
there.
"""

from __future__ import annotations


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, object] = {}

    def register(self, plugin: object) -> object:
        name = getattr(plugin, "name", "")
        if not name:
            raise ValueError("Plugin must define a non-empty 'name'")
        if name in self._plugins:
            raise ValueError(f"Plugin already registered: {name}")
        self._plugins[name] = plugin
        return plugin

    def get(self, name: str) -> object | None:
        return self._plugins.get(name)

    def require(self, name: str) -> object:
        plugin = self._plugins.get(name)
        if plugin is None:
            raise KeyError(f"No plugin registered as '{name}'")
        return plugin

    def names(self) -> list[str]:
        return sorted(self._plugins)

    def all(self) -> list:
        return list(self._plugins.values())


plugin_registry = PluginRegistry()
