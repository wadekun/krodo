# FIX-003: `.gitignore` 不再屏蔽 agent read/write + StallDetector 严格连续语义

> **状态**: ✅ 已实施(分支 `fix/gitignore-read-policy`)
> **影响范围**: `src/krodo/sandbox/ignore.py`, `src/krodo/tools/builtin/fs.py`,
>           `src/krodo/core/recovery.py`
> **发现日期**: 2026-07-05
> **严重程度**: 高(agent 在 `.gitignore` 路径下完全无法工作,触发误 stall)

## 问题描述

用户在 session `5040d7bc` 用 `deepseek/deepseek-v4-pro` 让 krodo 设计 Phase 2
plan(需要读 `.cursor/plans/plan_bd67e336.plan.md` 作参考),任务 halt:

> Task halted: agent stalled (3× identical write call) — try rephrasing or use full_auto

初诊把问题归为"模型认知偏差"——反复 cat 同一文件被 stall 抓住。**错了**——session
日志显示模型每次 `read_file` 都收到 `is_error=True, content=""`,模型反复尝试 cat
绕过是合理工程反应。问题在 krodo 代码,不在模型。

### 实际表现

```
you> 参考 .cursor/plans/plan_bd67e336.plan.md 设计 Phase 2 plan
[模型] read_file .cursor/plans/plan_bd67e336.plan.md   → is_error=True, content=""
[模型] read_file .cursor/plans/plan_bd67e336.plan.md   → is_error=True, content=""
[模型] (尝试 cat 绕过) run_shell: cat .cursor/...      → 成功
...
[模型] 第 3 次 cat → Task halted: agent stalled
```

### 期望表现

```
you> 参考 .cursor/plans/plan_bd67e336.plan.md 设计 Phase 2 plan
[模型] read_file .cursor/plans/plan_bd67e336.plan.md   → 成功(返回内容)
[模型] 基于 Phase 1 plan 设计 Phase 2 plan...
```

## 根因分析

三层独立的代码问题叠加:

### 真因 A(主要):`.gitignore` 屏蔽 agent 行为(过度保守)

`src/krodo/sandbox/ignore.py` 旧 4 层规则:

```
1. Hard-coded defaults(secrets + .git/ + __pycache__/ + node_modules/ ...)
2. Project .gitignore       ← 把"git 不追踪"误解为"agent 不能访问"
3. Project .krodoignore
4. User ~/.config/krodo/krodoignore
```

`read_file` 在 `src/krodo/tools/builtin/fs.py:89-91` 调用
`ctx.ignore.match(target)`,is_ignored=True 直接返回错误——所以 `.cursor/`(在
`.gitignore` 里)被屏蔽,模型读不到。

**行业惯例对照**:

| Agent | `.gitignore` 对 read | `.gitignore` 对 write |
|---|---|---|
| Claude Code | 不屏蔽 | 不屏蔽 |
| Aider | 不屏蔽 | 不屏蔽 |
| Cursor | 不屏蔽 | 不屏蔽 |
| krodo 旧版 | **屏蔽**(过度保守) | **屏蔽**(过度保守) |

### 真因 B:read_file 屏蔽时返回空 content,无 hint

`fs.py:91` 旧代码 `return str(match.error())`,但实际 tool_result content 是空
字符串(event logger 层面或 MatchResult.error() 实现的问题)。模型不知道为什么
读不到,只能反复试。

### 真因 C:StallDetector 跨多 turn 误报

`src/krodo/core/recovery.py:107-108` 旧代码:

```python
def record(self, tool_name, arguments):
    if tool_name not in _WRITE_TOOLS:
        return  # ← read-only tool 直接 return,不重置 _consecutive
```

导致:write(A) → write(A) → 30+ read-only → write(A) 第 3 次被判"连续 3 次"触发
StallError——read-only 调用之间的"陈旧"状态不重置。

## 修复方案

### 修复 A:`.gitignore` 完全不影响 agent 行为(主要)

`src/krodo/sandbox/ignore.py` 改 4 层 → 3 层:

```
新 3 层:
1. Hard-coded defaults(不变 — secrets + .git/ + __pycache__/ + node_modules/ ...)
2. Project .krodoignore
3. User ~/.config/krodo/krodoignore
```

**关键安全不变量**:`node_modules/` / `__pycache__/` / `.venv/` / `dist/` /
`build/` / `.git/` 等都在 `_HARDCODED_PATTERNS`(L39-86)——这些**不靠 .gitignore**
层。移除 .gitignore 层后这些目录仍被屏蔽,安全底线不降。

用户要 opt-out 任何路径,显式写 `.krodoignore`(专门的 agent 访问策略文件,语义
清晰)。

`KrodoIgnore.match()` 方法签名 / 返回值**不变**——所有调用点(fs.py / search.py /
patch.py)零改动。

### 修复 B:read_file 屏蔽时返回清晰错误

`src/krodo/tools/builtin/fs.py:88-97` 改:

```python
match = ctx.ignore.match(target)
if match.is_ignored:
    return (
        f"ERROR: path '{params.path}' is ignored by krodo "
        f"(rule: '{match.pattern}' from {match.source}). "
        "This is a krodo policy decision, not a missing file. "
        "To allow access, list the path in <workspace>/.krodoignore "
        "as an override, or use run_shell with approval."
    )
```

让模型知道:**这是 krodo 主动屏蔽,不是文件不存在**,并给出绕过 hint。

### 修复 C:StallDetector 严格"连续相邻"语义

