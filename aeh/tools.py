"""Tool definitions exposed to agents, and the dispatcher that runs them against a `World`.

Every tool is a plain function `fn(world, **args) -> str`. The JSON schemas below are what models see;
`dispatch` validates arguments against them, applies injected faults, runs the tool and records the call.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from .sandbox import fixtures
from .sandbox.python_exec import run_python as _run_python
from .sandbox.world import ToolCallRecord, ToolError, World

MAX_ROWS = 50
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


# ---------------------------------------------------------------------------------------------- database
def get_schema(w: World) -> str:
    rows = w.query("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    return "\n\n".join(r[0] for r in rows)


def sql_query(w: World, query: str) -> str:
    q = query.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", q, re.I):
        raise ToolError("only read-only SELECT queries are allowed; use the dedicated tools to change data")
    w.db.execute("PRAGMA query_only = ON")
    try:
        cur = w.db.execute(q)
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as e:
        raise ToolError(f"SQL error: {e}") from None
    finally:
        w.db.execute("PRAGMA query_only = OFF")
    cols = [d[0] for d in cur.description or []]
    truncated = len(rows) > MAX_ROWS
    return json.dumps(
        {"columns": cols, "rows": [list(r) for r in rows[:MAX_ROWS]], "row_count": min(len(rows), MAX_ROWS),
         "truncated": truncated},
        ensure_ascii=False,
    )


def _order(w: World, order_id: int) -> tuple:
    row = w.query("SELECT id, status, total_usd FROM orders WHERE id = ?", (order_id,))
    if not row:
        raise ToolError(f"order {order_id} not found")
    return row[0]


def issue_refund(w: World, order_id: int, amount_usd: float, reason: str) -> str:
    _, status, total = _order(w, order_id)
    if status not in ("delivered", "shipped"):
        raise ToolError(f"order {order_id} has status '{status}' and cannot be refunded")
    if amount_usd <= 0:
        raise ToolError("amount_usd must be positive")
    already = w.query("SELECT COALESCE(SUM(amount_usd), 0) FROM refunds WHERE order_id = ?", (order_id,))[0][0]
    if round(already + amount_usd, 2) > round(total, 2):
        raise ToolError(f"refund exceeds order total: total {total:.2f}, already refunded {already:.2f}")
    cur = w.db.execute(
        "INSERT INTO refunds (order_id, amount_usd, reason, created_at) VALUES (?,?,?,?)",
        (order_id, round(amount_usd, 2), reason, w.today),
    )
    if round(already + amount_usd, 2) == round(total, 2):
        w.db.execute("UPDATE orders SET status = 'refunded' WHERE id = ?", (order_id,))
    w.db.commit()
    return json.dumps({"refund_id": cur.lastrowid, "order_id": order_id, "amount_usd": round(amount_usd, 2)})


def update_order_status(w: World, order_id: int, status: str) -> str:
    _, current, _ = _order(w, order_id)
    allowed = {"pending": {"cancelled", "shipped"}, "shipped": {"delivered"}}
    if status not in allowed.get(current, set()):
        raise ToolError(f"cannot change order {order_id} from '{current}' to '{status}'")
    w.db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    w.db.commit()
    return json.dumps({"order_id": order_id, "old_status": current, "new_status": status})


def adjust_stock(w: World, sku: str, delta: int) -> str:
    row = w.query("SELECT stock FROM products WHERE sku = ?", (sku,))
    if not row:
        raise ToolError(f"unknown sku '{sku}'")
    new = row[0][0] + delta
    if new < 0:
        raise ToolError(f"stock for {sku} cannot go below zero (current {row[0][0]})")
    w.db.execute("UPDATE products SET stock = ? WHERE sku = ?", (new, sku))
    w.db.commit()
    return json.dumps({"sku": sku, "old_stock": row[0][0], "new_stock": new})


# ---------------------------------------------------------------------------------------------- files
def list_files(w: World, directory: str = "/") -> str:
    d = w.norm_path(directory)
    prefix = d.rstrip("/") + "/"
    entries: set[str] = set()
    for p in w.files:
        if p.startswith(prefix):
            rest = p[len(prefix):]
            entries.add(rest.split("/")[0] + ("/" if "/" in rest else ""))
    if not entries:
        raise ToolError(f"directory '{d}' not found or empty")
    return "\n".join(sorted(entries))


def read_file(w: World, path: str) -> str:
    p = w.norm_path(path)
    if p not in w.files:
        raise ToolError(f"file '{p}' not found")
    return w.files[p]


def write_file(w: World, path: str, content: str) -> str:
    p = w.norm_path(path)
    if p.startswith("/docs/"):
        raise ToolError("/docs is read-only")
    w.files[p] = content
    return f"wrote {len(content)} chars to {p}"


def delete_file(w: World, path: str) -> str:
    p = w.norm_path(path)
    if p not in w.files:
        raise ToolError(f"file '{p}' not found")
    if p.startswith("/docs/"):
        raise ToolError("/docs is read-only")
    del w.files[p]
    return f"deleted {p}"


def run_python(w: World, code: str) -> str:
    out, new_files = _run_python(code, w.files, backend=w.python_backend)
    for p in list(w.files):
        if p.startswith("/docs/"):
            new_files[p] = w.files[p]  # keep /docs read-only even from python
    w.files.clear()
    w.files.update(new_files)
    return out


# ---------------------------------------------------------------------------------------------- email
def search_emails(w: World, query: str = "", unread_only: bool = False) -> str:
    """Word-level search like a real mail client: messages matching more query terms rank first."""
    terms = [t for t in re.findall(r"[\w.@#-]+", query.lower()) if len(t) > 1]
    scored = []
    for m in w.inbox:
        if unread_only and not m["unread"]:
            continue
        text = (m["subject"] + " " + m["body"] + " " + m["from"]).lower()
        score = sum(t.lstrip("#") in text for t in terms)
        if terms and score == 0:
            continue
        scored.append((-score, m))
    hits = [{"id": m["id"], "from": m["from"], "date": m["date"], "subject": m["subject"], "unread": m["unread"]}
            for _, m in sorted(scored, key=lambda x: x[0])]
    return json.dumps(hits, ensure_ascii=False)


def read_email(w: World, email_id: str) -> str:
    for m in w.inbox:
        if m["id"] == email_id:
            m["unread"] = False
            return json.dumps({k: m[k] for k in ("id", "from", "to", "date", "subject", "body")}, ensure_ascii=False)
    raise ToolError(f"email '{email_id}' not found")


def send_email(w: World, to: str, subject: str, body: str) -> str:
    if not EMAIL_RE.match(to or ""):
        raise ToolError(f"invalid recipient address '{to}'")
    msg_id = f"S{len(w.outbox) + 1}"
    w.outbox.append({"id": msg_id, "to": to.strip().lower(), "subject": subject, "body": body})
    return json.dumps({"sent": True, "message_id": msg_id})


# ---------------------------------------------------------------------------------------------- calendar
def list_events(w: World, date: str) -> str:
    if not DATE_RE.match(date):
        raise ToolError("date must be YYYY-MM-DD")
    evs = sorted((e for e in w.calendar if e["date"] == date), key=lambda e: e["start"])
    return json.dumps(evs, ensure_ascii=False)


def create_event(w: World, title: str, date: str, start_time: str, end_time: str,
                 attendees: list[str] | None = None) -> str:
    if not DATE_RE.match(date):
        raise ToolError("date must be YYYY-MM-DD")
    if not (TIME_RE.match(start_time) and TIME_RE.match(end_time)):
        raise ToolError("times must be HH:MM (24h)")
    if end_time <= start_time:
        raise ToolError("end_time must be after start_time")
    attendees = [a.strip().lower() for a in (attendees or [])]
    ev = {"id": f"NEW{len(w.calendar) + 1}", "title": title, "date": date, "start": start_time,
          "end": end_time, "attendees": attendees}
    w.calendar.append(ev)
    return json.dumps({"created": True, "event": ev})


# ---------------------------------------------------------------------------------------------- external APIs
def get_exchange_rate(w: World, base: str, quote: str) -> str:
    rates = fixtures.EXCHANGE_RATES_FROM_USD
    b, q = base.upper(), quote.upper()
    if b not in rates or q not in rates:
        raise ToolError(f"unsupported currency pair {b}/{q}; supported: {', '.join(rates)}")
    return json.dumps({"base": b, "quote": q, "rate": round(rates[q] / rates[b], 6), "as_of": w.today})


def get_shipping_quote(w: World, country: str, weight_kg: float) -> str:
    if weight_kg <= 0:
        raise ToolError("weight_kg must be positive")
    base, per_kg = fixtures.SHIPPING_ZONES.get(country.upper(), fixtures.SHIPPING_DEFAULT_ZONE)
    return json.dumps({"country": country.upper(), "weight_kg": weight_kg,
                       "price_usd": round(base + per_kg * weight_kg, 2)})


# ---------------------------------------------------------------------------------------------- registry
TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_schema", "Return the CREATE TABLE statements for the store database.", _obj({}), get_schema),
    Tool("sql_query",
         f"Run a read-only SQL (SQLite) SELECT query against the store database. Returns at most {MAX_ROWS} rows as JSON.",
         _obj({"query": {"type": "string", "description": "A single SELECT statement."}}, ["query"]), sql_query),
    Tool("issue_refund", "Issue a refund against an order. Records it in the refunds table.",
         _obj({"order_id": {"type": "integer"}, "amount_usd": {"type": "number"},
               "reason": {"type": "string"}}, ["order_id", "amount_usd", "reason"]), issue_refund),
    Tool("update_order_status", "Change an order's status (e.g. pending -> cancelled).",
         _obj({"order_id": {"type": "integer"},
               "status": {"type": "string", "enum": ["pending", "shipped", "delivered", "cancelled"]}},
              ["order_id", "status"]), update_order_status),
    Tool("adjust_stock", "Add (positive delta) or remove (negative delta) units of a product from inventory.",
         _obj({"sku": {"type": "string"}, "delta": {"type": "integer"}}, ["sku", "delta"]), adjust_stock),
    Tool("list_files", "List the entries of a directory in the shared drive. Sub-directories end with '/'.",
         _obj({"directory": {"type": "string", "description": "Absolute path, e.g. '/' or '/data'."}}),
         list_files),
    Tool("read_file", "Read a text file from the shared drive.",
         _obj({"path": {"type": "string"}}, ["path"]), read_file),
    Tool("write_file", "Create or overwrite a text file on the shared drive.",
         _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]), write_file),
    Tool("delete_file", "Delete a file from the shared drive.", _obj({"path": {"type": "string"}}, ["path"]),
         delete_file),
    Tool("run_python",
         "Execute a Python 3 script and return its stdout/stderr. The shared drive is mounted at the working "
         "directory (e.g. open('data/sales_q4.csv')). Files written there are saved back to the drive. "
         "No network access. Print what you need to see.",
         _obj({"code": {"type": "string"}}, ["code"]), run_python),
    Tool("search_emails", "Search the operations inbox. Matches any query word; best matches first. An empty query returns every message. Returns id, sender, "
         "date, subject and unread flag (no bodies; use read_email).",
         _obj({"query": {"type": "string"}, "unread_only": {"type": "boolean"}}), search_emails),
    Tool("read_email", "Read one email by id (marks it as read).", _obj({"email_id": {"type": "string"}}, ["email_id"]),
         read_email),
    Tool("send_email", "Send an email from ops@acme.example.",
         _obj({"to": {"type": "string", "description": "A single recipient address."},
               "subject": {"type": "string"}, "body": {"type": "string"}}, ["to", "subject", "body"]), send_email),
    Tool("list_events", "List the operations team's calendar events on a date.",
         _obj({"date": {"type": "string", "description": "YYYY-MM-DD"}}, ["date"]), list_events),
    Tool("create_event", "Create a calendar event. Does not check for conflicts.",
         _obj({"title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
               "start_time": {"type": "string", "description": "HH:MM, 24h"},
               "end_time": {"type": "string", "description": "HH:MM, 24h"},
               "attendees": {"type": "array", "items": {"type": "string"}}},
              ["title", "date", "start_time", "end_time"]), create_event),
    Tool("get_exchange_rate", "Get the current exchange rate from one currency to another (external FX service).",
         _obj({"base": {"type": "string", "description": "ISO code, e.g. USD"},
               "quote": {"type": "string", "description": "ISO code, e.g. EUR"}}, ["base", "quote"]),
         get_exchange_rate),
    Tool("get_shipping_quote", "Get a shipping price quote in USD for a package (external carrier service).",
         _obj({"country": {"type": "string", "description": "ISO 3166 alpha-2 code"},
               "weight_kg": {"type": "number"}}, ["country", "weight_kg"]), get_shipping_quote),
]}


def tool_specs() -> list[dict[str, Any]]:
    return [t.spec() for t in TOOLS.values()]


# ---------------------------------------------------------------------------------------------- dispatch
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def validate_args(schema: dict[str, Any], args: Any) -> str | None:
    """Minimal JSON-schema check (types, required, enum, no extras). Returns an error message or None."""
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    props = schema.get("properties", {})
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"
    extra = [k for k in args if k not in props]
    if extra:
        return f"unexpected argument(s): {', '.join(extra)}"
    for k, v in args.items():
        spec = props[k]
        expected = _TYPES[spec["type"]]
        if isinstance(v, bool) and spec["type"] in ("integer", "number"):
            return f"argument '{k}' must be {spec['type']}"
        if spec["type"] == "integer" and isinstance(v, float) and v.is_integer():
            args[k] = int(v)
            continue
        if not isinstance(v, expected):
            return f"argument '{k}' must be {spec['type']}"
        if "enum" in spec and v not in spec["enum"]:
            return f"argument '{k}' must be one of {spec['enum']}"
    return None


def dispatch(world: World, name: str, raw_args: Any) -> tuple[str, bool]:
    """Run one tool call. Returns (output text, is_error). Never raises for agent mistakes."""
    args: Any = raw_args
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as e:
            msg = f"Error: arguments are not valid JSON ({e.msg})"
            world.calls.append(ToolCallRecord(name, {"_raw": raw_args}, False, "invalid", msg))
            return msg, True

    tool = TOOLS.get(name)
    if tool is None:
        msg = f"Error: unknown tool '{name}'. Available tools: {', '.join(TOOLS)}"
        world.calls.append(ToolCallRecord(name, args if isinstance(args, dict) else {}, False, "invalid", msg))
        return msg, True

    err = validate_args(tool.parameters, args)
    if err:
        msg = f"Error: invalid arguments for {name}: {err}"
        world.calls.append(ToolCallRecord(name, args if isinstance(args, dict) else {}, False, "invalid", msg))
        return msg, True

    fault = world.faults.get(name)
    if fault and fault.calls_seen < fault.fail_times:
        fault.calls_seen += 1
        msg = f"Error: {fault.error}"
        world.calls.append(ToolCallRecord(name, args, False, "fault", msg))
        return msg, True

    try:
        out = tool.fn(world, **args)
        world.calls.append(ToolCallRecord(name, args, True, None, out[:500]))
        return out, False
    except ToolError as e:
        msg = f"Error: {e}"
        world.calls.append(ToolCallRecord(name, args, False, "runtime", msg))
        return msg, True
