"""Embed benchmark results and task metadata into docs/index.html.

Usage: python scripts/build_page_data.py

The page reads everything it renders from the JSON block
<script id="results-data" type="application/json">...</script>, so regenerating it after a run keeps the
page in sync with results/ without hand-editing numbers.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aeh.report import load_episodes, summarize  # noqa: E402
from aeh.runner import RunConfig  # noqa: E402
from aeh.tasks import load_tasks  # noqa: E402

# (group id, label, run directory, max trial index to include, config used to detect an unfinished run)
# The OpenAI run was stopped part-way through its third trial; only trials 0-1 are complete.
GROUPS = [
    ("llama", "Llama 3.1 8B, local (main comparison)", ROOT / "results" / "llama", None, "configs/llama.yaml"),
    ("openai", "OpenAI models (reference, 2 trials)", ROOT / "results" / "main", 1, None),
]
PAGE = ROOT / "docs" / "index.html"
KEEP = ["design", "model", "episodes", "trials", "success_rate", "ci95", "pass_hat_k", "partial_credit",
        "cost_per_episode_usd", "cost_total_usd", "llm_calls_mean", "tool_calls_mean", "invalid_calls_mean",
        "tokens_mean", "seconds_mean", "failures", "by_category", "by_task"]


def main() -> None:
    data: dict = {"generated": date.today().isoformat(), "groups": [], "tasks": []}
    for gid, label, run_dir, max_trial, config in GROUPS:
        if not (run_dir / "episodes.jsonl").exists():
            print(f"skip {gid}: no episodes")
            continue
        eps = [e for e in load_episodes(run_dir) if max_trial is None or e["trial"] <= max_trial]
        summary = summarize(eps)
        if config:
            cfg = RunConfig.load(ROOT / config)
            expected = len(load_tasks()) * len(cfg.agents) * cfg.trials
            if len(eps) < expected:
                label = f"{label.split(' (')[0]} (in progress: {len(eps)} of {expected} episodes)"
        data["groups"].append({
            "id": gid, "label": label,
            "agents": [{"name": name, **{k: round(v, 6) if isinstance(v, float) else v
                                         for k, v in s.items() if k in KEEP}}
                       for name, s in summary.items()],
        })
        print(f"{gid}: {len(eps)} episodes, {len(summary)} agents")

    for t in load_tasks():
        data["tasks"].append({
            "id": t.id, "category": t.category, "difficulty": t.difficulty, "feasible": t.feasible,
            "prompt": t.prompt, "faults": [f["tool"] for f in t.setup.get("faults", [])],
            "checks": [{"type": c["type"], "kind": c.get("kind")} for c in t.checks],
        })

    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = PAGE.read_text(encoding="utf-8")
    pattern = re.compile(r'(<script id="results-data" type="application/json">)(.*?)(</script>)', re.S)
    if not pattern.search(html):
        raise SystemExit("results-data block not found in docs/index.html")
    PAGE.write_text(pattern.sub(lambda m: m.group(1) + blob + m.group(3), html), encoding="utf-8")
    print(f"updated {PAGE.relative_to(ROOT)} ({len(blob):,} bytes of data)")


if __name__ == "__main__":
    main()
