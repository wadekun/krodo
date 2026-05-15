# Architecture Review：`docs/architecture.md` v0.2

> Reviewer: AI Architect | Date: 2026-05-06 | Document Version: v0.2

---

## 总体评价

v0.2 是一份**高质量的设计基线文档**，可以用于指导 Phase 0-1 的实现。新增的 §3.4 Protocol 接口、§2.1 时序图、§5.3 .codaignore、§5.4 Git 安全网、§0.3 硬约束、§11 工程原则都是有价值的补充。

上轮 Review（v0.1）中的主要问题已部分解决：Protocol 接口补全、Python 版本升级到 3.12+、Phase 0 验收标准细化。下面只列出**仍未解决或新增的问题**。

---

## A. 需要修正（存在矛盾或影响可行性）

### A1. §6 目录结构与 §12 First Step 自相矛盾

这是当前文档最严重的问题。两处给出了**不一致的项目结构**。

§6 写的是：
```
coda/
  apps/cli/
  packages/core/
  packages/llm/
  packages/tools/
  ...
```

§12 First Step 实际执行的是：
```bash
mkdir -p src/coda/{cli,core,llm,tools,sandbox,memory,obs}
```

这是两套完全不同的结构。开发者拿到这份文档会困惑该用哪个。

**建议**：统一为 `src/coda/` 扁平结构（§12 的方案）。理由：

- 这是 Python 项目的标准 `src` layout，所有主流打包工具（uv/hatch/setuptools）原生支持
- IDE（PyCharm/VS Code）对这种结构的自动补全和跳转支持最好
- `from coda.core import AgentLoop` 比 `from packages.core import ...` 自然得多
- 所有成功的 Python coding agent（Aider/SWE-agent/OpenHands）都是这种结构
- monorepo 拆包的收益在 CLI 单进程场景下为零，成本却是 import 路径别扭 + 打包配置复杂

如果未来确实需要拆包（如做 IDE 插件复用 core），那时候再拆也来得及——Python 的包拆分成本很低。

### A2. 工具数量计数不一致

文档中至少有三处给出了不同的数字：

- §9 结论："8 个核心工具"
- §5.1 Must Have："核心工具集（11 个）"
- §8 Phase 1 任务 1.5："11 个"

§5.2 实际列出了 11 个 Pydantic Model（ReadFile, WriteFile, EditFile, Glob, ListDir, Grep, RunShell, ApplyPatch, GitStatus, GitDiff, GitCommit）。

**建议**：以 §5.2 为准，§9 的"8 个"改为"11 个"。

### A3. §5.4 Git checkpoint 的技术描述需要修正

原文：

> 任何写操作执行前自动创建 checkpoint：`git stash create` 或专用 branch

`git stash create` 的行为是：创建一个 stash commit 对象并返回其 SHA，但**不会修改工作区，也不会把 stash push 到 stash 栈上**。这意味着：

- 如果只是 `git stash create`，文件系统中被修改的文件仍然保持修改状态
- Agent 后续执行的工具调用仍然看到"已修改"的文件
- 如果工具执行失败，要用这个 SHA 做回滚，需要 `git stash apply <sha>` 或 `git checkout <sha> -- .`

设计意图是对的（写操作前保存快照以便回滚），但技术实现需要更精确。

**建议**改为：

```
写操作前：
1. git stash create → 获取 checkpoint_sha
2. 记录 (checkpoint_sha, session_id, seq_num) 到 session store
3. 执行写操作

回滚时（coda undo）：
1. git checkout <checkpoint_sha> -- <affected_paths>
2. 或 git reset --hard <checkpoint_sha>（仅影响 working tree，不移动 HEAD）
```

### A4. `LLMProvider.stream_chat` 返回类型需要确认

§3.4 中 `stream_chat` 的签名：

```python
def stream_chat(
    self, messages: list[Message], tools: list["ToolDef"] | None = None
) -> AsyncIterator[LLMChunk]: ...
```

这里用的是 `def`（非 `async def`）返回 `AsyncIterator`。技术上可行（方法本身不是协程，但返回异步迭代器），但有两个注意点：

- LiteLLM 的流式接口返回 `CustomStreamWrapper`，需要包装成 `AsyncIterator[LLMChunk]`
- 调用方用 `async for chunk in provider.stream_chat(...)` 消费

**建议**：在 Protocol 注释中明确标注调用方式，或改为 `async def` 返回 `AsyncGenerator[LLMChunk, None]`（语义更清晰，实现者不容易混淆）。

---

## B. 需要补充（影响完整性）

### B1. System Prompt 设计策略缺失

System prompt 决定了 agent 的行为质量，但它不在任何子系统中——它是跨系统的设计决策。文档只在 §2 的数据流中提到"含 system prompt"，没有专门讨论设计策略。

