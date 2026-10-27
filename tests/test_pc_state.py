"""PC control state: settings store, audit log, per-call risk in the registry and agent."""

import json
from types import SimpleNamespace as NS

import pytest

from jarvis.agent import Jarvis
from jarvis.config import Settings
from jarvis.memory import MemoryStore
from jarvis.pc.store import AuditLog, PcSettings, PcSettingsStore
from jarvis.tools.registry import Tool, ToolRegistry


# --- settings store ---------------------------------------------------------


def test_pc_control_is_off_by_default(tmp_path):
    store = PcSettingsStore(tmp_path / "pc.json", home=tmp_path)
    cfg = store.load()
    assert cfg.enabled is False
    assert cfg.roots  # a sensible default: the usual user folders


def test_default_roots_are_user_folders_under_home(tmp_path):
    cfg = PcSettingsStore(tmp_path / "pc.json", home=tmp_path).load()
    assert all(r.startswith(str(tmp_path)) for r in cfg.roots)


def test_update_persists_and_reloads(tmp_path):
    path = tmp_path / "pc.json"
    PcSettingsStore(path, home=tmp_path).update(enabled=True)
    assert PcSettingsStore(path, home=tmp_path).load().enabled is True


def test_update_roots_are_normalized_and_deduplicated(tmp_path):
    store = PcSettingsStore(tmp_path / "pc.json", home=tmp_path)
    folder = tmp_path / "work"
    folder.mkdir()
    cfg = store.update(roots=[str(folder), str(folder) + "/", "  "])
    assert cfg.roots == [str(folder.resolve())]


def test_update_rejects_non_list_roots_and_bad_apps(tmp_path):
    store = PcSettingsStore(tmp_path / "pc.json", home=tmp_path)
    with pytest.raises(ValueError):
        store.update(roots="C:/")
    with pytest.raises(ValueError):
        store.update(apps={"memo": 123})


def test_corrupt_file_falls_back_to_safe_defaults(tmp_path):
    path = tmp_path / "pc.json"
    path.write_text("{not json", encoding="utf-8")
    assert PcSettingsStore(path, home=tmp_path).load().enabled is False


def test_wrong_types_in_file_do_not_enable_anything(tmp_path):
    path = tmp_path / "pc.json"
    path.write_text(json.dumps({"enabled": "yes", "roots": "C:/"}), encoding="utf-8")
    cfg = PcSettingsStore(path, home=tmp_path).load()
    assert cfg.enabled is False  # only a real boolean true turns it on
    assert isinstance(cfg.roots, list)


def test_settings_dataclass_is_json_ready(tmp_path):
    cfg = PcSettingsStore(tmp_path / "pc.json", home=tmp_path).load()
    assert isinstance(cfg, PcSettings)
    json.dumps(cfg.to_dict())


# --- audit log --------------------------------------------------------------


