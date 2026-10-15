"""Task definitions: loaded from YAML files in `tasks/`.

A task is a natural-language request plus:
  * ``setup``  - optional overrides applied to the seed world (extra SQL, files, emails, injected faults)
  * ``checks`` - deterministic assertions over the final answer and final world state (see `aeh.checks`)
  * ``oracle`` - a scripted reference solution, used by the test-suite to prove the task is solvable
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TASK_DIR = Path(__file__).resolve().parent.parent / "tasks"

CATEGORIES = [
    "retrieval",      # find facts with one or two lookups
    "multi_step",     # chain several tools and compute
    "action",         # change world state correctly
    "recovery",       # cope with faults, wrong paths, hidden schema
    "infeasible",     # the request cannot be done; agent must say so instead of inventing
    "safety",         # policy limits, prompt injection, destructive operations
    "long_horizon",   # many dependent steps across tools
]


@dataclass
class Task:
    id: str
    category: str
    difficulty: str
    prompt: str
    checks: list[dict[str, Any]]
    feasible: bool = True
    setup: dict[str, Any] = field(default_factory=dict)
    oracle: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        unknown = set(d) - {f for f in cls.__dataclass_fields__}
        if unknown:
            raise ValueError(f"task {d.get('id')}: unknown fields {sorted(unknown)}")
        task = cls(**d)
        if task.category not in CATEGORIES:
            raise ValueError(f"task {task.id}: unknown category {task.category!r}")
        if task.difficulty not in ("easy", "medium", "hard"):
            raise ValueError(f"task {task.id}: difficulty must be easy|medium|hard")
        if not task.checks:
            raise ValueError(f"task {task.id}: needs at least one check")
        return task


def load_tasks(task_dir: str | Path = DEFAULT_TASK_DIR, ids: list[str] | None = None,
               categories: list[str] | None = None) -> list[Task]:
    tasks: list[Task] = []
    for path in sorted(Path(task_dir).glob("*.yaml")):
        try:
            items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except yaml.YAMLError as e:
            raise ValueError(f"{path.name}: invalid YAML: {e}") from None
        for item in items:
            tasks.append(Task.from_dict(item))
    seen: set[str] = set()
    for t in tasks:
        if t.id in seen:
            raise ValueError(f"duplicate task id {t.id}")
        seen.add(t.id)
    if ids:
        missing = set(ids) - seen
        if missing:
            raise ValueError(f"unknown task ids: {sorted(missing)}")
        tasks = [t for t in tasks if t.id in ids]
    if categories:
        tasks = [t for t in tasks if t.category in categories]
    return tasks
