"""The sandbox world: all mutable state an agent can observe or change during one episode.

A `World` is built fresh for every episode from the fixtures plus the task's `setup` block, so episodes
are isolated from each other and fully deterministic. Tools (see `aeh.tools`) are the only way an agent
touches the world; every call is recorded in `World.calls` for scoring.
"""

from __future__ import annotations

import copy
import posixpath
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import fixtures


@dataclass
class ToolCallRecord:
    tool: str
    args: dict[str, Any]
    ok: bool
    error_kind: str | None = None  # "invalid" (bad name/args) | "runtime" (tool raised) | "fault" (injected)
    output: str = ""


@dataclass
class Fault:
    """Injected failure: the first `fail_times` calls to `tool` return `error` instead of running."""

    tool: str
    fail_times: int
    error: str
    calls_seen: int = 0


class ToolError(Exception):
    """Raised by a tool to return an error message to the agent (counts as a runtime tool error)."""


@dataclass
class World:
    db: sqlite3.Connection
    files: dict[str, str]
    inbox: list[dict[str, Any]]
    outbox: list[dict[str, Any]] = field(default_factory=list)
    calendar: list[dict[str, Any]] = field(default_factory=list)
    faults: dict[str, Fault] = field(default_factory=dict)
    calls: list[ToolCallRecord] = field(default_factory=list)
    python_backend: str = "subprocess"
    today: str = fixtures.TODAY

    # ------------------------------------------------------------------ construction
    @classmethod
    def build(cls, setup: dict[str, Any] | None = None, python_backend: str = "subprocess") -> "World":
        setup = setup or {}
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.executescript(fixtures.SCHEMA)
        db.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", fixtures.CUSTOMERS)
        db.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", fixtures.PRODUCTS)
        prices = {p[0]: p[3] for p in fixtures.PRODUCTS}
        for oid, cid, date, status, channel, items in fixtures.ORDERS:
            total = round(sum(prices[s] * q for s, q in items), 2)
            db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)", (oid, cid, date, status, channel, total))
            db.executemany(
                "INSERT INTO order_items VALUES (?,?,?,?)", [(oid, s, q, prices[s]) for s, q in items]
            )
        db.executemany(
            "INSERT INTO refunds (order_id, amount_usd, reason, created_at) VALUES (?,?,?,?)", fixtures.REFUNDS
        )
        for stmt in setup.get("sql", []):
            db.execute(stmt)
        db.commit()

        files = dict(fixtures.FILES)
        for path, content in (setup.get("files") or {}).items():
            if content is None:
                files.pop(path, None)
            else:
                files[path] = content

        inbox = copy.deepcopy(fixtures.EMAILS) + copy.deepcopy(setup.get("emails", []))
        calendar = copy.deepcopy(fixtures.CALENDAR) + copy.deepcopy(setup.get("events", []))
        faults = {
            f["tool"]: Fault(tool=f["tool"], fail_times=int(f.get("fail_times", 1)), error=f["error"])
            for f in setup.get("faults", [])
        }
        return cls(db=db, files=files, inbox=inbox, calendar=calendar, faults=faults, python_backend=python_backend)

    # ------------------------------------------------------------------ helpers used by tools and checks
    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        return self.db.execute(sql, params).fetchall()

    @staticmethod
    def norm_path(path: str) -> str:
        if not isinstance(path, str) or not path.strip():
            raise ToolError("path must be a non-empty string")
        p = path.strip().replace("\\", "/")
        if not p.startswith("/"):
            p = "/" + p
        p = posixpath.normpath(p)
        return "/" if p in ("/", "//") else p

    def snapshot(self) -> dict[str, Any]:
        """A JSON-serialisable view of the final state, stored with each episode for debugging."""
        return {
            "outbox": self.outbox,
            "new_events": [e for e in self.calendar if not e["id"].startswith("EV")],
            "refunds": [list(r) for r in self.query("SELECT order_id, amount_usd, reason FROM refunds ORDER BY id")],
            "files_changed": sorted(
                p for p in set(self.files) | set(fixtures.FILES) if self.files.get(p) != fixtures.FILES.get(p)
            ),
        }
