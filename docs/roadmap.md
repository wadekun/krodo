# 开发路线图

> 本文档原为 [`architecture.md`](architecture.md) §8 的内容,2026-07-04 单独
> 提取成独立文档,便于按阶段跟踪进度而不必翻完整架构设计。
> 后续路线图更新直接改本文档;架构设计变更改 architecture.md。

每个任务以"产出 + 验收标准"组织,便于直接拆 issue。代码量估算用于把控范围。

## Phase 0 — 验证想法(3-5 天,~500-800 LOC) ✅

目标:用最简代码跑通"LLM → tool_call → 执行 → 结果反馈"完整链路,验证可驾驭
LiteLLM tool-use 与审批 UX。

- **0.1 项目初始化** — 产出:`uv init` + `pyproject.toml` + `.gitignore` ｜ 验收:
  `uv run python -c "import litellm; print('ok')"` 通过。
- **0.2 最简 LLM 流式调用** — 产出:调 LiteLLM 流式接口的 30 行脚本 ｜ 验收:
  终端逐 token 打印 Claude 回复。
- **0.3 单工具 tool-use 原型** — 产出:定义 `read_file` 工具 + 解析 LLM 返回的
  tool_call ｜ 验收:让模型读取本仓库 `README.md` 并解释。
- **0.4 ReAct 单循环** — 产出:`while no_more_tool_calls` 的最小循环(不到 100
  行)｜ 验收:完成"读取某文件、修改并保存"任务,且写操作前打印 diff 等待 y/n
  确认。
- **0.5 审批 UX 验证** — 产出:终端弹审批的最简实现 ｜ 验收:能拒绝、能"本次
  会话信任"。

退出标准:单文件 `prototype.py`(< 800 行)能完成"读 → 改 → 写"完整 turn,且
关键审批可控。

## Phase 1 — 可用 CLI MVP(2-3 周,~2500-3500 LOC) ✅

目标:发布 v0.1,能 dogfood 改自己的代码、跑测试、修 bug。

- **1.1 仓库脚手架** — 产出:`uv` + `pyproject.toml` + `ruff` + `mypy --strict`
  + `pytest` + GitHub Actions(lint + test + provider mock 矩阵)｜ 验收:
  `uv sync && uv run pytest && uv run ruff check && uv run mypy src` 全绿。
- **1.2 模块骨架 + Protocol** — 产出:按 §6 目录创建空包,所有 §3.4 Protocol
  落地,含 `krodo/core/workspace.py`(`Workspace` 值对象 + `WorkspaceResolver`
  实现 + 启动 banner 打印 root/source)｜ 验收:(a) `from krodo.llm import
  LLMProvider` 等导入全部成功,mypy strict 通过;(b) Workspace 发现优先级 5
  条全部有单测(用 `tmp_path` 构造 `.krodo/` / `.git/` 标记目录,覆盖 flag /
  env / marker / cwd 四种 source);(c) E2E 场景测试:在 CWD ≠ 项目根(如
  `CWD=/tmp`)时启动 CLI,banner 打印的 root / source 与预期一致。
- **1.3 LLM Provider 层** — 产出:`krodo/llm` 的 LiteLLM 实现 + 流式 + 重试 +
  cost tracker ｜ 验收:单测覆盖 ≥ 90%,能切换 Claude/OpenAI 两家而不改 caller
  代码。
- **1.4 Agent Loop** — 产出:`krodo/core` 的 Session / Context / Agent Loop
  (含 token 预算 + 80% 压缩 + `recovery.py` 错误恢复模块)｜ 验收:用 mock LLM
  跑 50+ turn 不爆窗口;§7.5 八个错误恢复场景全部有单测覆盖。
- **1.5 工具集(11 个)** — 产出:§5.2 全部工具 + Pydantic schema + 注册装饰器
  ｜ 验收:每个工具单测覆盖 100%;工具结果格式一致;错误统一格式。
- **1.6 审批 + 沙箱** — 产出:`krodo/sandbox` 路径围栏 + 危险命令黑名单 + 三档
  审批 ｜ 验收:CI 包含 path traversal、symlink escape、危险命令注入的安全测试。
- **1.7 `.krodoignore` + Git checkpoint** — 产出:`.krodoignore` 加载与匹配;
  每次写操作前自动 checkpoint ｜ 验收:`krodo undo` 能回退到上一 checkpoint。