`src/krodo/core/recovery.py:StallDetector.record()` 改:

```python
def record(self, tool_name, arguments):
    sig = _signature(tool_name, arguments)
    self._recent.append(...)
    is_write = tool_name in _WRITE_TOOLS

    if is_write and sig == self._last_sig:
        # Same write tool called with identical args as the previous
        # *adjacent* write — extend the consecutive chain.
        self._consecutive += 1
    else:
        # Any other call (read-only, or a different write) breaks the
        # chain. If this call is itself a write, it starts a new chain
        # of length 1; reads leave the counter at 0.
        self._consecutive = 1 if is_write else 0
        self._last_sig = sig if is_write else None

    if self._consecutive >= _STALL_THRESHOLD:
        raise StallError(tool_name, self._consecutive)
```

新语义:stall 严格定义为"3 次**真正相邻**的相同 write call"。中间任何其他 tool
调用(read 或不同 write)都打断连续链。

## 影响面评估

| 场景 | 旧行为 | 新行为 |
|---|---|---|
| `read_file .env` | 屏蔽(hardcoded) | 屏蔽(**不变**) |
| `read_file .cursor/foo` | 屏蔽(.gitignore) | **可读** ✅ |
| `read_file node_modules/foo` | 屏蔽(hardcoded) | 屏蔽(**不变**) |
| `read_file docs/_drafts/x.md` | 屏蔽(.gitignore) | **可读** ✅ |
| `read_file tmp/debug.log` | 屏蔽(.gitignore) | **可读** ✅ |
| `write_file .cursor/foo` | 屏蔽(.gitignore) | **可写** ✅(跟 Claude Code 一致) |
| `write_file .env` | 屏蔽(hardcoded) | 屏蔽(**不变**) |
| 用户在 `.krodoignore` 加 `.cursor/` | 屏蔽 | 屏蔽(**不变**) |
| StallDetector | 跨多 turn 误报 | 只在真正连续时报 ✅ |

## 安全不变量

- Hardcoded secrets(`.env` / `*.pem` / `id_rsa*` / `credentials.json` /
  `*.kdbx` / `*.p12` 等)——**永远屏蔽,任何配置无法关闭**
- Common noise directories(`.git/` / `__pycache__/` / `node_modules/` /
  `.venv/` / `dist/` / `build/` / `.mypy_cache/` 等)——**永远屏蔽**
- Large / binary files(`*.bin` / `*.so` / `*.zip` / `*.parquet` / `*.sqlite`
  等)——**永远屏蔽**
- 用户显式 `.krodoignore`——**优先级最高,可屏蔽任何路径**

## 验证计划

### 单元测试(新增 / 修改)

- `tests/unit/test_ignore.py`:
  - `TestGitignoreNotConsulted` 类 4 个测试验证 .gitignore 不再被加载
  - `TestKrodoignore` 全部保留 + 新增 `.krodoignore` 与 `.gitignore` 冲突时的优先级测试
- `tests/unit/test_fs_tools.py`:
  - `test_read_file_ignored_returns_clear_error`(替代旧 `test_read_file_ignored_by_krodoignore`)
  - `test_read_file_under_gitignore_succeeds`(新增,验证修复 A)
- `tests/unit/test_recovery.py`:
  - `test_read_only_intervention_resets_consecutive`(新增,验证修复 C 核心)
  - `test_different_write_resets_consecutive`(新增)
  - `test_truly_consecutive_writes_still_stall`(新增 sanity check)

### CI gate

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
# 全绿(704 测试通过)
```

### 端到端复现 session 5040d7bc 场景

```bash
export KRODO_MODEL=deepseek/deepseek-v4-pro
krodo --root /Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo
# 输入 "参考 .cursor/plans/plan_bd67e336.plan.md 设计 phase 2 plan"
# 期望:read_file 一次成功(不再需要 cat 绕过)
```

## 受影响 / 不受影响的场景清单

| 场景 | 受影响? | 备注 |
|---|---|---|
| 读取项目源码(`src/` / `tests/` / `docs/`) | ❌ 不受影响 | 本来就不在 .gitignore |
| 读取 IDE / 工具配置(`.cursor/` / `.idea/` / `.vscode/`) | ✅ 修复 | 现在可读 |
| 读取 gitignored 草稿(`docs/_drafts/`) | ✅ 修复 | 现在可读 |
| 读取 gitignored debug 输出(`tmp/`) | ✅ 修复 | 现在可读 |
| 读取密钥(`.env` / `*.pem`) | ❌ 不受影响 | hardcoded 屏蔽 |
| 读取 Python 缓存(`__pycache__/`) | ❌ 不受影响 | hardcoded 屏蔽 |
| 写 `.cursor/foo` | ✅ 修复 | 现在可写(配合 git checkpoint 恢复) |
| 写 `.env` | ❌ 不受影响 | hardcoded 屏蔽 |
| 用户 `.krodoignore` 显式屏蔽 | ❌ 不受影响 | 优先级最高 |

## 不在本次范围

- 改 system prompt(模型行为已经合理)
- 重命名 `_WRITE_TOOLS` / `_HARDCODED_PATTERNS`(等真正必要时再做)
- list_dir 是否默认隐藏 `.gitignore` 文件(本次保持现状;后续单独 PR)
- 配置开关 `KRODO_RESPECT_GITIGNORE`(默认行为变了就够,无需 config 开关)
- 持久化 StallDetector 状态到 session(防止跨 turn 误报的另一种方案,本次不需要)
