"""Agent loop tests against a scripted fake client (no network)."""

from types import SimpleNamespace as NS

import pytest

from jarvis.agent import FALLBACK_BETA, Jarvis
from jarvis.config import Settings
from jarvis.memory import MemoryStore


def text(t):
    return NS(type="text", text=t)


def tool_use(id_, name, input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **params):
        # Snapshot messages: the agent keeps appending to the same list.
        self.calls.append({**params, "messages": list(params["messages"])})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeStream(item)


def make(script, approve=lambda n, i: False, **settings):
    client = FakeClient(script)
    store = MemoryStore()
    agent = Jarvis(Settings(**settings), store=store, client=client, approve=approve)
    return agent, client, store


def msg(content, stop_reason):
    return NS(content=content, stop_reason=stop_reason, stop_details=None)


def test_plain_answer_and_request_shape():
    agent, client, _ = make([msg([text("안녕하세요")], "end_turn")])
    result = agent.ask("hi")
    assert result.text == "안녕하세요" and result.steps == 1
    params = client.calls[0]
    assert params["model"] == "claude-opus-5-5"
    assert params["thinking"]["type"] == "adaptive"
    assert params["fallbacks"] == "default" and FALLBACK_BETA in params["betas"]
    first_user = params["messages"][0]["content"]
    assert first_user[0]["text"].startswith("<situation>") and first_user[1]["text"] == "hi"
    assert any(t.get("name") == "web_search" for t in params["tools"])


def test_tool_loop_writes_memory_and_returns_results_in_one_message():
    agent, client, store = make(
        [
            msg(
                [
                    tool_use("t1", "remember", {"kind": "weakness", "content": "회계 지식 부족", "importance": 4}),
                    tool_use("t2", "calculate", {"expression": "1200 * 12"}),
                ],
                "tool_use",
            ),
            msg([text("기억했고, 연 14400입니다.")], "end_turn"),
        ]
    )
    result = agent.ask("나 회계 약해. 월 1200이면 연간 얼마?")
    assert result.tool_calls == ["remember", "calculate"]
    assert store.recall("회계")[0].kind == "weakness"
    tool_msg = client.calls[1]["messages"][-1]
    assert tool_msg["role"] == "user"
    assert [r["tool_use_id"] for r in tool_msg["content"]] == ["t1", "t2"]
    assert tool_msg["content"][1]["content"] == "14400"


def test_situation_includes_weakness_on_next_turn():
    agent, client, store = make([msg([text("a")], "end_turn"), msg([text("b")], "end_turn")])
    agent.ask("1")
    store.remember("weakness", "회계 지식 부족", 4)
    agent.ask("2")
    situation = client.calls[1]["messages"][-1]["content"][0]["text"]
    assert "회계 지식 부족" in situation


def test_confirm_tool_requires_approval():
    seen = []
    agent, client, _ = make(
        [
            msg([tool_use("t1", "run_python", {"code": "print(1)"})], "tool_use"),
            msg([text("ok")], "end_turn"),
        ],
        approve=lambda name, inp: seen.append(name) or False,
    )
    agent.ask("run it")
    result = client.calls[1]["messages"][-1]["content"][0]
    assert seen == ["run_python"]
    assert result["is_error"] and "declined" in result["content"]


def test_pause_turn_resends_and_step_limit():
    agent, client, _ = make(
        [msg([text("searching")], "pause_turn"), msg([text("done")], "end_turn")]
    )
    assert agent.ask("x").text == "done" and len(client.calls) == 2

    looping = [msg([tool_use(f"t{i}", "calculate", {"expression": "1+1"})], "tool_use") for i in range(3)]
    agent, _, _ = make(looping, max_steps=3)
    result = agent.ask("loop")
    assert result.stop_reason == "max_steps"


def test_refusal_is_reported():
    m = NS(content=[], stop_reason="refusal", stop_details=NS(explanation="policy"))
    agent, _, _ = make([m])
    result = agent.ask("x")
    assert result.stop_reason == "refusal" and "policy" in result.text


def test_failed_turn_is_rolled_back():
    agent, _, _ = make([msg([tool_use("t1", "calculate", {"expression": "1"})], "tool_use"), RuntimeError("net")])
    with pytest.raises(RuntimeError):
        agent.ask("x")
    assert agent.messages == []
