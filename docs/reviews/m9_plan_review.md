# M9 Plan Review — tree-sitter 符号索引

**Review 对象**: Phase 2 M9 实施计划(tree-sitter 符号索引,~600 LOC)
**Reviewer**: Claude
**Date**: 2026-07-12
**Verdict**: ✅ 方向正确、质量高 —— 3 处与设计决策不一致建议开工前修正,若干实现细节可开工时定

---

## 总评

plan 结构清晰、范围克制(不做 repo-map / 符号工具 / LSP)、工程细节扎实(依赖版本约束精确到 Rust/PyO3 重写线、SQLite schema、写工具失效 + 查询时校验双层防护、PR 拆分合理)。方向与之前 design 讨论一致(M9/M10 对调后 M9 = 索引地基)。

但有 **3 处与 design 阶段已定决策不一致**(dataclass 缺 precision、signature 提取未定、配置 list 语义不清),建议开工前补齐——尤其 dataclass 字段是破坏性变更,晚改成本高。

---

## 🔴 重要(开工前必修)

### A. `SymbolDef` / `SymbolRef` 缺 `precision` 字段

> [!WARNING]
> design 阶段明确决定:符号工具结果带 `precision: syntactic | semantic` 标签,让模型知道答案来自 tree-sitter(句法)还是 LSP(语义)。但 plan 的 dataclass 定义里**没有这个字段**。

plan 当前的数据结构:

```python
SymbolDef(path, line, name, kind, signature)   # ← 无 precision
SymbolRef(path, line, name)                     # ← 无 precision
```

**后果**:Phase 3 加 LSP 后端时,要么改 dataclass(破坏性,所有调用点 + JSONL 事件 schema 跟着改),要么只能从 backend 类型推断精度(丢失"逐查询标注"的细粒度)。

**建议**:dataclass 一步到位预留字段:

```python
@dataclass(frozen=True)
class SymbolDef:
    path: str
    line: int
    name: str
    kind: str
    signature: str
    precision: Literal["syntactic", "semantic"]  # M9 永远填 "syntactic"
```

M9 的 tree-sitter 后端永远填 `"syntactic"`,零运行时成本,Phase 3 挂 LSP 时只需在 LspBackend 填 `"semantic"`,Protocol 不动。这正符合 plan 自己引用的"工程原则 #2:每个核心模块从 Protocol 开始"——Protocol 的数据结构应一步到位。

---

### B. `signature` 字段的提取方式没定

> [!WARNING]
> `SymbolDef.signature` 是 repo-map(M10)渲染里**最有价值的信息**(函数签名让模型不读文件就知道 API 形态),但 plan 没说怎么提取。

tree-sitter 的 `tags.scm` 查询(Aider vendor 的)主要提取**符号名 + kind**(如 `@name.definition.function`),**不直接给完整签名**(参数列表、返回类型)。extract.py 怎么拿 signature?三种可能:

| 选项 | 实现 | 精度 | 成本 |
|---|---|---|---|
| 1. 读符号所在行 | `lines[node.start_point.row]` | 低(多行签名截断) | 极低 |
| 2. 解析 def 节点文本 | 取节点到 `:` / `{` 的文本 | 中 | 每语言写规则 |
| 3. 扩 tags.scm query | 加 `(method (parameters) @signature)` 之类 | 高 | 查询生态要改 |

plan 没选。**建议开工前定**:优先选项 3(扩 query,精度最高且 tree-sitter 原生支持),fallback 选项 1(读行,够用于 M10 map 渲染)。这个决策直接影响 M10 repo-map 质量——签名拿不全,map 退化成"文件名 + 函数名列表",失去 Aider 式 map 的核心价值。

---

### C. 配置 `list` 值在 M9 的行为不清

> [!WARNING]
> design 阶段把 `symbol_backend` 升级为 `str | list[str]`(fallback 链,如 `[lsp, treesitter]` = LSP 优先、fallback tree-sitter)。但 plan 的校验只覆盖标量。

plan 写:

> 合法值校验 `treesitter | off`(`lsp` 值预留,Phase 2 给出"未实现"报错)

**未覆盖的场景**:用户配 list 时 M9 怎么响应?

| 配置 | M9 期望行为 |
|---|---|
| `[treesitter]` | 等价标量 `treesitter` |
| `[off]` | 等价标量 `off` |
| `[lsp, treesitter]` | ?(lsp 未实现) |
| `[treesitter, lsp]` | ?(lsp 未实现) |
| `[]` | 空列表语义? |

**建议 plan 明确**:M9 接受 `[treesitter]` / `[off]` 及等价标量;**任何含 `lsp` 的 list 报友好错误**("LSP 后端 Phase 3 实现,当前仅 treesitter")。配置 schema(`str | list[str]`)一步到位,Phase 3 挂 LSP 时只放宽校验,不改类型——这跟 plan 的"配置解析支持 `str | list[str]` schema"表述一致,但校验行为要补全。

---

## 🟡 中等(建议开工前定,否则实现期返工)

### D. SQLite 实战细节缺失

plan 的 schema 描述够用,但漏了 4 个实战点:

