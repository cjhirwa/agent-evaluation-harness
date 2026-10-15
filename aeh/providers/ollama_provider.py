"""Local models through Ollama's native /api/chat endpoint (no extra dependencies).

The native API is used instead of Ollama's OpenAI-compatible endpoint because it lets us set
``num_ctx``: Ollama's default context window is small and silently truncates long agent transcripts.
Responses whose prompt reaches the context limit are flagged with stop_reason "context_limit".
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from .base import ModelResponse, Provider, ToolCall, Usage


class OllamaProvider(Provider):
    def __init__(self, model: str = "llama3.1:latest", num_ctx: int = 16384, temperature: float = 0.0,
                 max_tokens: int = 2048, host: str | None = None, timeout: float = 3600):
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.timeout = timeout

    @staticmethod
    def _to_wire(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {"function": {"name": tc.name,
                                      "arguments": tc.arguments if isinstance(tc.arguments, dict) else {}}}
                        for tc in m["tool_calls"]
                    ]
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "content": m["content"], "tool_name": m["name"]})
        return out

    def complete(self, system, messages, tools):
        body: dict[str, Any] = {
            "model": self.model, "stream": False, "messages": self._to_wire(system, messages),
            "options": {"num_ctx": self.num_ctx, "temperature": self.temperature, "num_predict": self.max_tokens},
        }
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        req = urllib.request.Request(f"{self.host}/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            resp = json.load(r)

        msg = resp.get("message", {})
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass
            calls.append(ToolCall(tc.get("id") or f"call_{len(messages)}_{i}", fn.get("name", ""), args))

        prompt = resp.get("prompt_eval_count", 0) or 0
        cached = resp.get("prompt_eval_cached_count", 0) or 0
        usage = Usage(input_tokens=prompt - cached, cached_input_tokens=cached,
                      output_tokens=resp.get("eval_count", 0) or 0)
        stop = resp.get("done_reason", "")
        if prompt >= self.num_ctx - 16:
            stop = "context_limit"
        return ModelResponse(text=msg.get("content", "") or "", tool_calls=calls, usage=usage, stop_reason=stop)
