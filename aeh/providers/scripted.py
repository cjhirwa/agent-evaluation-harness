"""Deterministic providers that need no API: used for oracle validation, baselines and tests."""

from __future__ import annotations

import json
from typing import Any

from .base import ModelResponse, Provider, ToolCall, Usage


class ScriptedProvider(Provider):
    """Replays a fixed script of steps, one per model call.

    Each step is either ``{"call": tool_name, "args": {...}}`` (a group of consecutive calls is issued
    in one turn when a step sets ``"parallel": true``), or ``{"answer": "final text"}``.
    In ReAct mode the same script is rendered as Thought/Action/Final Answer text.
    """

    def __init__(self, script: list[dict[str, Any]], model: str = "scripted", react: bool = False):
        self.model = model
        self.react = react
        # ReAct can only issue one action per turn, so parallel groups are played sequentially.
        self._turns = [[s] for s in script] if react else self._group(script)
        self._i = 0

    @staticmethod
    def _group(script: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        turns: list[list[dict[str, Any]]] = []
        for step in script:
            if turns and step.get("parallel") and "call" in turns[-1][-1]:
                turns[-1].append(step)
            else:
                turns.append([step])
        return turns

    def complete(self, system, messages, tools):
        if self._i >= len(self._turns):
            return ModelResponse(text="(script exhausted)", usage=Usage(input_tokens=1, output_tokens=1))
        turn = self._turns[self._i]
        self._i += 1
        usage = Usage(input_tokens=100, output_tokens=20)
        if "answer" in turn[0]:
            text = turn[0]["answer"]
            return ModelResponse(text=f"Final Answer: {text}" if self.react else text, usage=usage)
        if self.react:
            step = turn[0]
            return ModelResponse(text=f"Thought: next step.\nAction: {step['call']}\n"
                                      f"Action Input: {json.dumps(step.get('args', {}))}", usage=usage)
        calls = [ToolCall(f"call_{self._i}_{j}", s["call"], dict(s.get("args", {}))) for j, s in enumerate(turn)]
        return ModelResponse(text="", tool_calls=calls, usage=usage)


class RefuseProvider(Provider):
    """Baseline that never acts and always declines. Measures how much a benchmark rewards inaction."""

    model = "always-refuse"

    def complete(self, system, messages, tools):
        return ModelResponse(text="I'm sorry, I can't complete this request.", usage=Usage())
