from app.plugins.base import ParserPlugin
from app.plugins.darknet import DarknetPlugin
from app.plugins.telegram import TelegramPlugin


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, ParserPlugin] = {}

    def register(self, plugin: ParserPlugin) -> None:
        self._plugins[plugin.parser_type] = plugin

    def get(self, parser_type: str) -> ParserPlugin:
        plugin = self._plugins.get(parser_type)
        if not plugin:
            raise KeyError(f"Unknown parser_type: {parser_type}")
        return plugin

    def list_types(self) -> list[str]:
        return sorted(self._plugins.keys())

    def is_known(self, parser_type: str) -> bool:
        return str(parser_type) in self._plugins


plugin_registry = PluginRegistry()
plugin_registry.register(TelegramPlugin())
plugin_registry.register(DarknetPlugin())
