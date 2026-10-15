from __future__ import annotations

from typing import Any

from .base import ModelResponse, Provider, ToolCall, Usage

__all__ = ["Provider", "ModelResponse", "ToolCall", "Usage", "make_provider"]


def make_provider(name: str, model: str, **params: Any) -> Provider:
    if name == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(model, **params)
    if name == "ollama":
        from .ollama_provider import OllamaProvider
        return OllamaProvider(model, **params)
    if name == "refuse":
        from .scripted import RefuseProvider
        return RefuseProvider()
    raise ValueError(f"unknown provider {name!r} (expected openai | ollama | refuse)")
