"""Terminal front end: interactive loop wiring (no network)."""

from types import SimpleNamespace as NS

import jarvis.cli as cli


def test_interactive_cli_prints_the_answer_before_compacting(monkeypatch, capsys, tmp_path):
    order = []

    class StubJarvis:
        def ask(self, text):
            order.append("ask")
            return NS(text="긴 답", stop_reason="end_turn")

        def compact_if_long(self):
            order.append("compact")
            return "compacted a 90,000-token conversation"

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_make_jarvis", lambda settings, **kwargs: StubJarvis())
    lines = iter(["아까 얘기 계속", "/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    assert cli.main([]) == 0
    out = capsys.readouterr()
    assert order == ["ask", "compact"] and "긴 답" in out.out and "compacted a 90,000-token" in out.err
