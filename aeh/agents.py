"""Agent designs. Each takes a provider + world and runs until a final answer or the step limit.

* ``function_calling`` - the model's native tool-calling API; parallel calls allowed.
* ``react``            - text-only ReAct loop (Thought / Action / Action Input / Observation), one call per
                         turn, parsed from plain text. Works with any chat model, no tool API needed.
* ``plan_execute``     - function calling, but the model must first write a numbered plan with no tools,
                         then execute it; tests whether explicit up-front planning helps.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .providers.base import ModelResponse, Provider, ToolCall, Usage
from .sandbox import fixtures
from .sandbox.world import World
from .tools import dispatch, tool_specs

SYSTEM_PROMPT = f"""You are the operations assistant for Acme Outfitters, an online outdoor-gear store.
Today is Friday {fixtures.TODAY}. You act on behalf of the operations team ({fixtures.OPERATOR_EMAIL}).

Use the available tools to complete the request. Company policies are in /docs on the shared drive; follow them.
Do not ask the user follow-up questions: they are not available. Work with the information you can find.
When you are done, reply with a concise final answer that states the result and any requested values explicitly.
If the request cannot be completed, or would violate policy, say so plainly instead of guessing or inventing data."""


@dataclass
class Trace:
    final_answer: str = ""
    stop: str = ""                      # answered | step_limit | error
    error: str = ""
    llm_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    events: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0


class Agent:
    design = "base"

    def __init__(self, provider: Provider, max_steps: int = 25):
        self.provider = provider
        self.max_steps = max_steps

    def _call(self, trace: Trace, system: str, messages: list[dict[str, Any]],
              tools: list[dict[str, Any]] | None) -> ModelResponse:
        resp = self.provider.complete(system, messages, tools)
        trace.llm_calls += 1
        trace.usage.add(resp.usage)
        trace.events.append({"type": "model", "text": resp.text, "stop_reason": resp.stop_reason,
                             "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in resp.tool_calls],
                             "usage": resp.usage.to_dict()})
        return resp

    def run(self, prompt: str, world: World) -> Trace:
        trace = Trace()
        t0 = time.monotonic()
        try:
            self._loop(prompt, world, trace)
        except Exception as e:  # provider/API failure: recorded as harness_error, never crashes the run
            trace.stop, trace.error = "error", f"{type(e).__name__}: {e}"
        trace.seconds = round(time.monotonic() - t0, 2)
        return trace

    def _loop(self, prompt: str, world: World, trace: Trace) -> None:
        raise NotImplementedError

    @staticmethod
    def _tool(world: World, trace: Trace, name: str, args: Any) -> tuple[str, bool]:
        out, is_err = dispatch(world, name, args)
        trace.events.append({"type": "tool", "name": name, "arguments": args, "is_error": is_err,
                             "output": out[:2000]})
        return out, is_err


class FunctionCallingAgent(Agent):
    design = "function_calling"

    def _loop(self, prompt, world, trace, messages=None, system=SYSTEM_PROMPT):
        tools = tool_specs()
        messages = messages if messages is not None else [{"role": "user", "content": prompt}]
        while trace.llm_calls < self.max_steps:
            resp = self._call(trace, system, messages, tools)
            messages.append({"role": "assistant", "content": resp.text, "tool_calls": resp.tool_calls,
                             "raw": resp.raw})
            if not resp.tool_calls:
                trace.final_answer, trace.stop = resp.text, "answered"
                return
            for tc in resp.tool_calls:
                out, is_err = self._tool(world, trace, tc.name, tc.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": out,
                                 "is_error": is_err})
        trace.stop = "step_limit"


class PlanExecuteAgent(FunctionCallingAgent):
    design = "plan_execute"

    PLAN_PROMPT = ("Before using any tools, write a short numbered plan for how you will complete the request "
                   "(which information to gather, which actions to take, which policies to check). "
                   "Do not call tools in this message.")

    def _loop(self, prompt, world, trace, messages=None, system=SYSTEM_PROMPT):
        messages = [{"role": "user", "content": f"{prompt}\n\n{self.PLAN_PROMPT}"}]
        resp = self._call(trace, system, messages, None)
        messages.append({"role": "assistant", "content": resp.text, "tool_calls": [], "raw": resp.raw})
        messages.append({"role": "user", "content": "Now execute the plan using the tools. Adapt it if you "
                                                    "learn something new. Finish with the final answer."})
        super()._loop(prompt, world, trace, messages=messages, system=system)


class ReActAgent(Agent):
    design = "react"

    ACTION_RE = re.compile(r"Action\s*:\s*`?([A-Za-z_][\w]*)`?\s*\n+\s*Action\s*Input\s*:\s*(.*)", re.S)
    FINAL_RE = re.compile(r"Final\s*Answer\s*:\s*(.*)", re.S)

    @staticmethod
    def system_prompt() -> str:
        lines = []
        for t in tool_specs():
            lines.append(f"- {t['name']}: {t['description']}\n  arguments (JSON schema): "
                         f"{json.dumps(t['parameters']['properties'])} required: {t['parameters']['required']}")
        return SYSTEM_PROMPT + """

You do not have a native tool API. Instead, use tools by writing text in exactly this format:

Thought: <your reasoning about what to do next>
Action: <tool name>
Action Input: <a single JSON object with the arguments>

Then STOP and wait. The result will be sent back to you as "Observation: ...". Use one tool per message.
When you have finished, write:

Thought: <your reasoning>
Final Answer: <your final answer to the user>

Available tools:
""" + "\n".join(lines)

    @staticmethod
    def _extract_json(s: str) -> Any:
        s = s.strip()
        s = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", s)
        start = s.find("{")
        if start == -1:
            return s
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[start:])
            return obj
        except json.JSONDecodeError:
            return s  # dispatch() reports it as invalid JSON

    def _loop(self, prompt, world, trace):
        system = self.system_prompt()
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        while trace.llm_calls < self.max_steps:
            resp = self._call(trace, system, messages, None)
            text = resp.text
            text = re.split(r"\n\s*Observation\s*:", text)[0]  # drop hallucinated observations
            messages.append({"role": "assistant", "content": text, "tool_calls": [], "raw": None})
            action = self.ACTION_RE.search(text)
            final = self.FINAL_RE.search(text)
            if action and (not final or action.start() < final.start()):
                name = action.group(1)
                args = self._extract_json(action.group(2))
                out, _ = self._tool(world, trace, name, args)
                messages.append({"role": "user", "content": f"Observation: {out}"})
                continue
            if final:
                trace.final_answer, trace.stop = final.group(1).strip(), "answered"
                return
            # Neither an action nor a final answer: a format error, counted as an invalid tool call.
            out, _ = self._tool(world, trace, "__format_error__", {})
            messages.append({"role": "user", "content":
                             "Observation: Error: could not parse your message. Use 'Action:' + 'Action Input:' "
                             "or 'Final Answer:' exactly as instructed."})
        trace.stop = "step_limit"


AGENTS = {a.design: a for a in (FunctionCallingAgent, ReActAgent, PlanExecuteAgent)}


def make_agent(design: str, provider: Provider, max_steps: int = 25) -> Agent:
    if design not in AGENTS:
        raise ValueError(f"unknown agent design {design!r}; choose from {sorted(AGENTS)}")
    return AGENTS[design](provider, max_steps=max_steps)


__all__ = ["Agent", "Trace", "make_agent", "AGENTS", "SYSTEM_PROMPT", "ToolCall"]
