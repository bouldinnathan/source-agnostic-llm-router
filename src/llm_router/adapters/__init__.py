"""Built-in and extensible adapter registry."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import Any

from ..errors import ConfigError
from .anthropic import AnthropicMessagesAdapter
from .base import Adapter
from .gemini import GeminiGenerateContentAdapter
from .generic import GenericJSONAdapter
from .ollama import OllamaChatAdapter
from .openai import OpenAIChatAdapter, OpenAIResponsesAdapter

BUILTIN_ADAPTERS: dict[str, type[Any]] = {
    "openai-chat": OpenAIChatAdapter,
    "openai-compatible": OpenAIChatAdapter,
    "openai-responses": OpenAIResponsesAdapter,
    "anthropic-messages": AnthropicMessagesAdapter,
    "anthropic": AnthropicMessagesAdapter,
    "gemini-generate": GeminiGenerateContentAdapter,
    "gemini": GeminiGenerateContentAdapter,
    "ollama-chat": OllamaChatAdapter,
    "ollama": OllamaChatAdapter,
    "generic-json": GenericJSONAdapter,
}


class AdapterRegistry:
    def __init__(self) -> None:
        self._instances: dict[str, Adapter] = {}

    def register(self, name: str, adapter: Adapter) -> None:
        self._instances[name] = adapter

    def get(self, name: str) -> Adapter:
        if name in self._instances:
            return self._instances[name]
        if name in BUILTIN_ADAPTERS:
            instance = BUILTIN_ADAPTERS[name]()
            self._instances[name] = instance
            return instance

        loaded = self._load_entry_point(name) if ":" not in name else self._load_reference(name)
        if isinstance(loaded, type):
            loaded = loaded()
        if not hasattr(loaded, "complete"):
            raise ConfigError(f"Adapter '{name}' does not define an async complete method")
        self._instances[name] = loaded
        return loaded

    @staticmethod
    def _load_entry_point(name: str) -> Any:
        matches = list(entry_points(group="llm_router.adapters", name=name))
        if not matches:
            builtins = ", ".join(sorted(BUILTIN_ADAPTERS))
            raise ConfigError(
                f"Unknown adapter '{name}'. Built-ins: {builtins}; "
                "custom adapters may use module:object or the llm_router.adapters entry-point group"
            )
        return matches[0].load()

    @staticmethod
    def _load_reference(reference: str) -> Any:
        module_name, object_name = reference.split(":", 1)
        try:
            module = importlib.import_module(module_name)
            return getattr(module, object_name)
        except (ImportError, AttributeError) as exc:
            raise ConfigError(f"Could not load adapter '{reference}': {exc}") from exc

__all__ = ["Adapter", "AdapterRegistry", "BUILTIN_ADAPTERS"]
