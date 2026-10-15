"""Aggregate episodes.jsonl into a markdown report, a JSON summary and (optionally) charts."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .scoring import FAILURE_TYPES
from .tasks import CATEGORIES


def load_episodes(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if p.is_dir():
        p = p / "episodes.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def summarize(eps: list[dict[str, Any]]) -> dict[str, Any]:
    by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in eps:
        by_agent[e["agent"]].append(e)

    out: dict[str, Any] = {}
    for agent, rows in by_agent.items():
        valid = [r for r in rows if r["failure_type"] != "harness_error"]
        n = len(valid)
        k = sum(r["success"] for r in valid)
        lo, hi = wilson(k, n)
        by_task: dict[str, list[bool]] = defaultdict(list)
        for r in valid:
            by_task[r["task_id"]].append(r["success"])
        trials = max((len(v) for v in by_task.values()), default=0)
        pass_all = [all(v) for v in by_task.values() if len(v) == trials]
        cost = sum(r["cost_usd"] or 0 for r in valid)
        out[agent] = {
            "design": rows[0]["design"], "model": rows[0]["model"],
            "episodes": n, "harness_errors": len(rows) - n, "trials": trials,
            "success_rate": k / n if n else 0.0, "ci95": [lo, hi],
            "pass_hat_k": sum(pass_all) / len(pass_all) if pass_all else 0.0,
            "partial_credit": sum(r["partial_credit"] for r in valid) / n if n else 0.0,
            "cost_total_usd": cost,
            "cost_per_episode_usd": cost / n if n else 0.0,
            "cost_per_success_usd": cost / k if k else None,
            "llm_calls_mean": sum(r["llm_calls"] for r in valid) / n if n else 0.0,
            "tool_calls_mean": sum(r["tool_calls"] for r in valid) / n if n else 0.0,
            "invalid_calls_mean": sum(r["invalid_tool_calls"] for r in valid) / n if n else 0.0,
            "tokens_mean": sum(r["usage"]["input_tokens"] + r["usage"]["cached_input_tokens"]
                               + r["usage"]["cache_write_tokens"] + r["usage"]["output_tokens"]
                               for r in valid) / n if n else 0.0,
            "seconds_mean": sum(r["seconds"] for r in valid) / n if n else 0.0,
            "failures": dict(Counter(r["failure_type"] for r in valid)),
            "by_category": {
                c: (sum(r["success"] for r in valid if r["category"] == c)
                    / max(1, sum(r["category"] == c for r in valid)))
                for c in CATEGORIES if any(r["category"] == c for r in valid)
            },
            "by_task": {t: sum(v) / len(v) for t, v in sorted(by_task.items())},
        }
    return out


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def markdown(summary: dict[str, Any]) -> str:
    agents = list(summary)
    L = ["# Results", "", "## Overall", "",
         "| Agent | Model | Design | Success (95% CI) | pass^k | Partial | $/episode | $/success | LLM calls | Tool calls | Invalid calls | Tokens | Sec |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in agents:
        s = summary[a]
        cps = f"${s['cost_per_success_usd']:.4f}" if s["cost_per_success_usd"] is not None else "n/a"
        L.append(f"| {a} | {s['model']} | {s['design']} | {_pct(s['success_rate'])} "
                 f"({_pct(s['ci95'][0])}-{_pct(s['ci95'][1])}) | {_pct(s['pass_hat_k'])} | "
                 f"{_pct(s['partial_credit'])} | ${s['cost_per_episode_usd']:.4f} | {cps} | "
                 f"{s['llm_calls_mean']:.1f} | {s['tool_calls_mean']:.1f} | {s['invalid_calls_mean']:.2f} | "
                 f"{s['tokens_mean']:,.0f} | {s['seconds_mean']:.1f} |")
    L += ["", f"pass^k = share of tasks solved in all {summary[agents[0]]['trials']} trials." if agents else "", "",
          "## Success by category", "", "| Category | " + " | ".join(agents) + " |",
          "|---|" + "---|" * len(agents)]
    for c in CATEGORIES:
        if any(c in summary[a]["by_category"] for a in agents):
            L.append(f"| {c} | " + " | ".join(_pct(summary[a]["by_category"].get(c, 0)) for a in agents) + " |")
    L += ["", "## Failure types (episodes)", "", "| Failure type | " + " | ".join(agents) + " |",
          "|---|" + "---|" * len(agents)]
    for f in FAILURE_TYPES:
        if any(summary[a]["failures"].get(f) for a in agents):
            L.append(f"| {f} | " + " | ".join(str(summary[a]["failures"].get(f, 0)) for a in agents) + " |")
    tasks = sorted({t for a in agents for t in summary[a]["by_task"]})
    L += ["", "## Per-task success rate", "", "| Task | " + " | ".join(agents) + " |",
          "|---|" + "---|" * len(agents)]
    for t in tasks:
        L.append(f"| {t} | " + " | ".join(_pct(summary[a]["by_task"].get(t, 0)) for a in agents) + " |")
    return "\n".join(L) + "\n"


def charts(summary: dict[str, Any], out_dir: Path) -> list[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    agents = list(summary)
    paths = []
    colors = ["#2a6fdb", "#e0782f", "#2f9e6b", "#9b59b6", "#c0392b", "#7f8c8d"]

    # Local models cost nothing per token, so for free runs plot tokens instead of dollars.
    free = all(not summary[a]["cost_total_usd"] for a in agents)
    xs = {a: summary[a]["tokens_mean"] if free else summary[a]["cost_per_episode_usd"] * 1000 for a in agents}
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for i, a in enumerate(agents):
        s = summary[a]
        if xs[a] <= 0:
            continue  # the refuse baseline uses no tokens and costs nothing; it can't sit on a log axis
        x, y = xs[a], s["success_rate"] * 100
        lo, hi = s["ci95"][0] * 100, s["ci95"][1] * 100
        ax.errorbar(x, y, yerr=[[y - lo], [hi - y]], fmt="o", color=colors[i % len(colors)], capsize=4, ms=8)
        ax.annotate(a, (x, y), textcoords="offset points", xytext=(8, 6), fontsize=9)
    ax.set_xscale("log")
    ax.set_xlabel("Mean tokens per episode (log scale)" if free else "Mean cost per episode (USD x 1e-3, log scale)")
    ax.set_ylabel("Success rate (%)")
    ax.set_title(("Success vs tokens" if free else "Success vs cost") + " (95% Wilson CI)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    p = out_dir / "success_vs_cost.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(p)

    fails = [f for f in FAILURE_TYPES if f != "success" and any(summary[a]["failures"].get(f) for a in agents)]
    if fails:
        fig, ax = plt.subplots(figsize=(8, 4.5))
        left = [0.0] * len(agents)
        cmap = plt.get_cmap("tab10")
        for j, f in enumerate(fails):
            vals = [summary[a]["failures"].get(f, 0) / max(1, summary[a]["episodes"]) * 100 for a in agents]
            ax.barh(agents, vals, left=left, label=f, color=cmap(j % 10))
            left = [l + v for l, v in zip(left, vals)]
        ax.set_xlabel("% of episodes")
        ax.set_title("Failure types")
        ax.legend(fontsize=8, loc="lower right")
        ax.invert_yaxis()
        fig.tight_layout()
        p = out_dir / "failure_types.png"
        fig.savefig(p, dpi=150)
        plt.close(fig)
        paths.append(p)
    return paths


def build_report(run_dir: str | Path) -> Path:
    run_dir = Path(run_dir)
    summary = summarize(load_episodes(run_dir))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    md = markdown(summary)
    imgs = charts(summary, run_dir)
    if imgs:
        md += "\n## Charts\n\n" + "\n".join(f"![{p.stem}]({p.name})" for p in imgs) + "\n"
    out = run_dir / "report.md"
    out.write_text(md, encoding="utf-8")
    return out