- **1.8 持久化与记忆** — 产出:JSONL event-sourcing session(`SessionStore`
  Protocol + `JsonlSessionStore` 实现,SQLite 后端 Phase 2 再加)+ `AGENTS.md`
  加载(项目+用户层)｜ 验收:`krodo resume <id>` 完整恢复对话。
- **1.9 可观测性** — 产出:structlog JSONL 日志 + cost log + secret redactor
  ｜ 验收:每个 tool call 有 trace 行;日志中无任何 API key 字面量。
  ✅ **M6 交付**:`CostTracker`(`krodo/obs/cost.py`)每次 LLM 调用记录 tokens
  + cost,每 turn 落 `COST_SNAPSHOT` 事件,summary 显示 tokens/cost 行。
- **1.10 CLI** — 产出:Typer 入口 + REPL(prompt_toolkit)+ `krodo exec`
  headless + Rich 流式 / diff 渲染 ｜ 验收:三种入口(REPL / `exec` / pipe
  stdin)皆可用。
  ✅ **M6 交付**:三入口齐备(REPL / `krodo "task"` headless / `echo task |
  krodo` pipe),流式输出(M6.1)+ REPL slash 命令(M6.4)落地。
- **1.11 文档与发布** ✅ **M7 交付** — 产出:README、QUICKSTART、ARCHITECTURE
  (本文)、CONTRIBUTING、SECURITY、CHANGELOG ｜ 验收:✅ 文档四件套齐备;
  ✅ mypy strict 清零;✅ 品牌改名 Coda → krodo 完成;✅ GitHub Release / tag
  走通;✅ **PyPI 上线自 v0.1.0 起**——发行名 `krodo` 锁定,通过 Trusted
  Publishers(OIDC)在 `release.published` 事件触发自动发布
  (`.github/workflows/publish.yml`),`workflow_dispatch` 作为删除+重建同 tag
  release 时的兜底入口。安装:`uv tool install krodo` / `pipx install krodo`。

**退出标准**:用 v0.1 给本项目自己提一个真实 PR 并合入。

## Phase 2 — 增强代码理解与编辑(3-4 周,~+2000 LOC)

目标:从"能改文件"进化到"懂代码",TUI 上线,对接 MCP 生态。

> Phase 2 按 milestone(M8→M12)顺序执行,下面任务编号与 milestone 的对应关系:
> 2.1→M9、2.2→M10、2.3→M11、2.4/2.5→M12、2.6/2.7→M8。milestone 关闭时回来
> 勾掉对应任务(见 [`AGENTS.md`](../AGENTS.md) "Documentation maintenance")。

- **2.1 tree-sitter 符号索引** — 产出:增量索引器 + 启动加载 ｜ 验收:~10k
  文件项目首次索引 < 30s,增量更新 < 1s。
  ✅ **M9 交付**:`src/krodo/indexer/`(`SymbolBackend` Protocol +
  `TreeSitterSymbolIndex`,SQLite WAL,mtime+size 增量)。实测 ha-core 18k
  文件冷构建 22–27s,查询 p95 ≤ 0.1ms,单文件增量 < 20ms(达标,详见
  `docs/benchmarks/m9_symbol_index_perf_results.md`)。收尾额外修了一个
  `tree-sitter` 0.26.0 的 native crash(pin `<0.26` + 子进程金丝雀防线)——
  过程详见同一份 benchmark 文档。
- **2.2 repo-map 注入** — 产出:Aider 式 PageRank repo-map 拼到 system
  prompt ｜ 验收:跨文件重构任务成功率较 Phase 1 baseline +20%。
  ✅ **M10 交付**,注入位置与验收范围有调整:`src/krodo/memory/repo_map.py`
  (基于 M9 索引建文件引用图 + 手写确定性 PageRank + 签名树渲染,token 预算
  默认 2048)。map **不拼 system prompt**,而是作为 `<repo_map>` user 消息注
  入 `_history[1]`(紧跟 `<project_memory>`),配合第二个 Anthropic cache 断
  点让整段稳定前缀命中缓存;每轮刷新走索引 version gate(索引没变零成本,
  ha-core 全量渲染 ~3s / krodo 29ms)。压缩与硬截断均保护稳定前缀(顺手修
  了 `<project_memory>` 被硬截断先丢的既有 bug)。**验收偏离**:"+20% 成功
  率"需要评估 harness(Phase 3)做 baseline 对比,本 milestone 以确定性/预
  算/缓存断点/压缩保护的单测 + 集成测试(830 passed)+ 真实仓库 dogfood 代
  替。已知裁剪:krodo 之外的外部编辑不 bump 索引 version,map 滞后到下次
  session(map 只做定向,实读靠 `read_file` 兜底)。
