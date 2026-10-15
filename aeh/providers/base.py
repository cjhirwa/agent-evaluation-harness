"""Provider-neutral model interface.

Agents talk to models through `Provider.complete`, using a small neutral message format:

    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall, ...], "raw": <provider-native payload>}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str, "is_error": bool}

Each provider converts this to its wire format. Assistant turns keep the provider-native payload in
``raw`` so they can be replayed unchanged (required e.g. for reasoning/thinking blocks).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Any  # dict when the model produced valid JSON, else the raw string


@dataclass
class Usage:
    input_tokens: int = 0          # uncached prompt tokens
    cached_input_tokens: int = 0   # prompt tokens read from cache
    cache_write_tokens: int = 0    # prompt tokens written to cache, for providers that bill it
    output_tokens: int = 0         # includes reasoning tokens
    reasoning_tokens: int = 0      # subset of output_tokens, when reported

    def add(self, other: "Usage") -> None:
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + getattr(other, k))

    def to_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""
    raw: Any = None


class Provider(ABC):
    model: str

    @abstractmethod
    def complete(self, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None) -> ModelResponse:
        """One model call. `tools` are neutral specs: {name, description, parameters}."""