1. **WAL 模式**未提——SQLite 默认 `journal_mode=delete`,建议 `PRAGMA journal_mode=WAL`(读不阻塞写,查询 <100ms 验收更稳)
2. **sha256 全算成本**——`files` 表有 sha256 字段,10k 文件首次 build 要算 10k 次 sha256(IO 密集)。建议 **mtime + size 双校验为主,sha256 只在 mtime 可疑时算**(罕见 touch 场景)。否则首次 build <30s 的验收可能被 sha256 IO 吃掉
3. **path 存储**绝对/相对未定——建议存**相对 workspace 路径**(可移植,workspace 移动不破索引)
4. **invalidate 的"懒重解析"语义**——查询时同步重解析(阻塞,可能破 100ms)还是异步重建?未说清。建议查询时**只做 mtime 校验**,过期文件的重建走 invalidate 队列(下一轮查询前异步处理),避免单个查询被慢解析拖死

### E. `apply_patch` 的 `paths` 提取未说

plan 说 `write_file` / `edit_file` / `apply_patch` 写成功后调 `ctx.indexer.invalidate(paths)`。前两者 `paths` = 单文件,直接。但 **apply_patch 可能改多文件**,`paths` 从哪来?

- patch 解析出的文件列表(hunks 涉及的文件)?
- plan 没说 apply_patch 怎么收集 paths

**建议**:plan 补一句"apply_patch 的 paths = 解析出的所有 hunk 目标文件"。

### F. vendor `tags.scm` 的合规

plan 说"vendor 自 Aider 的 tags.scm,保留 license 头"。两点补充:

1. **仓库 NOTICE / THIRD-PARTY 文件**——Apache-2.0 项目 vendor MIT 代码,通常要在 NOTICE 声明第三方版权 + 来源。光在 .scm 文件头留 license 不够
2. **vendor 来源锁版本**——应记录"来自 Aider vX.Y.Z commit abc123",方便后续追踪上游更新 + 归属明确

---

## 🟢 小(实现期消化)

- **G. 验收措辞混淆**:验收 #1 "find_symbol 查询 <100ms" 容易跟 M11 的 `find_symbol` **工具**混淆(M9 不注册工具)。改成 "`SymbolBackend.find_symbol()` 调用 <100ms" 更准
- **H. 验收项目未指定**:"5 个真实 Python 项目" / "10k 文件仓库" 没指名——建议明确(如 cpython / fastapi / 本仓库),避免验收时挑容易的
- **I. import-linter 概念混淆**:plan 说"EXPECTED_SUBMODULES 加 krodo.indexer" + "import-linter 落地按 CONTRIBUTING Phase 2 CI 计划"。两者是不同东西——`EXPECTED_SUBMODULES` 是子模块**列表**测试,import-linter **contract** 是依赖方向规则。M9 只动前者,后者仍是 deferred,别在 plan 里把它们讲成一件事
- **J. 依赖 license 审计未写明**:实际 `tree-sitter` / `tree-sitter-language-pack` 都 MIT,但 plan 应显式说"已确认 MIT,与 Apache-2.0 兼容"(工程原则 #7)
- **K. 打包体积影响**:`tree-sitter-language-pack` 预编译 wheel ~2MB,对 `uv tool install krodo` 体验有影响,plan 没评估
- **L. extract.py 鲁棒性**:文件大小上限(参考 `agents_md._READ_LIMIT_BYTES`)、编码处理(utf-8 errors=)、二进制误判防护——未提

---

## ✅ 亮点(确认做对的)

| 决策 | 评价 |
|---|---|
| 依赖版本约束精确到 `tree-sitter-language-pack>=1.12` | ✅ 避开旧线 macOS arm64 死锁,有据 |
| 依赖预算检查(13 → 15,到 architecture §0.3 上限) | ✅ 守纪律 |
| 写工具失效 + 查询时校验**双层防护** | ✅ run_shell 漏的钩子靠查询兜底 |
| `ToolContext.indexer` 默认 None | ✅ 现有单测零改动,向后兼容 |
| INDEX_BUILD/UPDATE 事件 replay noop(同 COST_SNAPSHOT) | ✅ resume 逻辑自洽 |
| PR 拆分(PR1 indexer 核心独立,PR2 接线) | ✅ 回滚粒度好 |
| M9/M10 对调 + 范围克制(不做 map/工具/LSP) | ✅ 单一数据地基,消费方在 M10/M11 |

---

## Summary

| 维度 | 评价 |
|---|---|
| 方向 / 范围 | ✅ 正确,与 design 决策一致 |
| 工程细节深度 | ✅ 依赖 / schema / 失效链路扎实 |
| 与 design 决策一致性 | ⚠️ 3 处不一致(precision / signature / config list) |
| 实现细节完备性 | 🟡 SQLite 4 点 + apply_patch paths + vendor 合规 |
| 验收可测性 | ✅ 6 条具体(措辞小修) |
| 最大风险 | dataclass 缺 precision(Phase 3 破坏性变更) |

---

## 建议下一步

1. **开工前修 A/B/C 三点**——尤其 A(precision 字段),dataclass 变更是破坏性的,越晚改成本越高。建议在 plan 的 `base.py` dataclass 定义 + config 校验两处补齐
2. **D-F 开工时定**——SQLite pragma / sha256 策略 / apply_patch paths / vendor NOTICE,实现期第 1-2 个 commit 里就定下来,避免返工
3. **G-L 实现期消化**——措辞 / 验收项目 / 鲁棒性细节,边写边补
4. **本 review 归档**——放 `docs/reviews/`,跟 `m8_review.md` 同级,作为 M9 决策记录

如果认可这些点,可以直接基于本 review 修订 M9 plan 后开工。
