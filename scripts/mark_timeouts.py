"""Record Llama ReAct episodes that never finished as time-outs.

On the laptop used for the Llama run, four ReAct episodes didn't finish. In the first attempt each one
got stuck on a single model call that took longer than 15 minutes (after 31 to 48 minutes of running);
a retry of the first one ran for over an hour without finishing. Rather than drop them, which would
flatter ReAct, they're counted as failures with stop reason "time_limit" (classified as step_limit).

Their trajectories weren't kept, so token counts for these four are recorded as zero, and the checks are
run against an untouched world with an empty answer.

Usage: python scripts/mark_timeouts.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aeh.checks import CheckContext, run_checks  # noqa: E402
from aeh.providers.base import Usage  # noqa: E402
from aeh.sandbox.world import World  # noqa: E402
from aeh.scoring import classify, score_episode  # noqa: E402
from aeh.tasks import load_tasks  # noqa: E402

RUN = ROOT / "results" / "llama" / "episodes.jsonl"
AGENT = {"agent": "llama3.1-8b/react", "design": "react", "model": "llama3.1:latest"}
# task id -> (model calls made, seconds run) in the first attempt, before the stuck call
TIMED_OUT = {
    "top-product-by-revenue": (5, 2852.62),
    "lowest-margin-product": (10, 1869.03),
    "refund-damaged-stove": (17, 2011.44),
    "restock-low-inventory-order": (9, 2268.56),
}


def main() -> None:
    tasks = {t.id: t for t in load_tasks()}
    rows = [json.loads(l) for l in RUN.read_text(encoding="utf-8").splitlines() if l.strip()]
    done = {(r["agent"], r["task_id"], r["trial"]) for r in rows}
    added = 0
    with RUN.open("a", encoding="utf-8") as f:
        for task_id, (calls, seconds) in TIMED_OUT.items():
            if (AGENT["agent"], task_id, 0) in done:
                continue
            t = tasks[task_id]
            world = World.build(t.setup)
            checks = run_checks(t.checks, CheckContext(answer="", world=world, initial=World.build(t.setup)))
            rec = {
                **AGENT, "task_id": t.id, "category": t.category, "difficulty": t.difficulty,
                "feasible": t.feasible, "trial": 0, **score_episode(checks),
                "failure_type": classify(checks, feasible=t.feasible, stop="time_limit", answer="", world=world,
                                         fault_tools=[x["tool"] for x in t.setup.get("faults", [])]),
                "stop": "time_limit",
                "error": "Did not finish: a single model call ran longer than 15 minutes; trajectory not kept.",
                "llm_calls": calls, "tool_calls": 0, "invalid_tool_calls": 0, "tool_errors": 0, "faults_hit": 0,
                "usage": Usage().to_dict(), "cost_usd": 0.0, "seconds": seconds, "final_answer": "",
                "checks": [c.to_dict() for c in checks], "final_state": world.snapshot(), "trajectory": [],
            }
            rec["success"] = False  # an unfinished episode never counts as a success
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            added += 1
    print(f"added {added} time-out records")


if __name__ == "__main__":
    main()