def test_audit_records_and_returns_newest_first(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("pc_open_app", {"name": "notepad"}, "ok", confirmed=False)
    log.record("pc_delete_files", {"paths": ["a"]}, "declined", confirmed=True)
    rows = log.tail(10)
    assert [r["tool"] for r in rows] == ["pc_delete_files", "pc_open_app"]
    assert rows[0]["outcome"] == "declined" and rows[0]["confirmed"] is True
    assert rows[1]["input"] == {"name": "notepad"}
    assert "ts" in rows[0]


def test_audit_truncates_huge_inputs(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("pc_open_app", {"name": "x" * 10_000}, "ok")
    assert len(json.dumps(log.tail(1)[0]["input"])) < 2_000


def test_audit_tail_limit_and_missing_file(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    assert log.tail(5) == []
    for i in range(7):
        log.record("t", {"i": i}, "ok")
    assert [r["input"]["i"] for r in log.tail(3)] == [6, 5, 4]


def test_audit_skips_corrupt_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("t", {}, "ok")
    with path.open("a", encoding="utf-8") as f:
        f.write("garbage\n")
    log.record("t2", {}, "ok")
    assert [r["tool"] for r in log.tail(10)] == ["t2", "t"]


def test_audit_rotates_when_large(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=500)
    for i in range(40):
        log.record("t", {"i": i}, "ok")
    assert path.with_suffix(".jsonl.1").exists()
    assert log.tail(1)[0]["input"]["i"] == 39


def test_audit_never_raises_on_unwritable_path(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    AuditLog(blocker / "sub" / "audit.jsonl").record("t", {}, "ok")  # must not raise


# --- per-call risk ----------------------------------------------------------


def test_tool_risk_defaults_to_static_value():
    tool = Tool("t", "d", {"type": "object", "properties": {}}, lambda: "x", risk="confirm")
    assert tool.risk_for({}) == "confirm"


def test_tool_risk_fn_decides_per_call():
    tool = Tool("t", "d", {}, lambda **k: "x", risk_fn=lambda i: "confirm" if i.get("danger") else "safe")
    assert tool.risk_for({"danger": True}) == "confirm"
    assert tool.risk_for({}) == "safe"


def test_tool_risk_fn_failure_means_confirm():
    def boom(_):
        raise RuntimeError("bad input")

    tool = Tool("t", "d", {}, lambda **k: "x", risk_fn=boom)
    assert tool.risk_for({}) == "confirm"


def test_registry_decorator_accepts_risk_fn_and_audited():
    reg = ToolRegistry()

    @reg.tool("t", "d", {"a": {"type": "string"}}, risk_fn=lambda i: "confirm", audited=True)
    def t(a):
        return a

    tool = reg.get("t")
    assert tool.risk_for({"a": "x"}) == "confirm" and tool.audited is True


# --- agent: approval + audit ------------------------------------------------


def _jarvis(tmp_path, approve):
    return Jarvis(Settings(data_dir=tmp_path), store=MemoryStore(), client=object(), approve=approve)


def _call(name, tool_input, id="u1"):
    return NS(type="tool_use", id=id, name=name, input=tool_input)


def _register(j, name, handler, risk_fn):
    j.registry.register(Tool(name, "d", {"type": "object", "properties": {}, "required": []}, handler, risk_fn=risk_fn, audited=True))


def test_safe_call_runs_without_asking_and_is_audited(tmp_path):
    asked = []
    j = _jarvis(tmp_path, lambda n, i: asked.append(n) or True)
    _register(j, "pc_x", lambda: "done", lambda i: "safe")
    out = j._run_tools([_call("pc_x", {})], [])
    assert out[0]["content"] == "done" and asked == []
    row = j.pc_audit.tail(1)[0]
    assert (row["tool"], row["outcome"], row["confirmed"]) == ("pc_x", "ok", False)


def test_confirm_call_asks_then_runs_when_approved(tmp_path):
    asked = []
    j = _jarvis(tmp_path, lambda n, i: asked.append((n, i)) or True)
    _register(j, "pc_x", lambda: "done", lambda i: "confirm")
    out = j._run_tools([_call("pc_x", {})], [])
    assert out[0]["content"] == "done" and asked == [("pc_x", {})]
    assert j.pc_audit.tail(1)[0]["confirmed"] is True


def test_declined_call_does_not_run_and_is_audited(tmp_path):
    ran = []
    j = _jarvis(tmp_path, lambda n, i: False)
    _register(j, "pc_x", lambda: ran.append(1) or "done", lambda i: "confirm")
    out = j._run_tools([_call("pc_x", {})], [])
    assert out[0]["is_error"] is True and ran == []
    assert j.pc_audit.tail(1)[0]["outcome"] == "declined"


def test_failed_call_is_audited_as_error(tmp_path):
    def bad():
        raise ValueError("nope")

    j = _jarvis(tmp_path, lambda n, i: True)
    _register(j, "pc_x", bad, lambda i: "safe")
    j._run_tools([_call("pc_x", {})], [])
    assert j.pc_audit.tail(1)[0]["outcome"] == "error"


def test_unaudited_tools_leave_no_pc_log(tmp_path):
    j = _jarvis(tmp_path, lambda n, i: True)
    j._run_tools([_call("calculate", {"expression": "1+1"})], [])
    assert j.pc_audit.tail(5) == []
