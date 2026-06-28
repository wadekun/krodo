# Krodo

**[English](README.md) | 简体中文**

[![CI](https://github.com/wadekun/krodo/actions/workflows/ci.yml/badge.svg)](https://github.com/wadekun/krodo/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: Pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange.svg)](CHANGELOG.md)
[![PyPI](https://img.shields.io/pypi/v/krodo.svg)](https://pypi.org/project/krodo/)

> 一个**本地优先、多 Provider** 的命令行 coding agent,基于 Python 3.12+。
>
> 状态: 🚧 **Pre-alpha (v0.1.1)** — Phase 1 功能完整 & 已上 PyPI(REPL + headless + pipe 三入口,11 个工具,JSONL 会话,三档审批模式)。Phase 2(TUI、MCP client、tree-sitter 符号索引)规划中。

Krodo 是一个开源 coding agent,灵感来自 Claude Code、Codex CLI 和 Aider。它本地运行,通过工具(read / edit / shell / git / grep)操作你的代码库,通过 [LiteLLM](https://github.com/BerriAI/litellm) 支持任意 LLM provider — Anthropic、OpenAI、Gemini、DeepSeek、Qwen,以及通过 Ollama / vLLM 跑本地模型。

## 为什么再做一个 coding agent

- **本地优先**:你的代码不离开本机,只有 LLM API 调用走外网。
- **多 Provider day 1 就有**:一个配置标志就能在 Claude、GPT、Gemini、DeepSeek、Qwen 或本地模型之间切换。
- **三种 CLI 形态共用一个 core**:`krodo` REPL、`krodo "<prompt>"` headless、`krodo tui`(Phase 2)— 全部共享同一个 agent loop。
- **默认安全**:三档审批模式(`read_only` / `auto_edit` / `full_auto`),路径围栏,危险命令黑名单,每次写操作前自动 git checkpoint。
- **模块化单体**:`core` / `llm` / `tools` / `sandbox` / `memory` / `obs` 之间是干净的 Protocol 接口。好读,好贡献。

完整设计思路见 [`docs/architecture.md`](docs/architecture.md)(英文)。

## 路线图

| Phase | 范围 | 状态 |
|------:|:------|:----:|
| 0 | 验证 ReAct loop 的单文件原型 | ✅ done |
| 1 M1 | Walking skeleton(3 个工具,CLI,agent loop) | ✅ done |
| 1 M2 | 完整工具集(11 个)+ 三档审批 + pattern trust | ✅ done |
| 1 M3 | 上下文管理(token 预算 + 双策略压缩)+ 7 种错误恢复 | ✅ done |
| 1 M4 | `.krodoignore` + git checkpoint + `krodo undo` + diff 预览 | ✅ done |
| 1 M5 | 持久化与记忆:JSONL 会话、`krodo resume`、AGENTS.md、配置文件 | ✅ done |
| 1 M6 | 流式输出 + cost 追踪 + pipe stdin + REPL slash 命令 + 审批持久化 | ✅ done |
| 1 M7 | 品牌改名(Coda → krodo)+ mypy strict 清零 + 文档四件套 + GitHub release + dogfood PR | ✅ done |
| 2 | tree-sitter 符号索引、repo-map、Textual TUI、MCP client | — |
| 3 | OS 级沙箱、评估 harness、OpenTelemetry / Langfuse | — |
| 4 | 生产级:Rust 热点路径、单文件分发、LiteLLM Proxy | — |

## Quick start

### 试用 v0.1.1(Phase 1 功能完整版)

```bash
uv tool install krodo                 # 或: pipx install krodo
export ANTHROPIC_API_KEY=sk-ant-...   # 或 OPENAI_API_KEY / ZAI_API_KEY / ...

mkdir -p /tmp/krodo-sandbox

# Headless:跑一个任务后退出
krodo --root /tmp/krodo-sandbox "create hello.py that prints Hello Krodo, then run it"

# REPL:不传 prompt,进入交互式多轮对话
krodo --root /tmp/krodo-sandbox
# you> create a simple mario game
# (assistant works, then…)
# you> now add a sound effect when collecting coins
# you> exit          # 或 Ctrl-D / Ctrl-C 两次

# Pipe:stdin 作为 prompt...
echo "create hello.py that prints Hello Krodo" | krodo --root /tmp/krodo-sandbox

# ...或作为额外上下文(有 prompt 时)
git diff | krodo "review this change for bugs"
```

助手文本**token-by-token 流式输出**,每次会话结束 summary 会显示成本行:
`tokens     : 12.3k in / 4.1k out | cost $0.0231`。

REPL 模式下,对话历史(包括上一轮 agent 做的所有事)会跨轮保留,所以"现在加个 X"
或"修一下刚才那个 bug"这种追问自然好用。退出方式:`exit` / `quit` / `:q` / Ctrl-D /
在 prompt 处连按两次 Ctrl-C。

### REPL slash 命令

slash 命令在本地处理 — 永远不会发给 LLM:

| 命令 | 行为 |
|:--------|:-------|
| `:help` | 列出可用命令 |
| `:sessions` | 显示当前 workspace 最近 10 个会话 |
| `:undo` | 把文件恢复到上一个 checkpoint |
| `:cost` | 显示会话 token / cost 累计 |
| `:resume <id>` | 切换到另一个会话(历史会被 replay) |
| `:q` | 退出(同 `exit`) |

### 恢复之前的会话

会话会自动持久化。你可以从上次中断的地方继续:

```bash
# 列出最近的会话
krodo resume --list

# 按 session ID(或唯一前缀)恢复
krodo resume a3f2b1

# 在指定 workspace 恢复
krodo resume --root /tmp/krodo-sandbox a3f2b1
```

`krodo resume` 会把存储的事件历史 replay 进新的 REPL,所以模型记得上次的所有事 —
改过哪些文件、调用过什么工具、对话内容。

### 安装(PyPI)

Krodo 已在 PyPI 上线。选你顺手的工具:

```bash
uv tool install krodo                # 推荐(快、隔离)
# 或
pipx install krodo                   # 同样好用
# 或
pip install krodo                    # 能用,但 pip 全局安装容易跟其他包打架
```

验证:

```bash
krodo --version                      # krodo <version>
krodo --help
```

如果从源码跑(开发用):

```bash
git clone https://github.com/wadekun/krodo
cd krodo
uv sync                              # 创建 .venv,装运行时 + dev deps
uv run krodo --help                  # 未 pip 安装,需要 `uv run`
```

## 可用工具(共 11 个)

| 工具 | 类别 | 需要审批 | 描述 |
|:-----|:---------|:--------:|:------------|
| `read_file` | 读 | 否 | 读文件(可选 offset/limit) |
| `list_dir` | 读 | 否 | 列目录(深度限制,跳过噪音目录) |
| `glob` | 读 | 否 | 按模式找文件(`**/*.py`) |
| `grep` | 读 | 否 | 正则搜索;有 ripgrep 用 ripgrep,否则 Python 兜底 |
| `git_status` | Git | 否 | 看工作树状态(`git status --porcelain`) |
| `git_diff` | Git | 否 | 看 unified diff(staged/unstaged,可选 path 过滤) |
| `write_file` | 写 | 是 | 写或覆盖文件 |
| `edit_file` | 写 | 是 | 定点字符串替换,强制唯一性 |
| `apply_patch` | 写 | 是 | 原子化应用 unified diff,失败回滚 |
| `run_shell` | Shell | 是 | 在 workspace 沙箱内执行 shell 命令 |
| `git_commit` | Git | 是 | commit 暂存文件(自动从 message 里 redact API key) |

## 上下文与恢复

Krodo 强制 token 预算,并提供双压缩策略,长会话也不会爆上下文窗口。

### Token 预算(§3.4.1)

预算是模型上下文窗口的 80%。到 80% 触发压缩。到 95% 触发硬截断兜底。可用预算归零时,
下一轮被拒绝并给清晰提示。

```
Total budget   = model_context_window × 0.80
Output reserve = total_budget × 0.15
Compress at    = 80% of budget (默认)
Truncate at    = 95% of budget
```

### 压缩策略

通过 `KRODO_COMPRESS` 环境变量选择:

| 策略 | Env 值 | 描述 |
|:---------|:----------|:------------|
| LLM 摘要(默认) | `llm` | 调同一个 LLM provider 把最旧 N 轮对话摘要成 `<SUMMARY>…</SUMMARY>` 块。 |
| 算法压缩 | `algorithmic` | 丢 `tool_result` 内容,保留 tool-call 元数据和文件路径。零额外 LLM 成本 — 离线开发或大型 codebase 友好。 |

```bash
# 用算法压缩(不调额外 LLM):
KRODO_COMPRESS=algorithmic krodo "..."

# 覆盖 Claude 的 token ratio(默认 1.1x,tiktoken 会少算):
KRODO_TOKEN_RATIO=1.15 krodo --model anthropic/claude-3-5-sonnet "..."
```

### 错误恢复(7 种场景,§7.5)

| # | 场景 | 恢复策略 |
|---|------|---------|
| 1 | LLM 返回非法 tool-call JSON | 重注入 schema + 错误;重试 ×2,然后 abort |
| 2 | 工具执行超时 | kill 子进程;用截断的部分结果跳过这次调用 |
| 3 | Agent stall(同一写工具调用 3 次) | Abort turn;给用户看最后 3 次调用 |
| 4 | 压缩引发的上下文丢失 | 重注入 pinned 文件路径 + 最后一条 user message |
| 5 | 文件被外部修改(SHA-256 conflict) | 拒绝写;要求 agent 先 re-read 文件 |
| 6 | Provider rate limit / 5xx | 指数退避 ×3(1s / 2s / 4s) |
| 7 | 文件权限拒绝(EACCES) | 跳过写;报告路径 + 权限位 |

### CLI 标志

```bash
# 限制单 turn 工具调用次数(默认 25):
krodo --max-tool-calls 5 "..."

# 设置压缩窗口(一次压缩多少轮对话):
krodo --summary-window 3 "..."
```

## 持久化与记忆

### 会话存储

每个会话自动保存到 `.krodo/sessions/<session_id>.jsonl`(在你的 workspace 里)。
每行是一个 JSON 事件(`USER_MESSAGE`、`ASSISTANT_MESSAGE`、`TOOL_CALL`、`TOOL_RESULT`、
`COMPRESSION` 等),带单调递增的 `seq` 号,多进程追加安全。

应用日志写到 `.krodo/logs/<session_id>.log`(纯 `structlog` JSONL — 跟会话事件分开)。

### AGENTS.md — 项目记忆

在项目任意位置放一个 `AGENTS.md`,Krodo 会自动把它作为 `<project_memory>` 注入每次会话:

| Tier | 位置 | 用途 |
|------|------|------|
| System | `~/.config/krodo/AGENTS.md` | 个人约定(所有 workspace 都生效) |
| Project | `<workspace>/AGENTS.md` | 项目特定规则(始终包含,不会被丢) |
| Subdir | `<cwd>/AGENTS.md` … 上溯到 workspace root | 当前工作目录的上下文文档 |

每文件上限 8K tokens;总预算 12K tokens(超限时先丢 subdir 文件)。

### 配置文件

可以在 `.krodo/config.yaml`(workspace 级)或 `~/.config/krodo/config.toml`(用户级)
设默认值。优先级:CLI flag > env var > workspace > user > 内置默认。

快速示例:

```yaml
# .krodo/config.yaml — workspace 级默认
model: deepseek/deepseek-v4-flash
approval: auto_edit
max_tool_calls: 15
```

**完整字段参考 + 10 个 provider + 各 provider API key + 故障排查
(字段名陷阱、兼容代理坑、错误模式诊断):见
[Models & Providers](docs/MODELS.md)(英文)。**

每次改完 config,务必跑 `krodo doctor` 验证实际加载的内容。

## .krodoignore 与 Git checkpoint

两道安全网:4-tier ignore 系统 + 写前自动 git checkpoint。

### .krodoignore — 4 层路径过滤(§5.3)

每个 `read_file`、`list_dir`、`glob`、`grep` 调用都会先经过 `KrodoIgnore` 才碰盘。
规则按特异性递增,从 4 个来源合并:

| Tier | 来源 | 可被覆盖? |
|------|------|----------|
| 1 | 硬编码默认(`.env`、`*.pem`、`id_rsa`、`node_modules/` 等) | ❌ 始终生效 |
| 2 | 项目 `.gitignore` | — |
| 3 | 项目 `.krodoignore`(workspace root) | 加自定义模式 |
| 4 | 用户级 `~/.config/krodo/krodoignore` | 个人 override |

当 path 匹配任意规则,工具返回:
```
PathIgnoredError: '<path>' is ignored (rule: '<pattern>' from <source>)
```

#### `.krodoignore` 示例

```gitignore
# 排除内部数据目录不被 agent 读
data/raw/
reports/*.csv

# 排除生成的 mock 文件
tests/fixtures/generated/
```

### Git checkpoint(§5.4)

每次写(`write_file`、`edit_file`、`apply_patch`)和写启发式 shell 命令之前,
Krodo 创建一个轻量 `git stash create` checkpoint:

1. 收集受影响路径。
2. `checkpoint_sha = git stash create` — **不**推到 stash 栈;工作树不动。
3. emit 一个 `CHECKPOINT` `SessionEvent` 到 `.krodo/logs/<session>.jsonl`。
4. 执行写。

非 git workspace 上,checkpoint 降级为 no-op(打 warning,写操作照常)。

### krodo undo

```bash
# 撤销最近会话的最后一个 checkpoint:
krodo undo [--root <workspace>]

# 撤销指定会话:
krodo undo --session <session_id> [--root <workspace>]
```

`krodo undo` 读会话 JSONL,找最近一个 `CHECKPOINT` 事件,跑
`git checkout <sha> -- <affected_paths>`,只恢复这些路径。其他文件不动。

| 条件 | 行为 |
|------|------|
| 非 git workspace | 退出码 1,友好错误信息 |
| 找不到 CHECKPOINT | 退出码 1,提示日志路径 |
| `affected_paths` = workspace root(shell 命令范围) | 恢复前提示确认 |

## CLI 子命令语义

Krodo 有三个具名子命令 — `resume`、`undo`、`doctor` — 加上自由形态的 headless prompt。
解析器这样路由:

| 调用 | 行为 |
|---|------|
| `krodo "create a mario game"` | Headless — prompt 是 `"create a mario game"` |
| `krodo` | 交互 REPL |
| `krodo resume` | Resume 子命令(最近会话) |
| `krodo resume abc123` | Resume 子命令,session ID `abc123` |
| `krodo resume --root /path` | Resume 子命令;`--root` 给 resume |
| `krodo --root /path resume` | Resume 子命令;全局 `--root` 作默认继承 |
| `krodo undo` | Undo 子命令 |
| `krodo doctor` | Doctor 子命令 |

**关键规则:**

- 第一个**非选项** token 会跟注册的子命令名比对。匹配就走子命令分发 — 永远不会被当 headless prompt。
- 全局标志(`--root`、`--model`、`--approval` 等)可以放在子命令 token **之前或之后**。
  放在前面会被传播给子命令作默认;子命令里显式给的标志永远赢。
- 自然语言 prompt 应该**加引号**,作为单 token 传入。不加引号第一个词可能撞上子命令名:
  ```bash
  krodo "resume the work from yesterday"   # ✓ headless 走完整 prompt
  krodo resume the work from yesterday     # ✗ 路由到 resume 子命令;"the" 是意外参数
  ```

## 本地开发

Krodo 用 [`uv`](https://docs.astral.sh/uv/) 管依赖和 venv。

```bash
git clone https://github.com/wadekun/krodo
cd krodo
uv sync                       # 装依赖 + 创建 .venv
uv run pytest                 # 跑测试
uv run ruff check             # lint
uv run mypy src               # 类型检查
```

LLM 凭证放环境变量(按你用的 provider 设):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
# 或用系统 keyring(共享机器推荐,见 docs/architecture.md §7.2)
```

## 项目结构

```
krodo/
  src/krodo/
    cli/        # Typer 入口、REPL、headless exec
    core/       # Agent loop、Context、Budget、Compression、Recovery、Events
    llm/        # LLMProvider Protocol + LiteLLM 适配
    tools/      # 文件 / shell / patch / search / git 工具
    sandbox/    # 路径围栏、命令策略、审批模式
    memory/     # JSONL 会话存储、krodo resume、AGENTS.md loader、config
    obs/        # structlog + OpenTelemetry + cost 追踪
  tests/{unit,integration,e2e}/
  docs/
    architecture.md         # 设计基线(先读这个)
    reviews/                # 历次架构 review 笔记
  scripts/
    prototype.py            # Phase 0 单文件原型(DEPRECATED — 用 krodo CLI)
```

## 贡献

这是一个学习 + 生产型项目。Phase 1 功能完整、CI 门控稳定,欢迎贡献。

基本规则:

- 所有代码过 `ruff` + `mypy --strict` + `pytest --cov`(完整 CI 门控见
  [`CONTRIBUTING.md`](CONTRIBUTING.md))。
- 所有新工具都要 100% 单测覆盖 + 一个对录制 LLM 响应的集成测试(`vcrpy`)。
- 改 agent loop 的所有变更都要过回归测试矩阵(Phase 2+)。
- 七大工程原则见 [`docs/architecture.md`](docs/architecture.md) §11。

## 文档

| 文档 | 内容 |
|------|------|
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | 5 分钟安装 + 跑第一个任务 |
| [`docs/MODELS.md`](docs/MODELS.md) | Model & provider 配置、切换、故障排查 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 开发环境、CI 门控、PR 流程、commit 规范 |
| [`SECURITY.md`](SECURITY.md) | 威胁模型、沙箱边界、漏洞报告 |
| [`CHANGELOG.md`](CHANGELOG.md) | Milestone-by-milestone 变更记录 |
| [`docs/architecture.md`](docs/architecture.md) | 完整设计基线(single source of truth) |
| [`AGENTS.md`](AGENTS.md) | 自动加载的项目记忆(每次会话都注入) |

## License

[Apache-2.0](LICENSE) © The Krodo Contributors
