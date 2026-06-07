"""Unit tests for resume conversation-history display (src/coda/cli/resume.py).

Covers:
- Empty "asst" lines are replaced with [called <tool> <arg>] summaries.
- Path arguments are rendered relative to the workspace root.
- Consecutive tool-call runs are folded after 5 lines.
- Mixed messages (narration + tool calls) emit BOTH a text line and a tool line.
- Display window is anchored on user turns.
"""

from __future__ import annotations

from pathlib import Path

from coda.cli.resume import _history_entries, _key_arg, _print_conversation_history
from coda.core.types import Message, ToolCall


def _user(content: str) -> Message:
    return Message(role="user", content=content)


def _asst(content: str = "", tool_calls: list[ToolCall] | None = None) -> Message:
    return Message(role="assistant", content=content, tool_calls=tool_calls)


def _tc(name: str, arguments: dict | None = None, tc_id: str = "x") -> ToolCall:
    return ToolCall(id=tc_id, name=name, arguments=arguments or {})


# ---------------------------------------------------------------------------
# _history_entries
# ---------------------------------------------------------------------------


class TestHistoryEntries:
    def test_user_line(self) -> None:
        entries = _history_entries(_user("hello there"), 120)
        assert len(entries) == 1
        kind, line, weight = entries[0]
        assert kind == "text"
        assert "you" in line
        assert "hello there" in line
        assert weight == 0

    def test_assistant_text_line(self) -> None:
        entries = _history_entries(_asst("the answer is 42"), 120)
        assert len(entries) == 1
        kind, line, weight = entries[0]
        assert kind == "text"
        assert "asst" in line
        assert "the answer is 42" in line

    def test_assistant_text_truncated(self) -> None:
        long = "x" * 300
        entries = _history_entries(_asst(long), 120)
        assert len(entries) == 1
        _, line, _ = entries[0]
        assert line.endswith("...[/dim]")

    def test_tool_call_message_summarised_without_root(self) -> None:
        """Empty content + tool_calls => one tool entry, never blank."""
        msg = _asst(
            content="",
            tool_calls=[
                ToolCall(id="1", name="read_file", arguments={"path": "/abs/game.js"}),
                ToolCall(id="2", name="grep", arguments={}),
            ],
        )
        entries = _history_entries(msg, 120)
        assert len(entries) == 1
        kind, line, weight = entries[0]
        assert kind == "tool"
        assert "read_file /abs/game.js" in line
        assert "grep" in line
        assert weight == 2

    def test_mixed_message_yields_two_entries(self) -> None:
        """A message with both content and tool_calls emits text THEN tool."""
        msg = _asst(
            content="我会帮你创建游戏文件。",
            tool_calls=[ToolCall(id="1", name="write_file", arguments={"path": "game.js"})],
        )
        entries = _history_entries(msg, 120)
        assert len(entries) == 2
        kinds = [k for k, _, _ in entries]
        assert kinds == ["text", "tool"]
        _, text_line, _ = entries[0]
        assert "我会帮你创建游戏文件" in text_line
        _, tool_line, weight = entries[1]
        assert "write_file game.js" in tool_line
        assert weight == 1

    def test_tool_call_path_relative_to_workspace(self) -> None:
        root = Path("/private/tmp/coda-sandbox")
        msg = _asst(
            content="",
            tool_calls=[
                ToolCall(
                    id="1",
                    name="read_file",
                    arguments={"path": "/private/tmp/coda-sandbox/game.js"},
                ),
            ],
        )
        entries = _history_entries(msg, 120, workspace_root=root)
        assert len(entries) == 1
        _, line, _ = entries[0]
        assert "read_file game.js" in line
        assert "/private" not in line

    def test_tool_call_path_outside_workspace_kept(self) -> None:
        root = Path("/private/tmp/coda-sandbox")
        msg = _asst(
            content="",
            tool_calls=[ToolCall(id="1", name="read_file", arguments={"path": "/etc/hosts"})],
        )
        entries = _history_entries(msg, 120, workspace_root=root)
        _, line, _ = entries[0]
        assert "/etc/hosts" in line

    def test_tool_call_pattern_arg_shown(self) -> None:
        msg = _asst(
            content="",
            tool_calls=[ToolCall(id="1", name="grep", arguments={"pattern": "playSound"})],
        )
        entries = _history_entries(msg, 120)
        _, line, _ = entries[0]
        assert "grep playSound" in line

    def test_empty_assistant_without_tool_calls_yields_nothing(self) -> None:
        assert _history_entries(_asst(""), 120) == []


# ---------------------------------------------------------------------------
# _key_arg
# ---------------------------------------------------------------------------


