# Contributing to Krodo

Thanks for considering a contribution! Krodo is pre-alpha — the bar for
merge is "moves us toward Phase 1 exit" or "fixes a real bug"; pure
style refactors are likely to be deferred.

## 1. Development environment

```bash
git clone https://github.com/wadekun/krodo
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

Cutting a release involves 4 files. **All four must be in the same commit**, otherwise the lockfile goes stale and CI's `uv sync --frozen` will eventually catch it.

### Release checklist (cutting vX.Y.Z)

1. **Bump version** in `pyproject.toml`:
   ```bash
   # 例: 0.1.1 → 0.1.2
   sed -i '' 's/^version = "0.1.1"/version = "0.1.2"/' pyproject.toml
   ```

2. **Sync uv.lock to the new version** — uv doesn't auto-update the krodo self-reference in lockfile on every `uv sync`; do it explicitly:
   ```bash
   uv lock                      # re-resolves lockfile, picks up new self-version
   ```
   Verify with:
   ```bash
   grep -A1 '^name = "krodo"' uv.lock   # should show version = "0.1.2"
   ```

3. **Update CHANGELOG**: move `[Unreleased]` block to `[x.y.z] — YYYY-MM-DD`, and add a fresh empty `[Unreleased]` block at the top. Update the link references at the bottom of the file.

4. **Single commit with all three files**:
   ```bash
   git add pyproject.toml uv.lock CHANGELOG.md
   git commit -m "chore(release): vX.Y.Z"
   ```
   Never commit only `pyproject.toml` + `CHANGELOG.md` and leave `uv.lock` for later — that creates a stale-lockfile state that bites the next contributor / CI run.

5. **Tag and push**:
   ```bash
   git tag vX.Y.Z
   git push origin main
   git push --tags
   ```

6. **Create GitHub Release** — this auto-triggers the PyPI publish workflow:
   ```bash
   gh release create vX.Y.Z --notes-from-tag
   # 或手写 release notes(取 CHANGELOG 的 [x.y.z] 节内容)
   ```

7. **Verify publish**:
   ```bash
   gh run watch $(gh run list --workflow=publish.yml --limit 1 --json databaseId -q '.[0].databaseId')
   curl -s https://pypi.org/pypi/krodo/json | python3 -c "import sys, json; print(json.load(sys.stdin)['info']['version'])"
   ```

### What the publish workflow does

`.github/workflows/publish.yml` triggers on `release.published`. It:

1. Checks out the tagged commit (via `actions/checkout` with `ref: ${{ inputs.ref || github.event.release.tag_name }}`).
2. Runs the full CI gate (ruff / format / mypy / pytest) — never ships a broken build.
3. Builds wheel + sdist via `uv build`.
4. Publishes to PyPI via Trusted Publishers (OIDC) — no API tokens.

If `release.published` doesn't fire (e.g. you deleted + re-created a release with the same tag, GitHub suppresses the event), the workflow also has `workflow_dispatch` as a manual fallback:

```bash
gh workflow run publish.yml -f ref=vX.Y.Z
```

### What if I forgot `uv.lock` in the release commit?

If you pushed the release tag and notice later that uv.lock is stale in main:

1. **PyPI wheel is fine** — the build was driven by pyproject.toml; uv.lock staleness doesn't affect the wheel contents.
2. **CI on main may eventually fail** if someone runs `uv sync --frozen` against the stale state (depends on uv's tolerance for self-reference mismatches).
3. **Fix forward**: commit the lockfile sync as a follow-up:
   ```bash
   uv lock                              # if not already done
   git add uv.lock
   git commit -m "fix(release): sync uv.lock to vX.Y.Z self-version

   The chore(release): vX.Y.Z commit bumped pyproject.toml but forgot
   uv.lock. Subsequent uv sync/build calls silently updated the self-
   reference. This commit lands that reconciliation."
   git push origin main
   ```
   Do NOT amend the tagged commit — tags are immutable once pushed.

### PyPI publication status

PyPI is **live** since v0.1.0. `krodo` distribution name is locked; all future releases auto-publish via Trusted Publishers.

## 8. Conduct & licensing

- Be excellent to each other. Discussions stay technical and respectful.
- Contributions land under the project's [Apache-2.0](LICENSE) license.
  You retain copyright; you grant a perpetual, worldwide, no-charge,
  royalty-free license to the project.
