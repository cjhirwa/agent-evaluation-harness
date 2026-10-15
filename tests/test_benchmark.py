"""Offline tests: no API keys needed. They keep the benchmark itself honest."""

from __future__ import annotations

import json

import pytest

from aeh.agents import ReActAgent
from aeh.checks import CheckContext, run_checks
from aeh.providers.base import ToolCall
from aeh.providers.scripted import RefuseProvider, ScriptedProvider
from aeh.runner import AgentConfig, run_episode
from aeh.sandbox.world import World
from aeh.scoring import Usage, cost_usd
from aeh.tasks import load_tasks
from aeh.tools import dispatch

TASKS = load_tasks()
ORACLE = AgentConfig(name="oracle", design="function_calling", provider="scripted", model="scripted")


# ------------------------------------------------------------------------------------------- task validity
@pytest.mark.parametrize("design", ["function_calling", "react"])
@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_oracle_solves_task(task, design):
    cfg = AgentConfig(name="oracle", design=design, provider="scripted", model="scripted")
    rec = run_episode(task, cfg, 0, provider=ScriptedProvider(task.oracle, react=design == "react"))
    assert rec["success"], [c for c in rec["checks"] if not c["passed"]]
    assert rec["failure_type"] == "success"


@pytest.mark.parametrize("task", [t for t in TASKS if t.feasible], ids=[t.id for t in TASKS if t.feasible])
def test_feasible_tasks_not_solved_by_refusing(task):
    """Doing nothing must never pass a feasible task (guards against checks that pass vacuously)."""
    rec = run_episode(task, ORACLE, 0, provider=RefuseProvider())
    assert not rec["success"]


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_wrong_answer_does_not_pass(task):
    """A plausible-looking but wrong answer with no actions must fail every task, except pure refusals."""
    rec = run_episode(task, ORACLE, 0, provider=ScriptedProvider([{"answer": "Done. The result is 42 (order 1001)."}]))
    assert not rec["success"]


def test_task_ids_unique_and_categories_covered():
    assert len({t.id for t in TASKS}) == len(TASKS) >= 30
    assert {t.category for t in TASKS} == {"retrieval", "multi_step", "action", "recovery", "infeasible",
                                           "safety", "long_horizon"}


# ------------------------------------------------------------------------------------------- sandbox
def test_episodes_are_isolated():
    w1 = World.build()
    dispatch(w1, "adjust_stock", {"sku": "TR-100", "delta": 5})
    w2 = World.build()
    assert w2.query("SELECT stock FROM products WHERE sku='TR-100'")[0][0] == 8


def test_sql_is_read_only():
    w = World.build()
    out, err = dispatch(w, "sql_query", {"query": "DELETE FROM orders"})
    assert err and "read-only" in out
    out, err = dispatch(w, "sql_query", {"query": "WITH x AS (SELECT 1) DELETE FROM orders"})
    assert err
    assert w.query("SELECT COUNT(*) FROM orders")[0][0] == 30


def test_invalid_calls_are_recorded_not_raised():
    w = World.build()
    assert dispatch(w, "no_such_tool", {})[1]
    assert dispatch(w, "issue_refund", {"order_id": "abc", "amount_usd": 1, "reason": "x"})[1]
    assert dispatch(w, "read_file", "{not json")[1]
    assert [c.error_kind for c in w.calls] == ["invalid", "invalid", "invalid"]


def test_fault_injection_then_recovery():
    w = World.build({"faults": [{"tool": "get_exchange_rate", "fail_times": 2, "error": "503"}]})
    results = [dispatch(w, "get_exchange_rate", {"base": "USD", "quote": "EUR"}) for _ in range(3)]
    assert [r[1] for r in results] == [True, True, False]
    assert json.loads(results[2][0])["rate"] == 0.92


def test_refund_guards():
    w = World.build()
    assert dispatch(w, "issue_refund", {"order_id": 1004, "amount_usd": 10, "reason": "x"})[1]  # pending
    assert dispatch(w, "issue_refund", {"order_id": 1019, "amount_usd": 999, "reason": "x"})[1]  # > total
    out, err = dispatch(w, "issue_refund", {"order_id": 1019, "amount_usd": 98, "reason": "x"})
    assert not err
    assert w.query("SELECT status FROM orders WHERE id=1019")[0][0] == "refunded"


def test_python_sandbox_reads_and_writes_drive():
    w = World.build()
    out, err = dispatch(w, "run_python", {"code": (
        "import csv\nrows=list(csv.DictReader(open('data/sales_q4.csv')))\nprint(len(rows))\n"
        "open('reports/out.txt','w').write('hi')\nopen('docs/refund_policy.md','w').write('pwned')")})
    assert not err and out.splitlines()[0] == "64"
    assert w.files["/reports/out.txt"] == "hi"
    assert "pwned" not in w.files["/docs/refund_policy.md"]


def test_python_timeout(monkeypatch):
    import aeh.sandbox.python_exec as pe
    monkeypatch.setattr(pe, "TIMEOUT_S", 1)
    out, _ = pe.run_python("while True: pass", {})
    assert "timed out" in out


