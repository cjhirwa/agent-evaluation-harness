"""Command line interface: `aeh tasks | validate | run | report | show`."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from .agents import AGENTS
from .providers.scripted import ScriptedProvider
from .runner import AgentConfig, RunConfig, run, run_episode
from .tasks import load_tasks


def cmd_tasks(args) -> int:
    tasks = load_tasks(args.task_dir) if args.task_dir else load_tasks()
    for t in tasks:
        print(f"{t.id:<30} {t.category:<13} {t.difficulty:<7} {t.prompt[:70]}")
    print(f"\n{len(tasks)} tasks: " + ", ".join(f"{c}={n}" for c, n in Counter(t.category for t in tasks).items()))
    return 0


def cmd_validate(args) -> int:
    """Run every task's oracle script (in both agent designs) and confirm all checks pass."""
    tasks = load_tasks(args.task_dir) if args.task_dir else load_tasks()
    bad = 0
    for t in tasks:
        for design in ("function_calling", "react"):
            cfg = AgentConfig(name=f"oracle-{design}", design=design, provider="scripted", model="scripted")
            rec = run_episode(t, cfg, 0, args.python_backend,
                              provider=ScriptedProvider(t.oracle, react=design == "react"))
            if not rec["success"]:
                bad += 1
                failed = [c for c in rec["checks"] if not c["passed"]]
                print(f"FAIL {t.id} [{design}]: {json.dumps(failed, ensure_ascii=False)}")
    print(f"{len(tasks) * 2 - bad}/{len(tasks) * 2} oracle episodes pass")
    return 1 if bad else 0


def cmd_run(args) -> int:
    cfg = RunConfig.load(args.config)
    if args.trials is not None:
        cfg.trials = args.trials
    if args.tasks:
        cfg.tasks = args.tasks.split(",")
    if args.python_backend:
        cfg.python_backend = args.python_backend
    path = run(cfg, args.out, args.task_dir)
    if not args.no_report:
        from .report import build_report
        print(f"report: {build_report(path.parent)}")
    return 0


def cmd_rescore(args) -> int:
    """Re-apply the current failure-classification rules to stored episodes (no model calls)."""
    from pathlib import Path
    from types import SimpleNamespace

    from .checks import CheckContext, CheckResult, run_checks
    from .sandbox.world import World
    from .scoring import calls_from_trajectory, classify, score_episode

    tasks = {t.id: t for t in (load_tasks(args.task_dir) if args.task_dir else load_tasks())}
    path = Path(args.run_dir) / "episodes.jsonl"
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    changed = Counter()
    for r in rows:
        t = tasks[r["task_id"]]
        faults = t.setup.get("faults", [])
        world = SimpleNamespace(calls=calls_from_trajectory(r["trajectory"], [f["error"] for f in faults]))
        # Answer checks only read the final answer, so they can be recomputed exactly; state and safety
        # checks need the final world, which is not stored, so their stored results are kept.
        initial = World.build(t.setup)
        fresh = run_checks(t.checks, CheckContext(answer=r["final_answer"], world=initial, initial=initial))
        checks = [f if f.kind == "answer" else CheckResult(c["type"], c["kind"], c["passed"], c["detail"], c.get("label"))
                  for f, c in zip(fresh, r["checks"])]
        r["checks"] = [c.to_dict() for c in checks]
        r.update(score_episode(checks, r["final_answer"]))
        new = classify(checks, feasible=t.feasible, stop=r["stop"], answer=r["final_answer"], world=world,
                       fault_tools=[f["tool"] for f in faults])
        if new != r["failure_type"]:
            changed[(r["failure_type"], new)] += 1
            r["failure_type"] = new
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    for (old, new), n in sorted(changed.items()):
        print(f"{old} -> {new}: {n}")
    print(f"rescored {len(rows)} episodes, {sum(changed.values())} changed")
    return 0


def cmd_report(args) -> int:
    from .report import build_report
    print(build_report(args.run_dir).read_text(encoding="utf-8"))
    return 0


def cmd_show(args) -> int:
    """Pretty-print one episode's trajectory for debugging."""
    from .report import load_episodes
    for e in load_episodes(args.run_dir):
        if e["task_id"] == args.task and e["agent"] == args.agent and e["trial"] == args.trial:
            print(f"# {e['agent']} / {e['task_id']} / trial {e['trial']}: {e['failure_type']}\n")
            for ev in e["trajectory"]:
                if ev["type"] == "model":
                    if ev["text"]:
                        print(f"MODEL: {ev['text']}\n")
                    for c in ev["tool_calls"]:
                        print(f"  CALL {c['name']}({json.dumps(c['arguments'], ensure_ascii=False)})")
                else:
                    tag = "ERR " if ev["is_error"] else ""
                    print(f"  -> {tag}{ev['output'][:400]}\n")
            print("CHECKS:")
            for c in e["checks"]:
                print(f"  [{'x' if c['passed'] else ' '}] {c['kind']:<6} {c['type']}: {c['detail'][:200]}")
            return 0
    print("episode not found", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aeh", description="Agent Evaluation Harness")
    p.add_argument("--task-dir", default=None, help="directory of task YAML files (default: ./tasks)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tasks", help="list tasks").set_defaults(fn=cmd_tasks)

    v = sub.add_parser("validate", help="check every task is solvable by its oracle script")
    v.add_argument("--python-backend", default="subprocess", choices=["subprocess", "docker"])
    v.set_defaults(fn=cmd_validate)

    r = sub.add_parser("run", help="run agents from a config file")
    r.add_argument("config")
    r.add_argument("--out", required=True, help="output directory (resumable)")
    r.add_argument("--trials", type=int)
    r.add_argument("--tasks", help="comma-separated task ids")
    r.add_argument("--python-backend", choices=["subprocess", "docker"])
    r.add_argument("--no-report", action="store_true")
    r.set_defaults(fn=cmd_run)

    rep = sub.add_parser("report", help="aggregate a run directory into report.md")
    rep.add_argument("run_dir")
    rep.set_defaults(fn=cmd_report)

    rs = sub.add_parser("rescore", help="re-apply failure classification to a run directory")
    rs.add_argument("run_dir")
    rs.set_defaults(fn=cmd_rescore)

    s = sub.add_parser("show", help="print one episode trajectory")
    s.add_argument("run_dir")
    s.add_argument("--agent", required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--trial", type=int, default=0)
    s.set_defaults(fn=cmd_show)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "AGENTS"]