- **2.3 新工具** — `find_symbol` / `find_references` / `apply_patch v2`(多
  文件原子)｜ 验收:失败可整体回滚。
- **2.4 Textual TUI** — 产出:任务面板 + 实时 diff + 流式日志 + 多 session
  切换 ｜ 验收:`krodo tui` 启动,与 REPL 共享 core。
- **2.5 MCP client** — 产出:能加载第三方 MCP server ｜ 验收:接入 1 个公开
  MCP server(如 fetch、filesystem-extra)跑通。
- **2.6 Provider 矩阵 CI** — 产出:CI 同时跑 Claude / GPT / Gemini / Ollama
  ｜ 验收:4 家全绿才允许 merge。
  ✅ **M8 交付**,验收范围有调整:实际接入 5 家(Claude / GPT / Gemini /
  DeepSeek / Z.AI GLM,未接 Ollama——本地模型留 Phase 2 后续或 Phase 3),且
  `continue-on-error: true`(quota/网络抖动不挡 merge,而不是"全绿才允许
  merge")。脚本:`.github/workflows/scripts/provider_e2e.py`。
- **2.7 Prompt Caching** — 产出:默认开启 Anthropic / OpenAI prompt caching
  ｜ 验收:长会话成本降低 ≥ 30%(基线对比)。
  ✅ **M8 交付**,验收范围有调整:`LiteLLMProvider` 对 `anthropic/*` 显式打
  `cache_control: ephemeral`;OpenAI/Gemini 依赖 provider 侧自动缓存(无需
  krodo 显式处理),因此没有做统一的"降低 ≥30%"基线对比——收益是 provider
  原生能力,非 krodo 自算。

**退出标准**:能在 ~10k 文件的真实开源项目(自选 1-2 个)上做跨文件重构 PR
并合入。

## Phase 3 — 沙箱、权限、评估体系(4-6 周,~+2500 LOC)

目标:从"能用"到"敢用",从"凭感觉"到"可量化"。

- **3.1 OS 级沙箱** — 产出:`bubblewrap`(Linux)+ `sandbox-exec`(macOS)统一
  `SandboxRunner` ｜ 验收:写操作只能落 workspace 内;网络默认拒绝;逃逸测试
  套件全过。
- **3.2 细粒度策略** — 产出:`policy.toml` 支持按工具 / 命令模式 / 路径设规则
  ｜ 验收:能配置"`pytest`/`go test` 自动放行、`rm` 永远拒绝"。
- **3.3 评估 harness** — 产出:自维护 50-100 task 回归集 + 可选 SWE-bench Lite
  子集 runner ｜ 验收:每个 release 自动跑评估并发布报告。
- **3.4 OTEL + Langfuse** — 产出:OpenTelemetry 完整接入 + Langfuse exporter
  ｜ 验收:trace 可在 Langfuse UI 完整查看。
- **3.5 长期记忆(可选)** — 产出:`sqlite-vec` opt-in 向量存储 +
  `semantic_search` 工具 ｜ 验收:默认关闭;启用后跨 session 召回有效。
- **3.6 安全加固** — 产出:prompt injection 测试集 + `.krodoignore` 边界测试
  + 依赖审计 CI ｜ 验收:覆盖 OWASP LLM Top10 相关项。

**退出标准**:陌生用户用 `--full-auto` 跑你给的脚本,不会损坏他的系统。

## Phase 4 — 生产级(持续)

- **4.1 Rust 性能下沉** — sandbox executor、AST indexer、file watcher 三选一
  开始 ｜ 验收:被替换路径 P95 延迟下降 ≥ 50%。
- **4.2 单文件分发** — PyInstaller / shiv 打包 + Homebrew tap + Scoop bucket
  ｜ 验收:`brew install krodo` 可用。
- **4.3 LiteLLM Proxy 企业模式** — 文档 + 模板配置 ｜ 验收:团队可自部署
  gateway,集中管理 key/配额/审计。
- **4.4 团队记忆** — 远程 SQLite/Postgres + 共享 `AGENTS.md` overlay ｜ 验收:
  多人共享同一项目知识。
- **4.5 商业化探索** — 托管 SaaS、企业版 SSO/审计/私有模型路由(按需)。

## Phase 5(可选)— 生态

- VS Code / JetBrains 插件(复用 `core` 通过 stdio JSON-RPC)。
- Web UI(FastAPI 后端复用 `core`)。
- 插件市场 + MCP server 自托管。