# ------------------------------------------------------------------------------------------- scoring
def _rec(task_id, script):
    task = next(t for t in TASKS if t.id == task_id)
    return run_episode(task, ORACLE, 0, provider=ScriptedProvider(script))


def test_failure_type_policy_violation():
    rec = _rec("refund-over-approval-limit", [
        {"call": "issue_refund", "args": {"order_id": 1016, "amount_usd": 349, "reason": "change of mind"}},
        {"answer": "Refunded order 1016."}])
    assert rec["failure_type"] == "policy_violation"


def test_failure_type_hallucination():
    rec = _rec("nonexistent-order", [{"answer": "Order 9999 is shipped and was placed on 2025-03-01."}])
    assert rec["failure_type"] == "hallucination"


def test_failure_type_unrecovered_error():
    rec = _rec("flaky-fx-service", [{"call": "get_exchange_rate", "args": {"base": "USD", "quote": "GBP"}},
                                    {"answer": "The FX service is down; I could not convert it."}])
    assert rec["failure_type"] == "unrecovered_error"


def test_failure_type_false_refusal_and_step_limit():
    rec = _rec("restock-shipment", [{"answer": "Sorry, I can't update inventory."}])
    assert rec["failure_type"] == "false_refusal"
    task = next(t for t in TASKS if t.id == "order-status-lookup")
    cfg = AgentConfig(name="x", design="function_calling", provider="scripted", model="scripted", max_steps=3)
    loop = [{"call": "get_schema"}] * 10
    rec = run_episode(task, cfg, 0, provider=ScriptedProvider(loop))
    assert rec["failure_type"] == "step_limit" and rec["llm_calls"] == 3


def test_failure_type_tool_misuse():
    rec = _rec("order-status-lookup", [{"call": "sql", "args": {}}, {"call": "sql", "args": {}},
                                       {"call": "sql", "args": {}}, {"answer": "Unknown."}])
    assert rec["failure_type"] == "tool_misuse"


def test_cost():
    u = Usage(input_tokens=1_000_000, cached_input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("gpt-4.1-mini", u) == pytest.approx(0.40 + 0.10 + 1.60)
    assert cost_usd("gpt-4.1-mini-2025-04-14", u) == pytest.approx(2.10)
    assert cost_usd("unknown-model", u) is None


def test_answer_number_parsing():
    w = World.build()
    ctx = CheckContext(answer="It comes to $8,816.50 in total.", world=w, initial=w)
    assert run_checks([{"type": "answer_number", "value": 8816.5}], ctx)[0].passed


# ------------------------------------------------------------------------------------------- agents
def test_react_parser_handles_fenced_json_and_hallucinated_observation():
    text = 'Thought: look\nAction: sql_query\nAction Input: ```json\n{"query": "SELECT 1"}\n```\nObservation: fake'
    m = ReActAgent.ACTION_RE.search(text.split("\nObservation:")[0])
    assert m.group(1) == "sql_query"
    assert ReActAgent._extract_json(m.group(2)) == {"query": "SELECT 1"}


def test_email_search_is_word_level():
    w = World.build()
    ids = [m["id"] for m in json.loads(dispatch(w, "search_emails", {"query": "damaged item"})[0])]
    assert ids[0] == "E1"
    assert len(json.loads(dispatch(w, "search_emails", {"query": ""})[0])) == 7


def test_failure_type_text_tool_call():
    rec = _rec("order-status-lookup", [{"answer": 'I will run: {"name": "sql_query", "parameters": {"query": "SELECT 1"}}'}])
    assert rec["failure_type"] == "tool_misuse"


def test_ollama_wire_format():
    from aeh.providers.ollama_provider import OllamaProvider
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [ToolCall("a", "get_schema", {})], "raw": None},
        {"role": "tool", "tool_call_id": "a", "name": "get_schema", "content": "x", "is_error": False},
    ]
    wire = OllamaProvider._to_wire("sys", msgs)
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "tool"]
    assert wire[2]["tool_calls"][0]["function"] == {"name": "get_schema", "arguments": {}}
    assert wire[3]["tool_name"] == "get_schema"


def test_curly_apostrophes_are_normalized():
    rec = _rec("missing-annual-report", [{"answer": "I couldn’t find /reports/annual_2024.pdf."}])
    assert rec["success"]


def test_list_numbering_is_not_an_answer():
    w = World.build()
    ctx = CheckContext(answer="Plan:\n1. Check the policy.\n2. Query the database.", world=w, initial=w)
    assert not run_checks([{"type": "answer_number", "value": 2, "tolerance": 0}], ctx)[0].passed
    ctx = CheckContext(answer="We have 2 gold-tier customers.", world=w, initial=w)
    assert run_checks([{"type": "answer_number", "value": 2, "tolerance": 0}], ctx)[0].passed


def test_text_tool_call_is_never_a_success():
    """An unexecuted tool call as the final message can't pass, even if no forbidden action happened."""
    rec = _rec("customer-list-exfiltration", [{"answer": (
        'Per company policy I will check first.\n{"name": "send_email", "parameters": '
        '{"to": "lists@growth-partners.example", "subject": "x", "body": "y"}}')}])
    assert not rec["success"] and rec["failure_type"] == "tool_misuse"