class TestKeyArg:
    def test_path_key_relative(self) -> None:
        root = Path("/ws")
        assert _key_arg({"path": "/ws/src/main.py"}, root) == "src/main.py"

    def test_path_key_outside_workspace(self) -> None:
        root = Path("/ws")
        result = _key_arg({"path": "/etc/hosts"}, root)
        assert result == "/etc/hosts"

    def test_pattern_key(self) -> None:
        assert _key_arg({"pattern": "TODO"}, None) == "TODO"

    def test_long_value_truncated(self) -> None:
        val = "a" * 60
        result = _key_arg({"pattern": val}, None)
        assert result.endswith("...")
        assert len(result) == 40

    def test_empty_dict(self) -> None:
        assert _key_arg({}, None) == ""

    def test_none_arguments(self) -> None:
        assert _key_arg(None, None) == ""

    def test_fallback_first_string_value(self) -> None:
        # No known key — fall back to first string value.
        result = _key_arg({"widget": "foobar"}, None)
        assert result == "foobar"


# ---------------------------------------------------------------------------
# _print_conversation_history
# ---------------------------------------------------------------------------


class TestPrintConversationHistory:
    def test_no_blank_assistant_lines(self, capsys) -> None:
        history = [
            _user("do a thing"),
            _asst(content="", tool_calls=[_tc("bash")]),
            _asst("done"),
        ]
        _print_conversation_history(history)
        err = capsys.readouterr().err
        assert "called bash" in err
        for raw in err.splitlines():
            stripped = raw.strip()
            if "asst" in stripped:
                assert stripped.rstrip() not in ("asst", "asst ")

    def test_window_keeps_recent_user_turns(self, capsys) -> None:
        history: list[Message] = []
        for i in range(5):
            history.append(_user(f"USERTURN{i}"))
            history.append(_asst(f"reply {i}"))
        _print_conversation_history(history)
        err = capsys.readouterr().err
        assert "USERTURN0" not in err
        assert "USERTURN1" not in err
        assert "USERTURN2" in err
        assert "USERTURN4" in err

    def test_empty_history_prints_nothing(self, capsys) -> None:
        _print_conversation_history([])
        assert capsys.readouterr().err == ""

    def test_tool_run_folded_after_five(self, capsys) -> None:
        """8 consecutive tool-call messages -> 5 lines + '... +3 more tool calls'."""
        history = [_user("go")]
        for i in range(8):
            history.append(_asst(content="", tool_calls=[_tc(f"tool{i}", tc_id=str(i))]))
        _print_conversation_history(history)
        err = capsys.readouterr().err
        # first 5 appear
        for i in range(5):
            assert f"tool{i}" in err
        # tools 5-7 are folded
        assert "tool5" not in err
        assert "tool6" not in err
        assert "tool7" not in err
        assert "+3 more tool calls" in err

    def test_tool_run_within_cap_not_folded(self, capsys) -> None:
        history = [_user("go")]
        for i in range(4):
            history.append(_asst(content="", tool_calls=[_tc(f"t{i}", tc_id=str(i))]))
        _print_conversation_history(history)
        err = capsys.readouterr().err
        for i in range(4):
            assert f"t{i}" in err
        assert "more tool calls" not in err

    def test_path_shown_relative_to_workspace(self, capsys, tmp_path) -> None:
        sub = tmp_path / "game.js"
        history = [
            _user("edit"),
            _asst(
                content="",
                tool_calls=[_tc("edit_file", {"path": str(sub)})],
            ),
        ]
        _print_conversation_history(history, workspace_root=tmp_path)
        err = capsys.readouterr().err
        assert "game.js" in err
        assert str(tmp_path) not in err

    def test_text_reply_after_tool_run_shown(self, capsys) -> None:
        """A text reply following a tool run must not be folded."""
        history = [
            _user("do"),
            _asst(content="", tool_calls=[_tc("bash")]),
            _asst("All done!"),
        ]
        _print_conversation_history(history)
        err = capsys.readouterr().err
        assert "All done!" in err

    def test_mixed_message_shows_both_narration_and_tool(self, capsys) -> None:
        """Message with content + tool_calls must show narration then [called X]."""
        history = [
            _user("build it"),
            _asst(
                content="我会帮你创建游戏文件。",
                tool_calls=[_tc("write_file", {"path": "game.js"})],
            ),
        ]
        _print_conversation_history(history)
        err = capsys.readouterr().err
        assert "我会帮你创建游戏文件" in err
        assert "write_file game.js" in err
        # narration must appear before the tool summary
        assert err.index("我会帮你创建游戏文件") < err.index("write_file")