**建议**至少补充以下内容（可放在 §5.5 或 §4 中）：

- **组成部分**：角色定义、工具使用规范、代码编辑规范、安全约束
- **注入策略**：AGENTS.md 内容如何注入（拼接到 system prompt 末尾？作为独立的 user message？）
- **行为约束**：告诉模型"先 read 再 edit"、"不要猜测文件内容"、"修改前先展示 diff"等
- **版本管理**：system prompt 是否可随项目配置更新，还是硬编码在代码中

即使不在架构文档中写完整 prompt，也应该定义 prompt 的**组装策略**和**注入点**。

### B2. Token 预算分配算法缺失

§0.3 硬约束提到"超过 80% 触发自动压缩"，但文档没有定义预算的分配策略。Phase 1 任务 1.4 要求"token 预算 + 80% 压缩"实现，但没有告诉实现者预算怎么算。

**建议**补充一个简单的预算模型（可放在 §3.4 ContextManager 注释或附录中）：

```
总预算 = model_context_window × 0.80

固定开销（每个 session）：
  system_prompt:  ~3K tokens
  AGENTS.md:      ~1-2K tokens
  tool_schemas:   ~2-3K tokens (11 个工具的 JSON Schema)

动态分配（每 turn 重新计算）：
  可用预算 = 总预算 - 固定开销 - 当前消息占用
  输出预留 = 可用预算 × 0.15
  实际可用 = 可用预算 - 输出预留

压缩触发：
  当前占用 > 总预算 × 0.80 → 压缩最早的 2 轮（用 LLM 生成摘要替换）
  当前占用 > 总预算 × 0.95 → 硬截断最早的历史 + 用户告警
```

### B3. 错误恢复策略不完整

§7 列出了单项风险和缓解措施，但缺少一个**整体的错误恢复策略**。以下是 coding agent 特有的关键错误场景：

| 场景 | 建议策略 |
|------|---------|
| LLM 返回格式异常（非有效 JSON） | 解析失败 → 回填格式错误给模型重试（最多 2 次）→ 仍失败则终止 turn 并提示用户 |
| 工具执行超时 | 默认 60s；超时后 kill 进程 + 回滚到上一个 checkpoint；回填超时错误给模型 |
| Agent 连续 N 轮无进展 | 检测连续 3 次相同 tool call（相同 name + 相似参数）→ 强制退出并提示用户 |
| 上下文压缩导致信息丢失 | 压缩前后对比关键文件路径是否保留；丢失时通知用户"上下文已压缩，部分历史可能不可用" |
| 并发文件修改冲突（用户手动改了同一文件） | edit_file 前用 hash 校验文件未变；hash 不匹配 → 回填冲突错误 + 当前文件内容让模型重新决策 |

这些不需要在架构文档中写很细，但应该有一个"错误恢复矩阵"表格。

### B4. Session 事件数据模型缺失

§3.4 定义了 `SessionStore` Protocol（`create` / `append_event` / `load` / `list_recent`），但没有定义 session 的事件 schema。event-sourcing 的关键是事件格式的稳定性和可重放性。

**建议**至少定义事件类型枚举：

```python
class SessionEventType(str, Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CHECKPOINT = "checkpoint"
    COMPRESSION = "compression"  # 压缩发生时记录，用于 debug
    COST_SNAPSHOT = "cost_snapshot"

class SessionEvent(BaseModel):
    id: str
    session_id: str
    type: SessionEventType
    timestamp: datetime
    data: dict  # 事件具体内容，结构由 type 决定
```

### B5. LiteLLM 版本锁定策略缺失

§3.2.A 决定使用 LiteLLM 作为底座，§0.3 要求"不引入不稳定（< 1.0 且半年无 release）的核心依赖"。但文档没有检查 LiteLLM 是否满足这个约束。LiteLLM 更新频率极高（几乎每周），且经常有不兼容变更。

**建议**：

- §12 的 `uv add litellm` 改为 `uv add "litellm>=1.40,<2"`，锁定主版本
- §0.3 或 §11 补充原则："核心依赖的 minor 版本升级需经 PR 审批并跑完回归测试"

---

## C. 需要细化（提升质量）

### C1. §0.3 tool call 上限 25 偏高

每 turn 25 次 tool call 在实际场景中意味着模型可能在一个 turn 里读 25 个文件或执行 25 条命令。以 Claude Sonnet 的定价，一次 25-tool-call 的 turn 成本可能在 $0.5-2 之间。

**建议**：MVP 默认值改为 **10**，同时允许用户在配置中调整。10 次 tool call 对大多数 coding 任务已经足够（读 3-4 个文件 + 改 1-2 个文件 + 跑 1-2 个命令 + 1-2 次 grep）。

