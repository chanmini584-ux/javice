"""Built-in tools: memory, goals/plans, exact calculation, and sandboxed Python."""

from __future__ import annotations

import ast
import math
import operator
import subprocess
import sys
import tempfile
from dataclasses import asdict

from ..memory import GOAL_STATUSES, MEMORY_KINDS, STEP_STATUSES, MemoryStore
from .registry import ToolError, ToolRegistry

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MATH_NAMES = {n: getattr(math, n) for n in dir(math) if not n.startswith("_")}
_MATH_NAMES.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum})


def safe_eval(expression: str) -> float | int:
    """Evaluate arithmetic + math.* functions without exec/eval."""

    def ev(node: ast.AST):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10_000:
                raise ToolError("exponent too large")
            return _BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _MATH_NAMES:
            return _MATH_NAMES[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _MATH_NAMES:
            if node.keywords:
                raise ToolError("keyword arguments are not supported")
            return _MATH_NAMES[node.func.id](*[ev(a) for a in node.args])
        if isinstance(node, (ast.List, ast.Tuple)):
            return [ev(e) for e in node.elts]
        raise ToolError(f"unsupported expression element: {type(node).__name__}")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"syntax error: {exc.msg}") from exc
    return ev(tree)


def register_builtin_tools(registry: ToolRegistry, store: MemoryStore) -> None:
    # --- memory --------------------------------------------------------------

    @registry.tool(
        "remember",
        "Save a durable fact about the user to long-term memory. Use it whenever you learn "
        "something that will still matter later: preferences, skills, weaknesses the user "
        "wants covered, important events, or insights about how they work best. Do not "
        "store secrets or one-off chatter.",
        {
            "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
            "content": {"type": "string", "description": "One self-contained sentence."},
            "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        },
    )
    def remember(kind: str, content: str, importance: int) -> str:
        return f"saved memory #{store.remember(kind, content, importance)}"

    @registry.tool(
        "recall",
        "Search long-term memory. Pass space-separated keywords (any language); an empty "
        "query returns the most important memories. Optionally filter by kind.",
        {
            "query": {"type": "string"},
            "kind": {"type": "string", "enum": ["", *MEMORY_KINDS]},
        },
        required=["query"],
    )
    def recall(query: str, kind: str = "") -> list[dict]:
        return [asdict(m) for m in store.recall(query, kind or None)]

    @registry.tool(
        "forget",
        "Delete a memory by id, e.g. when the user corrects it or it is outdated.",
        {"memory_id": {"type": "integer"}},
    )
    def forget(memory_id: int) -> str:
        return "deleted" if store.forget(memory_id) else f"no memory #{memory_id}"

    @registry.tool(
        "update_profile",
        "Set a core profile field (e.g. name, role, location, timezone, language, "
        "work_hours, skill_level). Profile fields are shown to you on every turn.",
        {"key": {"type": "string"}, "value": {"type": "string"}},
    )
    def update_profile(key: str, value: str) -> str:
        store.set_profile(key.strip().lower(), value.strip())
        return "profile updated"

    @registry.tool(
        "search_memos",
        "Search voice memos: things the user said out loud while JARVIS was listening. "
        "Empty query lists the most recent ones.",
        {"query": {"type": "string"}},
    )
    def search_memos(query: str) -> list[dict]:
        memos = store.memos(limit=200 if query.strip() else 20)
        terms = query.lower().split()
        hits = [m for m in memos if all(t in m.text.lower() for t in terms)]
        return [asdict(m) for m in hits[:20]]

    # --- goals & plans -------------------------------------------------------

    @registry.tool(
        "add_goal",
        "Record a goal the user is working toward. Ask for or infer the motivation (why).",
        {
            "title": {"type": "string"},
            "why": {"type": "string"},
            "deadline": {"type": "string", "description": "ISO date (YYYY-MM-DD) or empty."},
        },
        required=["title", "why"],
    )
    def add_goal(title: str, why: str, deadline: str = "") -> str:
        return f"goal #{store.add_goal(title, why, deadline or None)} created"

    @registry.tool(
        "list_goals",
        "List goals with their plans. status: active (default), done, dropped, or all.",
        {"status": {"type": "string", "enum": [*GOAL_STATUSES, "all"]}},
        required=[],
    )
    def list_goals(status: str = "active") -> list[dict]:
        goals = store.goals(None if status == "all" else status)
        return [{**asdict(g), "plan": [asdict(s) for s in store.plan(g.id)]} for g in goals]

    @registry.tool(
        "save_plan",
        "Save (replace) the ordered step plan for a goal. Steps should be concrete and "
        "verifiable; put risky or uncertain steps early so they fail fast.",
        {
            "goal_id": {"type": "integer"},
            "steps": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
    )
    def save_plan(goal_id: int, steps: list[str]) -> str:
        ids = store.save_plan(goal_id, steps)
        return f"saved {len(ids)} steps for goal #{goal_id}"

    @registry.tool(
        "update_status",
        "Update the status of a plan step (target=step) or a goal (target=goal).",
        {
            "target": {"type": "string", "enum": ["step", "goal"]},
            "id": {"type": "integer"},
            "status": {"type": "string", "enum": sorted({*STEP_STATUSES, *GOAL_STATUSES})},
        },
    )
    def update_status(target: str, id: int, status: str) -> str:
        ok = store.set_step_status(id, status) if target == "step" else store.set_goal_status(id, status)
        return "updated" if ok else f"no {target} #{id}"

    from .schedule import register_schedule_tools

    register_schedule_tools(registry, store)

    # --- computation ---------------------------------------------------------

    @registry.tool(
        "calculate",
        "Evaluate an arithmetic expression exactly (+ - * / // % **, and math functions "
        "such as sqrt, log, exp, sin). Always use this instead of mental arithmetic.",
        {"expression": {"type": "string"}},
    )
    def calculate(expression: str) -> str:
        return repr(safe_eval(expression))

    @registry.tool(
        "run_python",
        "Run a short Python 3 script in a subprocess and return stdout/stderr. Use it for "
        "data analysis, simulations, or calculations too complex for `calculate`. "
        "Requires the user's approval each time.",
        {"code": {"type": "string"}},
        risk="confirm",
    )
    def run_python(code: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                proc = subprocess.run(
                    [sys.executable, "-I", "-c", code],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired as exc:
                raise ToolError("timed out after 30s") from exc
        out = f"exit={proc.returncode}\n--- stdout ---\n{proc.stdout[-8000:]}"
        if proc.stderr:
            out += f"\n--- stderr ---\n{proc.stderr[-4000:]}"
        return out
