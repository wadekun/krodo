"""Tests for diff_preview (M4 PR3)."""

from __future__ import annotations

from rich.syntax import Syntax

from coda.cli.diff_preview import _MAX_DIFF_LINES, render_diff, render_new_file


class TestRenderDiff:
    def test_returns_syntax_object(self) -> None:
        result = render_diff("old line\n", "new line\n", "/tmp/test.py")
        assert isinstance(result, Syntax)

    def test_no_old_is_new_file(self) -> None:
        result = render_diff(None, "hello\n", "/tmp/hello.py")
        assert isinstance(result, Syntax)
        assert "/dev/null" in result.code

    def test_identical_content_no_changes_msg(self) -> None:
        result = render_diff("same\n", "same\n", "/tmp/x.py")
        assert "(no changes)" in result.code

    def test_diff_shows_removed_and_added(self) -> None:
        result = render_diff("old content\n", "new content\n", "/tmp/f.py")
        # The diff should contain +/- markers
        assert "-old content" in result.code or "+new content" in result.code

    def test_path_in_diff_header(self) -> None:
        result = render_diff("a\n", "b\n", "/abs/path/file.py")
        assert "/abs/path/file.py" in result.code

    def test_truncation_applied_to_large_diff(self) -> None:
        old = "".join(f"line {i}\n" for i in range(_MAX_DIFF_LINES + 10))
        new = "".join(f"modified {i}\n" for i in range(_MAX_DIFF_LINES + 10))
        result = render_diff(old, new, "/tmp/big.py")
        assert "truncated" in result.code

    def test_no_truncation_for_small_diff(self) -> None:
        result = render_diff("a\n", "b\n", "/tmp/small.py")
        assert "truncated" not in result.code

    def test_crlf_normalised(self) -> None:
        result = render_diff("line1\r\nline2\r\n", "line1\r\nline3\r\n", "/tmp/win.txt")
        assert isinstance(result, Syntax)
        # Should not raise or produce garbled output
        assert "line3" in result.code or "line2" in result.code

    def test_empty_old(self) -> None:
        result = render_diff("", "new content\n", "/tmp/empty.py")
        assert isinstance(result, Syntax)

    def test_empty_new(self) -> None:
        result = render_diff("old content\n", "", "/tmp/delete.py")
        assert isinstance(result, Syntax)


class TestRenderNewFile:
    def test_returns_syntax(self) -> None:
        result = render_new_file("content\n", "/tmp/new.py")
        assert isinstance(result, Syntax)

    def test_dev_null_header_present(self) -> None:
        result = render_new_file("content\n", "/tmp/new.py")
        assert "/dev/null" in result.code

    def test_path_present(self) -> None:
        result = render_new_file("content\n", "/abs/path/new.py")
        assert "/abs/path/new.py" in result.code
