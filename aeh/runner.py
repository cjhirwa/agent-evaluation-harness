"""Run agents over tasks and write one JSON line per episode.

Results are appended to ``<out_dir>/episodes.jsonl``; re-running the same command resumes and skips
episodes that already completed (harness errors are retried).
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .agents import make_agent
from .checks import CheckContext, run_checks
from .providers import Provider, make_provider
from .scoring import classify, cost_usd, score_episode, tool_stats
from .tasks import Task, load_tasks
from .sandbox.world import World


@dataclass
class AgentConfig:
    name: str
    design: str
    provider: str
    model: str
    params: dict[str, Any] = field(default_factory=dict)
    max_steps: int = 25

    def make_provider(self) -> Provider:
        return make_provider(self.provider, self.model, **self.params)


@dataclass
class RunConfig:
    agents: list[AgentConfig]
    trials: int = 3
    tasks: list[str] | None = None
    categories: list[str] | None = None
    concurrency: int = 8
    python_backend: str = "subprocess"
    pricing: dict[str, list[float]] = field(default_factory=dict)
    budget_usd: float | None = None

    @classmethod
    def load(cls, path: str | Path) -> "RunConfig":
        d = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        d["agents"] = [AgentConfig(**a) for a in d["agents"]]
        return cls(**d)


def run_episode(task: Task, agent_cfg: AgentConfig, trial: int, python_backend: str = "subprocess",
                pricing: dict[str, list[float]] | None = None, provider: Provider | None = None) -> dict[str, Any]:
    world = World.build(task.setup, python_backend=python_backend)
    agent = make_agent(agent_cfg.design, provider or agent_cfg.make_provider(), max_steps=agent_cfg.max_steps)
    trace = agent.run(task.prompt, world)

    initial = World.build(task.setup)
    checks = run_checks(task.checks, CheckContext(answer=trace.final_answer, world=world, initial=initial))
    failure = classify(checks, feasible=task.feasible, stop=trace.stop, answer=trace.final_answer, world=world,
                       fault_tools=[f["tool"] for f in task.setup.get("faults", [])])
    return {
        "agent": agent_cfg.name, "design": agent_cfg.design, "model": agent_cfg.model,
        "task_id": task.id, "category": task.category, "difficulty": task.difficulty, "feasible": task.feasible,
        "trial": trial,
        **score_episode(checks, trace.final_answer),
        "failure_type": failure,
        "stop": trace.stop, "error": trace.error,
        "llm_calls": trace.llm_calls, **tool_stats(world),
        "usage": trace.usage.to_dict(),
        "cost_usd": cost_usd(agent_cfg.model, trace.usage, pricing),
        "seconds": trace.seconds,
        "final_answer": trace.final_answer,
        "checks": [c.to_dict() for c in checks],
        "final_state": world.snapshot(),
        "trajectory": trace.events,
    }


def _done_keys(path: Path) -> set[tuple[str, str, int]]:
    keys = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r["failure_type"] != "harness_error":
                    keys.add((r["agent"], r["task_id"], r["trial"]))
    return keys


def run(cfg: RunConfig, out_dir: str | Path, task_dir: str | Path | None = None, log=print) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "episodes.jsonl"
    tasks = load_tasks(task_dir, ids=cfg.tasks, categories=cfg.categories) if task_dir else \
        load_tasks(ids=cfg.tasks, categories=cfg.categories)

    done = _done_keys(path)
    if path.exists():  # drop harness errors so they are retried
        kept = [l for l in path.read_text(encoding="utf-8").splitlines()
                if l.strip() and json.loads(l)["failure_type"] != "harness_error"]
        path.write_text("".join(l + "\n" for l in kept), encoding="utf-8")

    jobs = [(t, a, k) for k in range(cfg.trials) for a in cfg.agents for t in tasks
            if (a.name, t.id, k) not in done]
    log(f"{len(tasks)} tasks x {len(cfg.agents)} agents x {cfg.trials} trials; {len(jobs)} episodes to run "
        f"({len(done)} already done)")

    lock = threading.Lock()
    spent = 0.0
    stop_flag = threading.Event()

    def work(job):
        if stop_flag.is_set():
            return None
        t, a, k = job
        return run_episode(t, a, k, cfg.python_backend, cfg.pricing)

    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool, path.open("a", encoding="utf-8") as f:
        futures = [pool.submit(work, j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            rec = fut.result()
            if rec is None:
                continue
            with lock:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                spent += rec["cost_usd"] or 0.0
            mark = "PASS" if rec["success"] else rec["failure_type"]
            log(f"[{i}/{len(jobs)}] {rec['agent']:<24} {rec['task_id']:<28} t{rec['trial']} {mark:<18} "
                f"${rec['cost_usd'] or 0:.4f} steps={rec['llm_calls']} (total ${spent:.2f})")
            if cfg.budget_usd is not None and spent >= cfg.budget_usd and not stop_flag.is_set():
                stop_flag.set()
                log(f"Budget ${cfg.budget_usd:.2f} reached; finishing in-flight episodes and stopping.")
    return path
