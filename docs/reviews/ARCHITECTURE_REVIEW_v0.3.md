# Architecture Review：`docs/architecture.md` v0.3

> Reviewer: AI Architect | Date: 2026-05-06 | Document Version: v0.3

---

## 总体评价

v0.3 是一份**可直接作为设计基线、启动 Phase 0 实现的高质量文档**。

v0.2 Review 提出的全部 14 项问题（A1-A4 / B1-B5 / C1-C4 / D3）均有明确回应。其中新增的 §3.4.1 Token 预算算法、§5.5 System Prompt 策略、§5.6 AGENTS.md 机制、§7.5 错误恢复矩阵四块内容质量尤其高——不仅补全了缺失的设计，而且给出了可直接指导实现的细节。

下面只列出**仍存在的跨节一致性问题**（均为 v0.2→v0.3 合并时遗留的 stale reference）和少量细化建议。

---

## A. 遗留的跨节一致性问题（v0.2→v0.3 合并未清理）

### A1. §0.2 仍写 "monorepo"——与 §6.1 矛盾

§0.2 第 21 行：

> 仓库结构按 monorepo（`apps/cli`、`packages/core`、`packages/tools` 等）组织。

§6.1 已明确改为 `src/coda/` 扁平结构，并在末尾显式拒绝了 monorepo 拆包。

**建议**：§0.2 这句改为"仓库结构按 `src/` layout 单包组织（详见 §6.1）"。

### A2. §8 Phase 1 任务 1.3/1.4 仍引用 `packages/` 路径

- 1.3："产出：`packages/llm` 的 LiteLLM 实现"
- 1.4："产出：`packages/core` 的 Session / Context / Agent Loop"

这两个任务是在 v0.1 写的，§6 已经改了但 §8 没同步。

**建议**：改为 `coda/llm` 和 `coda/core`（与 §6.1 目录结构一致）。

### A3. §1 子系统计数仍为 "9 个" 但实际列出 10 个

这是 v0.1 Review 就提出的问题（原 A3），两轮修订均未处理。

实际列表（逐条计数）：

1. CLI / TUI 交互层
2. Agent 编排层
3. 任务规划层
4. 工具执行层
5. 文件/代码操作层
6. Shell / 命令执行层
7. 沙箱与权限控制层
8. 上下文与记忆层
9. LLM Provider 抽象层
10. 可观测性 / 评估层

**建议**：要么将"9 个"改为"10 个"，要么合并"文件/代码操作层"与"Shell/命令执行层"为一个"代码与命令操作层"（因为 Shell 本质上也是一种工具，且两者都通过 Tool Dispatcher 调度）。

---

## B. 细化建议（不阻塞 Phase 0，可 Phase 1 中逐步完善）

### B1. §5.5.3 prompt_version 写入事件类型有误

原文：

> prompt 的 hash 写入 `SessionEvent(type=COMPRESSION).data.prompt_version`

`COMPRESSION` 是上下文压缩事件的类型，语义上不适合承载 prompt 版本信息。

**建议**：prompt version 应在 session 创建时记录（`SessionEvent(type=COST_SNAPSHOT)` 或新增 `SESSION_INIT` 类型），而非在压缩时记录。如果意图是"压缩后检查 prompt 是否变了"，应单独记录。

### B2. §6.1 "ruff 的 import-linter" 描述不够准确

原文：

> 用 `ruff` 的 `import-linter` 或自写 CI lint 强制以上规则

`import-linter` 是一个独立的第三方工具（https://import-linter.readthedocs.io/），不是 ruff 的插件。ruff 目前没有内置的模块依赖边界检查功能。

**建议**：改为"用 `import-linter` 定义模块依赖契约，或自写 CI 脚本检查 import 规则"。

### B3. §3.4 `RecoveryCoordinator` 在 §7.5 提及但未在 §3.4 或 §1 中注册

§7.5 末尾提到"由 `ContextManager` 与一个新的 `RecoveryCoordinator` 协作实现"。但 §3.4 的 Protocol 接口列表中没有 `RecoveryCoordinator`，§1 的子系统列表也没有。

**建议**：如果在 Phase 1 确实要实现 `RecoveryCoordinator`，在 §3.4 补充其 Protocol（接口可以很简单）。如果只是命名一个概念，改为"错误恢复逻辑集中在 `core/recovery.py` 实现"即可，不必须升格为独立 Protocol。

### B4. §5.4.1 修改性命令启发式可考虑引用 §5.3 的黑名单

§5.4.1 定义了一套"修改性命令"启发式（含 `>` `>>` `mv` `rm` `sed -i`），§5.3 的 `.codaignore` 有危险命令黑名单。两者有交集但不完全相同。

**建议**：在 Phase 1 实现时统一为一处 `CommandPolicy`，不要在两个地方各维护一份命令分类逻辑。这个可以在实现阶段处理，不需要改文档。

