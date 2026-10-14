"""Agent loop tests against a scripted fake client (no network)."""

from types import SimpleNamespace as NS

import pytest

from jarvis.agent import COMPACT_BETA, FALLBACK_BETA, Jarvis
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
    def __init__(self, script, compact=None):
        self.script = list(script)
        self.calls = []
        self.compactions = []
        self.compact = compact  # the reply (or exception) for a compaction request
        self.beta = NS(messages=NS(stream=self._stream, create=self._create))

    def _stream(self, **params):
        # Snapshot messages: the agent keeps appending to the same list.
        self.calls.append({**params, "messages": list(params["messages"])})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeStream(item)

    def _create(self, **params):
        self.compactions.append({**params, "messages": list(params["messages"])})
        reply = self.compact.pop(0) if isinstance(self.compact, list) else self.compact
        if isinstance(reply, BaseException):
            raise reply
        return reply


def make(script, approve=lambda n, i: False, compact=None, **settings):
    client = FakeClient(script, compact)
    store = MemoryStore()
    agent = Jarvis(Settings(**settings), store=store, client=client, approve=approve)
    return agent, client, store


def msg(content, stop_reason, usage=None):
    return NS(content=content, stop_reason=stop_reason, stop_details=None, usage=usage)


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


WRITE_TOOLS = {"remember", "forget", "update_profile", "add_goal", "save_plan", "update_status",
               "add_schedule", "update_schedule", "run_python"}


def test_readonly_turn_hides_and_refuses_write_tools():
    # Unattended runs read untrusted text (feeds, search results): no tool that changes anything.
    agent, client, store = make(
        [msg([tool_use("t1", "forget", {"memory_id": 1})], "tool_use"), msg([text("ok")], "end_turn")]
    )
    store.remember("fact", "중요한 기억", 5)
    agent.ask("브리핑해줘", readonly=True)
    offered = {t["name"] for t in client.calls[0]["tools"]}
    assert {"recall", "search_memos", "list_goals", "list_schedule", "calculate", "web_search"} <= offered
    assert not offered & WRITE_TOOLS
    refused = client.calls[1]["messages"][-1]["content"][0]
    assert refused["is_error"] and store.recall("중요한")  # the memory survived


def test_normal_turn_keeps_write_tools():
    agent, client, _ = make([msg([text("ok")], "end_turn")])
    agent.ask("목요일 3시 치과")
    assert WRITE_TOOLS <= {t["name"] for t in client.calls[0]["tools"]}


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


def test_text_is_streamed_to_on_event():
    class StreamingFake(FakeStream):
        text_stream = ("안녕", "하세요")

    seen = []
    client = NS(beta=NS(messages=NS(stream=lambda **p: StreamingFake(msg([text("안녕하세요")], "end_turn")))))
    agent = Jarvis(Settings(), store=MemoryStore(), client=client, on_event=lambda k, d: seen.append((k, d)))
    assert agent.ask("hi").text == "안녕하세요"
    assert [d["text"] for k, d in seen if k == "text"] == ["안녕", "하세요"]


SUMMARY = NS(type="compaction", content="요약: 사용자와 JARVIS 확장 계획을 논의함", signature="sig")
COMPACTED = NS(stop_reason="compaction", content=[SUMMARY])


def used(context_tokens):
    """Usage of a response whose request read `context_tokens` (mostly from the prompt cache)."""
    return NS(input_tokens=1_000, cache_read_input_tokens=context_tokens - 1_500,
              cache_creation_input_tokens=0, output_tokens=500)


def test_long_conversation_is_compacted_in_place_once_the_answer_is_out():
    agent, client, _ = make(
        [msg([text("긴 답")], "end_turn", used(90_000)), msg([text("이어서")], "end_turn")], compact=COMPACTED
    )
    history = agent.messages  # the web app keeps one list per chat and reuses it
    assert agent.ask("계속").text == "긴 답" and client.compactions == []  # the answer is not held back
    assert agent.compact_if_long() == "compacted a 90,000-token conversation"
    request, turn = client.compactions[0], client.calls[0]
    assert request["compaction"]["type"] == "summarize" and request["betas"] == [COMPACT_BETA]
    assert "fallbacks" not in request and "format" not in request["output_config"]
    for same in ("model", "system", "tools", "thinking", "output_config", "cache_control"):  # keeps the prompt cache
        assert request[same] == turn[same], same
    assert history is agent.messages and agent.messages == [{"role": "assistant", "content": [SUMMARY]}]
    agent.ask("다음 질문")
    after = client.calls[1]
    assert COMPACT_BETA in after["betas"] and after["messages"][0]["content"] == [SUMMARY]
    assert [m["role"] for m in after["messages"]] == ["assistant", "user"]


def test_short_conversation_is_not_compacted():
    agent, client, _ = make([msg([text("ok")], "end_turn", used(20_000))], compact=COMPACTED)
    agent.ask("hi")
    assert agent.compact_if_long() is None
    assert client.compactions == [] and COMPACT_BETA not in client.calls[0]["betas"]


@pytest.mark.parametrize("reply", [RuntimeError("overloaded"), NS(stop_reason="refusal", content=[])])
def test_failed_compaction_keeps_the_conversation_and_waits_before_retrying(reply):
    agent, client, _ = make(
        [msg([text("긴 답")], "end_turn", used(90_000)), msg([text("또")], "end_turn", used(100_000)),
         msg([text("또또")], "end_turn", used(112_000))],
        compact=reply,
    )
    agent.ask("계속")
    assert agent.compact_if_long().startswith("compaction failed") and len(agent.messages) == 2
    agent.ask("계속")
    assert agent.compact_if_long() is None and len(client.compactions) == 1  # not again 10k tokens later
    agent.ask("계속")
    assert agent.compact_if_long().startswith("compaction failed") and len(client.compactions) == 2


def test_compaction_can_be_turned_off():
    agent, client, _ = make([msg([text("ok")], "end_turn", used(500_000))], compact=COMPACTED, compact_at_tokens=0)
    agent.ask("hi")
    assert agent.compact_if_long() is None and client.compactions == []


def test_a_successful_compaction_clears_the_wait_left_by_a_failed_one():
    turns = [msg([text(str(t))], "end_turn", used(t)) for t in (90_000, 110_000, 95_000)]
    agent, client, _ = make(turns, compact=[RuntimeError("overloaded"), COMPACTED, COMPACTED])
    agent.ask("1")
    assert agent.compact_if_long().startswith("compaction failed")  # retry waits until 110,000 tokens
    agent.ask("2")
    assert agent.compact_if_long().startswith("compacted")
    agent.ask("3")  # the summarized conversation grew past the limit again
    assert agent.compact_if_long().startswith("compacted") and len(client.compactions) == 3