### C2. AGENTS.md 机制的细节缺失

§5.1 提到"AGENTS.md 自动加载（项目根）"，但没有说明：

- **合并策略**：多级 AGENTS.md（项目根/子目录/用户级 `~/.config/coda/AGENTS.md`）全部拼接？有冲突时谁优先？
- **长度限制**：如果 AGENTS.md 有 50K tokens 怎么处理？截断？报错？
- **能力边界**：是否支持用户在 AGENTS.md 中定义自定义行为指令？

**建议**：补充简短的合并规则——项目根级 > 用户级 > 系统级；全部拼接（不 override）；总长度上限 8K tokens，超出截断并告警。

### C3. §12 的默认模型名需确认

```yaml
provider: anthropic/claude-sonnet-4-5
```

需确认这是否是 LiteLLM 的正确模型标识符。LiteLLM 的模型名通常需要带日期后缀或使用特定的简写格式。建议明确指定一个确实有效的模型名，或注明"以 LiteLLM 支持的模型列表为准"。

### C4. `requires_approval` 应为动态决策

§3.4 中 `Tool` Protocol 的 `requires_approval: bool` 是静态属性。但实际上同一种工具是否需要审批取决于上下文：

- `read_file` 读 `README.md` 不需要审批，读 `.env` 需要
- `run_shell` 跑 `ls` 不需要，跑 `rm -rf` 需要

**建议**：在 Protocol 注释中明确 `requires_approval` 是"默认值"，实际审批决策由 `ApprovalManager` 根据 tool name + args 动态决定。或者直接改为：

```python
class Tool(Protocol):
    definition: ToolDef
    requires_approval: bool  # 默认值，实际由 ApprovalManager.check() 动态决定
```

---

## D. 文档质量

### D1. 变更日志质量高

§10 详细列出了每个变更点，便于追溯。建议继续保持这个习惯。

### D2. §8 Phase 任务组织方式改进明显

"产出 + 验收标准"的格式可以直接拆 issue，非常实用。

### D3. §11 工程原则第 6 条措辞建议微调

原文："三次重复才提取"

可能被误解为"不要写函数"。建议改为：

> **三次跨模块重复才提取抽象**。同一个模块内的提取不需要等三次；只有当你准备创建一个被多个模块共享的抽象时才需要这个约束。不为假设的未来需求设计。

---

## 评分

| 维度 | v0.1 | v0.2 | 变化 |
|------|------|------|------|
| 完整性 | 8/10 | **8.5/10** | +Protocol 接口、时序图、.codaignore、Git 安全网 |
| 正确性 | 7/10 | **8/10** | +硬约束量化；- §6/§12 矛盾 |
| 可执行性 | 8/10 | **9/10** | +First Step 命令、验收标准、工程原则 |
| 一致性 | 9/10 | **7/10** | - §6 vs §12 矛盾、工具数不一致 |
| 可维护性 | 8/10 | **8.5/10** | +变更日志详细、工程原则明确 |
| **综合** | **8.0/10** | **8.2/10** | |

---

## 优先级排序

### 必须修正（阻塞进入 Phase 0）

| # | 问题 | 修正方式 | 工作量 |
|---|------|---------|-------|
| A1 | §6 vs §12 目录结构矛盾 | 统一为 `src/coda/` 扁平结构；§6 标注为"Phase 2+ 目标结构"或删除 | 30 min |
| A2 | 工具数量不一致 | §9 的"8 个"改为"11 个" | 5 min |

### 应该补充（Phase 0 之前或 Phase 0 过程中）

| # | 问题 | 建议位置 | 工作量 |
|---|------|---------|-------|
| B1 | System Prompt 策略 | 新增 §5.5 | 1-2 h |
| B2 | Token 预算算法 | 补充到 §3.4 ContextManager 注释 | 30 min |
| B4 | Session 事件类型 | 补充到 §3.4 SessionStore 之后 | 30 min |
| B5 | LiteLLM 版本锁定 | §12 的 `uv add` 加版本约束 | 10 min |

### 可以改善（Phase 1 过程中逐步完善）

| # | 问题 |
|---|------|
| A3 | Git checkpoint 技术实现细节 |
| A4 | `stream_chat` 返回类型注释 |
| B3 | 错误恢复矩阵 |
| C1 | tool call 上限从 25 调整为 10 |
| C2 | AGENTS.md 合并策略 |
| C3 | §12 默认模型名确认 |
| C4 | `requires_approval` 改为动态决策注释 |

---

## 结论

v0.2 文档质量已经可以支持 Phase 0 启动。**修正 A1/A2 两个一致性问题后**（预计 30 分钟），即可开始 `prototype.py` 的编写。B 类问题建议在 Phase 0 过程中逐步补充到文档中，不需要阻塞启动。