---

## C. v0.2 Review 各项的逐条验收

| Review 项 | v0.2 状态 | v0.3 处理 | 验收 |
|-----------|----------|----------|------|
| A1 §6 vs §12 矛盾 | 阻塞 | §6 重写为 `src/coda/`，显式拒绝 monorepo | **通过** |
| A2 工具数不一致 | 阻塞 | §9 改为 11 个 + 分类明细 | **通过** |
| A3 Git checkpoint 细节 | 需修正 | §5.4 拆为 4 个子节，命令链完整 | **通过** |
| A4 stream_chat 签名 | 讨论 | 加 docstring + 保留 `def`（合理） | **通过** |
| B1 System Prompt | 缺失 | §5.5 六层组成 + 注入策略 + 版本管理 | **通过** |
| B2 Token 预算 | 缺失 | §3.4.1 完整算法 + 实现要点 | **通过** |
| B3 错误恢复 | 缺失 | §7.5 八场景错误恢复矩阵 | **通过** |
| B4 Session 事件 | 缺失 | §3.4 `SessionEventType` + `SessionEvent` | **通过** |
| B5 LiteLLM 版本 | 缺失 | §12 加版本约束 + §11 原则 7 | **通过** |
| C1 tool call 上限 | 讨论 | 15 + 10 soft warning（合理折中） | **通过** |
| C2 AGENTS.md 细节 | 缺失 | §5.6 四子节（合并/限长/边界/缓存） | **通过** |
| C3 模型名 | 需确认 | 补日期后缀 + LiteLLM 命名说明 | **通过** |
| C4 requires_approval | 需细化 | 标注为默认值 + ApprovalManager 动态决策 | **通过** |
| D3 原则 6 措辞 | 建议 | 改为"三次跨模块重复才提取共用抽象" | **通过** |

**14/14 全部通过。**

---

## D. 新增内容的亮点

1. **§3.4.1 Token 预算算法**：固定开销 + 动态分配 + 触发动作三级模型，加上"压缩本身消耗 token 需记账"和"Claude 实测 ~1.1× tiktoken"这类实战经验，实现者拿到就能用。

2. **§5.5 System Prompt 策略**：六层组成结构附 token 估算，AGENTS.md 用独立 user message 注入以优化 prompt caching 命中率——这是一个经过实际性能考量后做出的精细设计。

3. **§5.6 AGENTS.md**：四级合并（系统/项目/子目录/用户）+ "不 override，让模型处理冲突"的设计哲学，简洁且务实。12K 总上限 + 按"项目级 > 子目录级 > 系统级"优先级截断的 fallback 策略清晰。

4. **§7.5 错误恢复矩阵**：8 个场景，每个都有"现象 → 处理"的完整描述。特别是"连续无进展"的死循环检测（3 次相同 tool call hash）和"并发文件修改冲突"的 hash 校验 + 重新读取策略，都是 coding agent 特有的高频痛点。

5. **§11 原则 7 核心依赖版本锁定**：从原则层面解决 LiteLLM 频繁更新的风险，比只在 `uv add` 里写版本号更可维护。

---

## 评分

| 维度 | v0.1 | v0.2 | v0.3 | 变化 |
|------|------|------|------|------|
| 完整性 | 8/10 | 8.5/10 | **9.5/10** | +Token 预算、System Prompt、AGENTS.md、错误恢复矩阵 |
| 正确性 | 7/10 | 8/10 | **9/10** | +A1-A4 全修；- §0.2/§8 stale reference |
| 可执行性 | 8/10 | 9/10 | **9.5/10** | +版本锁定、模型名、验收标准全面 |
| 一致性 | 9/10 | 7/10 | **8.5/10** | +§6/§12 统一；- §0.2/§8/§1 stale |
| 可维护性 | 8/10 | 8.5/10 | **9/10** | +变更日志极其详细、工程原则 7 条 |
| **综合** | **8.0** | **8.2** | **9.1** | |

---

## 结论

**v0.3 文档已达到可启动 Phase 0 的质量。**

剩余 3 个一致性问题（A1-A3）都是 v0.2→v0.3 合并时的 stale reference，**修正工作量合计 < 15 分钟**，不阻塞 Phase 0 启动。B 类 4 个细化建议可在 Phase 1 实现过程中逐步处理。

**建议下一步**：
1. 花 15 分钟修正 A1（§0.2 monorepo → src layout）、A2（§8 packages/ → coda/）、A3（§1 9→10）
2. 启动 Phase 0，写 `scripts/prototype.py`
3. Phase 1 过程中处理 B1-B4

同时建议把开发同学提到的"每次重大修订后跑一次跨节一致性检查"固化为流程——这正是 A1-A3 这类问题的根因。
