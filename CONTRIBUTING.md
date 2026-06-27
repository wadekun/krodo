# Contributing to Krodo

Thanks for considering a contribution! Krodo is pre-alpha — the bar for
merge is "moves us toward Phase 1 exit" or "fixes a real bug"; pure
style refactors are likely to be deferred.

## 1. Development environment

```bash
git clone https://github.com/liangck/krodo
cd krodo
uv sync                            # installs runtime + dev deps
```

Krodo targets **Python 3.12+** strictly. Modern syntax (`list[int]`,
`int | None`, `match`, `@override`) is required; `from __future__ import
annotations` is already in every module.

## 2. The CI gate (must pass before merge)

CI runs on every push and PR ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
The full gate runs on Ubuntu and macOS. **All four checks must be green:**

```bash
uv run ruff check .                # lint (E/F/I/N/W/UP/B/S/ASYNC/T20)
uv run ruff format --check .       # format
uv run mypy src                    # type check (--strict, zero errors)
uv run pytest                      # tests + coverage
```

Run them locally before pushing. To auto-fix what's auto-fixable:

```bash
uv run ruff check . --fix
uv run ruff format .
```

Coverage target: **≥ 90%** for `krodo.core` / `krodo.llm`; **100%** for
`krodo.tools` / `krodo.sandbox` (per `pyproject.toml` §`coverage.report`).

### Type-checking rules

- `mypy --strict` is non-negotiable. No `# type: ignore` without an
  inline reason next to it (e.g. `# type: ignore[foo]  # pydantic v2 quirks`).
- Pydantic v2 / typing.Protocol are the patterns; no `Any` outside
  interop boundaries (LiteLLM, tests).

### Lint rules to know

- **No `print()` in `src/`** (rule `T20`). Use `structlog` via
  `krodo.obs.logger`.
- **No `subprocess.run(shell=True)`** in production code (rule `S602`).
  Route shell through `krodo.sandbox.firewall.LocalSandboxRunner` which
  enforces the dangerous-command policy.
- **No bare `except`** (rule `E722`); use `except Exception` with a
  `# noqa: BLE001` if you genuinely need to swallow everything.

## 3. Code conventions

The seven engineering principles ([`docs/architecture.md`](docs/architecture.md) §11)
are the source of truth. The short version:

1. **Protocol first** — every core module begins as a `typing.Protocol`
   in `src/krodo/<module>/protocols.py`. Implementations come second.
2. **Safe by default** — deny unless explicitly allowed; dangerous ops
   always prompt; sensitive files always ignored.
3. **Day 1 observability** — every tool call gets a trace span; every
   LLM call records tokens + cost.
4. **Git is the safety net** — checkpoint with `git stash create` before
   every write; `krodo undo` restores.
5. **Extract shared abstractions only after a pattern repeats across
   three modules.** Don't pre-abstract.

### Module dependency rules

Enforced by `import-linter` (planned for Phase 2 CI):

```
core  →  {llm, tools, sandbox, memory, obs}    never the reverse
tools →  {sandbox, memory, obs}                 never on core
cli   →  core facade only                       never constructs messages or calls LLMs directly
```

### Adding a new tool

1. Define a `pydantic.BaseModel` for arguments in
   `src/krodo/tools/builtin/<area>.py`.
2. Implement `Tool` Protocol; return `ToolResult`, never raise.
3. Mark `requires_approval` correctly (read-only default, write/exec = `True`).
4. Register in `src/krodo/tools/registry.py`.
5. Add unit tests in `tests/unit/tools/test_<tool>.py`.
6. Update `AGENTS.md` "M2 tools" section if the tool is user-visible.

## 4. Commit message conventions

We follow the existing pattern (see `git log --oneline`):

```
<type>(<scope>): <subject>

<body — why, not what>
```

- **`type`**: `feat` / `fix` / `docs` / `style` / `refactor` / `test` / `chore`
- **`scope`**: milestone or module — `M7`, `rename`, `loop`, `mypy`, `repl`, etc.
- **subject**: lowercase imperative, ≤ 70 chars, no trailing period
- **body**: wrapped at ~72 chars, explain motivation and trade-offs

Examples from this repo:

```
feat(M6.4): REPL slash commands — :help/:sessions/:undo/:cost/:resume
fix(loop): synthesize tool_results for interrupted batches + REPL continue-on-limit
feat(rename): Coda → krodo full rename
docs(M6): AGENTS.md M6 section, README pipe/slash/cost, architecture changelog
```

Every commit should pass `ruff` + `mypy` + `pytest` on its own (so a
`git bisect` works). If you need multiple commits to land a green test
suite, mark intermediate commits with `[wip]` in the subject and
squash-merge the PR.

## 5. Pull request flow

1. **Open an issue first** for any non-trivial change (>50 LOC or any
   public API change). Quick bug fixes can go straight to PR.
2. Branch from `main`: `feat/<short-desc>` or `fix/<issue-number>-<desc>`.
3. Make commits per §4. Keep branches short-lived (< 2 weeks).
4. Open a PR; the description should answer:
   - What problem does this solve?
   - What's the user-visible change?
   - What's the test plan? (which tests added, what manual smoke test)
5. CI must be green before review.
6. **Squash-merge** is the default; one commit per PR.
7. Delete the branch after merge.

## 6. Reviewing a PR (for maintainers)

Checklist:

- [ ] CI green (all four checks, both OSes)
- [ ] `AGENTS.md` updated if a new tool / approval mode / CLI flag is added
- [ ] `docs/architecture.md` updated if a subsystem contract changes
- [ ] `CHANGELOG.md` `[Unreleased]` section has an entry
- [ ] No new `Any` / `type: ignore` without justification
- [ ] No `print()` introduced in `src/`
- [ ] No `subprocess.run(shell=True)` introduced
- [ ] Tests cover the new behaviour (look for assertion-free tests)

## 7. Releasing

Release process is being defined in M7. The short version:

1. Bump `version` in `pyproject.toml`.
2. Move `[Unreleased]` → `[x.y.z] — YYYY-MM-DD` in `CHANGELOG.md`.
3. Commit as `chore(release): vx.y.z`.
4. `git tag vx.y.z && git push --tags`.
5. `gh release create vx.y.z` with the CHANGELOG section as body.

PyPI publication (trusted-publishers via GitHub Actions) is **deferred
past v0.1.0**; first releases are GitHub Release + `uv tool install
git+https://github.com/liangck/krodo`.

## 8. Conduct & licensing

- Be excellent to each other. Discussions stay technical and respectful.
- Contributions land under the project's [Apache-2.0](LICENSE) license.
  You retain copyright; you grant a perpetual, worldwide, no-charge,
  royalty-free license to the project.
