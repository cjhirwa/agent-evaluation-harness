"""OpenAI Chat Completions provider (works for gpt-4.x and gpt-5.x / o-series reasoning models)."""

from __future__ import annotations

import json
from typing import Any

from .base import ModelResponse, Provider, ToolCall, Usage

REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")


class OpenAIProvider(Provider):
    def __init__(self, model: str, temperature: float | None = 0.0, max_tokens: int = 4096,
                 reasoning_effort: str | None = None, parallel_tool_calls: bool = True, **client_kwargs: Any):
        from openai import OpenAI

        self.model = model
        self.client = OpenAI(max_retries=6, timeout=180, **client_kwargs)
        self.reasoning = model.startswith(REASONING_PREFIXES)
        self.temperature = None if self.reasoning else temperature
        self.max_tokens = max(max_tokens, 16000) if self.reasoning else max_tokens
        self.reasoning_effort = reasoning_effort
        self.parallel_tool_calls = parallel_tool_calls

    @staticmethod
    def _to_wire(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name,
                                      "arguments": tc.arguments if isinstance(tc.arguments, str)
                                      else json.dumps(tc.arguments)}}
                        for tc in m["tool_calls"]
                    ]
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out

    def complete(self, system, messages, tools):
        kwargs: dict[str, Any] = {"model": self.model, "messages": self._to_wire(system, messages),
                                  "max_completion_tokens": self.max_tokens}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
            kwargs["parallel_tool_calls"] = self.parallel_tool_calls

        resp = self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        calls = []
        for tc in choice.message.tool_calls or []:
            try:
                args: Any = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = tc.function.arguments
            calls.append(ToolCall(tc.id, tc.function.name, args))

        u = resp.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        reasoning = getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
        usage = Usage(input_tokens=u.prompt_tokens - cached, cached_input_tokens=cached,
                      output_tokens=u.completion_tokens, reasoning_tokens=reasoning)
        return ModelResponse(text=choice.message.content or "", tool_calls=calls, usage=usage,
                             stop_reason=choice.finish_reason or "")
