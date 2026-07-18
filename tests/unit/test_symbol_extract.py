"""Tests for krodo.indexer.extract — single-file symbol extraction.

Covers the four first-batch languages (Python/JS/TS/Go), the backend/precision
defaults, signature multi-line truncation (review B), and the robustness rules
(1MB cap, binary skip, no-grammar, error tolerance — review L).
"""

from __future__ import annotations

from krodo.indexer.extract import (
    extract_symbols,
    grammar_for_path,
    supported_extensions,
)


def _names(extraction: object, *, refs: bool = False) -> list[str]:
    """Pull names out of a FileExtraction (defs by default)."""
    field = "refs" if refs else "defs"
    return [s.name for s in getattr(extraction, field)]


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def test_python_definitions_and_references() -> None:
    src = b"""\
PI = 3.14
class Foo:
    def bar(self, x):
        return self.baz(x)
    def baz(self, y):
        return y + 1
def use(f: Foo) -> int:
    return f.bar(1)
use(Foo())
"""
    ext = extract_symbols("pkg/mod.py", src)
    defs = {d.name: d for d in ext.defs}
    assert set(defs) == {"PI", "Foo", "bar", "baz", "use"}
    assert defs["Foo"].kind == "class"
    assert defs["bar"].kind == "function"
    assert defs["PI"].kind == "constant"
    assert defs["Foo"].line == 2
    assert defs["bar"].line == 3
    ref_names = set(_names(ext, refs=True))
    # call sites: use(...), Foo(), self.baz(...), f.bar(...)
    assert {"use", "Foo", "baz", "bar"} <= ref_names


def test_python_defaults_backend_and_precision() -> None:
    ext = extract_symbols("m.py", b"def f():\n    pass\nf()\n")
    d = ext.defs[0]
    assert d.backend == "treesitter"
    assert d.precision == "syntactic"
    assert ext.refs[0].backend == "treesitter"
    assert ext.refs[0].precision == "syntactic"


def test_python_signature_single_line() -> None:
    ext = extract_symbols("m.py", b"def alpha():\n    return 1\n")
    assert ext.defs[0].signature == "def alpha():"


def test_python_signature_multiline_truncated() -> None:
    src = b"def alpha(x: int,\n        y: str) -> str:\n    return f'{x}{y}'\n"
    ext = extract_symbols("m.py", src)
    sig = ext.defs[0].signature
    assert sig.startswith("def alpha(x: int,")
    assert sig.endswith("…")


def test_python_nested_method_signature_stripped() -> None:
    """A method inside a class must not carry leading indent in its signature."""
    ext = extract_symbols("m.py", b"class C:\n    def m(self): pass\n")
    m = next(d for d in ext.defs if d.name == "m")
    assert m.signature == "def m(self): pass"


# ---------------------------------------------------------------------------
# JavaScript / TypeScript / Go / TSX (smoke — review H: acceptance is Python-led)
# ---------------------------------------------------------------------------


def test_javascript_extraction() -> None:
    src = b"""\
function add(a, b) { return a + b; }
class Calc { mult(x, y) { return x * y; } }
const arrow = (n) => n + 1;
add(1, 2);
"""
    ext = extract_symbols("src/calc.js", src)
    assert {"add", "Calc", "mult", "arrow"} <= {d.name for d in ext.defs}
    assert "add" in _names(ext, refs=True)


def test_typescript_specific_constructs() -> None:
    src = b"""\
interface Animal { name: string; }
type ID = number;
enum Color { Red, Green }
function feed(a: Animal): void {}
feed(new Animal());
"""
    ext = extract_symbols("src/app.ts", src)
    kinds = {d.name: d.kind for d in ext.defs}
    assert kinds["Animal"] == "interface"
    assert kinds["ID"] == "type"
    assert kinds["Color"] == "enum"
    assert kinds["feed"] == "function"


def test_go_extraction() -> None:
    src = b"""\
package main
func add(a, b int) int { return a + b }
type S struct { x int }
func (s S) get() int { return s.x }
func run() { add(1, 2) }
"""
    ext = extract_symbols("cmd/main.go", src)
    kinds = {d.name: d.kind for d in ext.defs}
    assert kinds["main"] == "module"  # package_clause
    assert kinds["add"] == "function"
    assert kinds["get"] == "method"
    assert kinds["run"] == "function"
    assert kinds["S"] == "class"  # struct → class kind
    assert "add" in _names(ext, refs=True)


def test_tsx_uses_typescript_query() -> None:
    src = b"export const Card = (p: { x: number }) => <div>{p.x}</div>;\ninterface Box {}\n"
    ext = extract_symbols("src/Card.tsx", src)
    names = {d.name for d in ext.defs}
    assert "Card" in names
    assert "Box" in names


# ---------------------------------------------------------------------------
# Robustness (review L)
# ---------------------------------------------------------------------------


def test_unsupported_extension_returns_empty() -> None:
    ext = extract_symbols("README.md", b"# hi\ndef f(): pass\n")
    assert ext.defs == [] and ext.refs == []


def test_no_grammar_for_unknown_code_ext() -> None:
    ext = extract_symbols("src/lib.rs", b"fn main() {}\n")
    assert ext.defs == [] and ext.refs == []


def test_oversized_file_returns_empty() -> None:
    big = b"# x\n" + b"x = 1\n" * 300_000
    assert len(big) > 1_048_576
    ext = extract_symbols("big.py", big)
    assert ext.defs == [] and ext.refs == []


def test_binary_file_with_nul_returns_empty() -> None:
    ext = extract_symbols("bin.py", b"def f():\n    pass\n\x00\x00 garbage")
    assert ext.defs == [] and ext.refs == []


def test_invalid_utf8_does_not_crash() -> None:
    ext = extract_symbols("m.py", b"def f():\n    return '\xff'\n")
    assert ext.defs[0].name == "f"


def test_syntax_error_tolerated() -> None:
    ext = extract_symbols("broken.py", b"def good():\n    pass\ndef !!broken\n")
    assert "good" in [d.name for d in ext.defs]


# ---------------------------------------------------------------------------
# Helpers API
# ---------------------------------------------------------------------------


def test_grammar_for_path() -> None:
    assert grammar_for_path("a/b/c.py") == "python"
    assert grammar_for_path("App.tsx") == "tsx"
    assert grammar_for_path("mod.go") == "go"
    assert grammar_for_path("style.css") is None


def test_supported_extensions_contains_first_batch() -> None:
    exts = supported_extensions()
    for e in (".py", ".js", ".ts", ".tsx", ".go"):
        assert e in exts
