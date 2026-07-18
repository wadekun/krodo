# M9 收尾四步 Plan — Review

**Review 对象**: Phase 2 M9 收尾计划(PR3 pin 收窄 + 上游 issue + 金丝雀防线 + 文档同步/关闭里程碑)
**Reviewer**: Claude
**Date**: 2026-07-18
**Verdict**: ✅ 方向正确、范围克制、优先级合理 —— 1 处开工前应修正的诊断措辞(经实测验证),1 处 canary 设计可改进,若干小点。整体可执行。

---

## 总评

四步是收尾该做的四件事;PR3 pin 收窄排第一(修的是"所有新安装默认开启符号索引即启动崩溃"的普遍问题,不是平台特例防御)——优先级对。规则 #7 的依赖改动用"计划获批即视为批准"处理,机制妥当。canary 设计(子进程隔离 + 失败禁用)、上游 issue 起草后交用户自行提交(不代发)、"不做每文件子进程隔离"(fork 开销报销 30s 冷构建目标)、"不重跑性能基准"(验收已闭合)—— 这些判断都对。

但 review 中实测出**一个计划与既有 benchmark 文档都默认、却没人核过的点**,它修正了根因措辞;另有 canary 采样策略值得改。详见下。

---

## 🔴 重要(开工前修正)

### A. 根因不是"grammar ABI 不匹配",是"tree-sitter 0.26 query 引擎崩"

计划背景与 `docs/benchmarks/m9_symbol_index_perf_results.md` 都把根因写成 *"language-pack 1.12.5 预编译 grammar 与 tree-sitter 0.26 运行时 **ABI 不匹配**"*。Review 时实测核了一个关键点 —— **parse 不崩,query 才崩**:

```
# tree-sitter 0.26.0 + language-pack 1.12.5,对崩溃文件 httpx/_models.py
parser.parse(src)               →  ✅ 解析成功(root: module)
parser.parse + Query.captures   →  ❌ SIGSEGV(scan.py 实测,该文件在 24 崩溃之列)
```

**含义**:grammar 能正常产出语法树(parse OK),真正 SIGSEGV 的是 **tree-sitter 0.26 的 query 引擎(`QueryCursor.captures`)在匹配某些 query × 树组合时**。language-pack 1.12.5 恰好走标准 `Parser`+`Query` 路径(1.12.2 用自带 Rust wrapper 绕开了 0.26 的 C query 引擎,所以稳)。注意 Python 查询是**纯 pattern、无 `#select-adjacent!` 之类外来谓词**,故可排除"自定义谓词作怪"—— 是标准 query 匹配在某些 Python 树上崩。

**修复决策不变**(pin 收窄 `<0.26` 仍是解),但诊断归因要精确化,影响三处:

| 处 | 现状措辞 | 建议改为 |
|---|---|---|
| 上游 issue(step 2) | "grammar ABI mismatch" | "**tree-sitter 0.26 query engine regression**(`QueryCursor.captures`)on certain query×tree combos, exposed via the standard Parser+Query path" |
| CHANGELOG / commit(step 1) | `ABI mismatch with language-pack grammars` | `tree-sitter 0.26 query engine crashes (QueryCursor.captures); pin runtime <0.26` |
| benchmark results 文档 | "ABI 错位" | 同步为"0.26 query 引擎崩(parse 不受影响)" |

**对上游 issue 的关键影响**:最小复现**必须带 query**(parse-only 复现不了)。去 krodo 依赖后约 15 行即可:

```python
from tree_sitter_language_pack import get_parser
from tree_sitter import Query, QueryCursor
src = open("httpx/_models.py", "rb").read()
p = get_parser("python")
tree = p.parse(src)                       # OK
q = Query(p.language, open("python-tags.scm").read())
QueryCursor(q).captures(tree.root_node)   # SIGSEGV under 0.26; OK under 0.25.2
```

> 注:即便归因到 query 引擎,上报对象仍以 Goldziher/tree-sitter-language-pack 为主(它是"路由到标准 Parser+Query"的触发方),可同时在正文点出 py-tree-sitter 0.26 的 query 引擎嫌疑,便于上游转交。

---

## 🟡 中等(开工前定,否则实现期返工)

### B. canary 采样:别用"最大的 3 个",改"前 N 个采样"

计划 step 3 写"选工作区最大的 3 个受支持源文件"。两个问题:

1. **要全量 walk + stat 才能定出最大的** —— 在 ha-core 这种 18k 文件仓库上给启动多加数百毫秒(计划"~100ms 一次 fork"低估了 pre-walk 成本)。
2. **统计上偏弱**:canary 的任务是探测"grammar 整体坏了"(高崩溃率,如本次 40%),不是定位那一个毒文件。对 40% 率,3 个样本漏检率 ≈ 0.6³ ≈ 22%(仅 ~78% 命中);而**取 walk 时前 ~16 个受支持文件**(不用 size-sort),命中概率 ≈ 1 − 0.6¹⁶ ≈ 99.9%,且更便宜(省掉排序)。

**建议**:canary 子进程解析"walk 时遇到的前 N(N≈16)个受支持源文件";任意一个信号死亡 → 警告 + 本 session 禁用索引。广采样 > 挑大的。

---

## 🟢 小(实现期消化)

- **C. step 1 验证范围**:`uv sync` 后跑**完整** `uv run pytest`,不只是 indexer 子集 —— 确认 0.25.2 下其它模块无回归(预期没有,但闭环)。顺手在本机 `scan.py ~/bench/httpx` 实弹验 0 crashers。
- **D. canary 是 best-effort 保险,不是保证**:pin 收窄才是真正消掉已知崩溃的那刀;canary 防的是**未来 tree-sitter 运行时版本**回归。计划里点明一句"残留风险:非采样命中的坏 grammar 仍可能在 `build_full` 崩",免得后来者以为加了 canary 就崩不掉了。
- **E. PR 结构措辞冲突**:step 3 说 canary 是"独立小 PR",step 4 又说"feature 分支走 PR 合入 main"(单数)。M9 未发布,PR1/PR2/PR3/canary/docs 作为一个 feature 分支大 PR 一起合是 OK 的 —— 把这句在 plan 里讲明白,别让"独立小 PR"误导。pin 修复没有"必须先于其它合"的紧迫性(无发布版本受影响)。
- **F. pin 未来锁死**:`tree-sitter<0.26` 会挡住 0.26+ 直到 language-pack 出 0.26 兼容版。加一句"待 language-pack 发布 0.26 兼容版后重新放宽"备忘,免得 bit-rot。当前 0.25.2 可用,风险低。
- **G. commit-message 卫生**:收尾各 commit 引用 `docs/benchmarks/*`(tracked,OK),但注意别在 commit/CHANGELOG 里滑入 `.cursor/` 路径(plan 文档本身可链,commit 不可)。

---

## 🔵 可选(顺带,不阻塞)

- **H. canary PR 顺手补 indexer.close() 收尾**:canary 要动 `_build_symbol_index` / `cli/main.py`,正好把已记在 M10 待办里的"`:resume` 切换 session 累积 SQLite 连接/WAL 句柄、无收尾关闭路径"一起补(`_run_headless` 末尾、`run_repl` 退出、`repl_session_cycle` 每次 `_rebuild` 前关旧 `indexer`,注意 None-guard)。同处代码、约 3 行。要保持 canary PR 最小就略过,留 M10。
- **I. `INDEX_UPDATE` dangling 枚举**:另一条 M10 待办,不属于 M9 收尾,留 M10。

---

## ✅ 亮点(确认做对的)

| 决策 | 评价 |
|---|---|
| PR3 pin 收窄排第一 | ✅ 修普遍启动崩溃,不是平台特例 |
| 规则 #7 用计划获批即批准 | ✅ 机制正确 |
| 不做"每文件子进程"隔离 | ✅ fork 开销报销 <30s 冷构建 |
| canary 用子进程探测 + 失败禁用 | ✅ 方向对(只需改采样策略,见 B) |
| 上游 issue 起草交用户、不代发 | ✅ 外向动作边界守住了 |
| 不重跑性能基准(验收已闭合) | ✅ 范围克制 |
| CI benchmark job 顺延 M10 | ✅ 已记 M10 待办 |

---

## Summary

| 维度 | 评价 |
|---|---|
| 方向 / 范围 / 优先级 | ✅ 正确 |
| 诊断准确性 | ⚠️ 根因措辞需修正(parse 不崩、query 崩;非 grammar ABI) |
| canary 设计 | 🟡 采样策略改"前 N 个" |
| 实操细节完备性 | 🟡 验证范围 / PR 结构措辞 / pin 备忘 |
| 最大风险 | 上游 issue 用错归因 → 贻误正确修复方向(故 A 必须开工前修) |

---

## 建议下一步

1. **开工前修 A**(根因措辞:parse vs query)—— 影响 CHANGELOG/commit/issue/results 四处措辞,晚改成本高。
2. **定 B**(canary 采样:前 N 个 vs 最大 3)与 **H**(indexer.close() 是否捆进 canary PR)。
3. 认可后从 step 1(PR3 pin 收窄)开干:改 pin → `uv sync` → 全量 pytest + scan.py 实弹 → CHANGELOG(用修正后的措辞)→ 提交。
4. 上游 issue 草稿带 A 的精确归因 + 15 行最小复现,交用户 review 后自行提交。

如果认可 A/B(及可选 H),可据此先把 plan 改到位再开工,或直接带着这些修订进入实现。

---

# 二次 Review(根因修正后,2026-07-19)

**背景**:首次 review 后,作者补做判别实验把根因又修正了一层 —— 既不是"grammar ABI 错位"(原文),也不是本 review 🔴 A 给的"tree-sitter 0.26 query 引擎回归",而是 **py-tree-sitter 0.26.0 绑定层 `Point` 引用计数 use-after-free**(上游 [py-tree-sitter#466](https://github.com/tree-sitter/py-tree-sitter/pull/466),2026-07-08 已合入 master、未发版;[#472](https://github.com/tree-sitter/py-tree-sitter/issues/472) 同症状 closed as duplicate,原话 "Pinning `tree-sitter>=0.25,<0.26` is a complete workaround")。作者已据此更新 closeout plan 与 `docs/benchmarks/m9_symbol_index_perf_results.md`(末节「根因二次修正」)。本节复核更新后的 plan。

## 本 review 的独立复核

为避免再次误判(前两次归因都不准),实测核了根因与触发条件(同 harness,tree-sitter 0.26.0 + language-pack 1.12.5):

| 输入 | 结果 | 含义 |
|---|---|---|
| httpx `_models.py`(真实崩溃文件) | ❌ exit −11 | harness 能复现真实崩溃(基线) |
| 合成:1 个 def @row 300(>256) | ✅ OK | 单个高行号节点**不**触发 |
| 合成:250 个 def,全在 row ≤256 | ✅ OK | 全缓存值,无悬空引用 |
| 合成:600 个 def,跨 row 0–599(多数 >256) | ❌ exit −11 | **大量**非缓存 Point 读取才崩 |

→ **py-tree-sitter 0.26 归因成立**(官方 grammar 同样崩,language-pack 洗清);pin `<0.26` 是上游确认的 complete workaround。原 🔴 A 推荐的"改写为 query 引擎回归"措辞**作废** —— captures 本身正常返回,崩在迭代节点属性时(作者实测),根因是 `Point.row/column` 的 UAF。

## 🟡 一处措辞要再修(本 review 新发现,不影响任何决策)

plan 背景与 results 文档都写 **"行/列号 >256 才触发"**。上表证伪了这个"才"—— **单个** row>256 的节点不崩;要**大量**非缓存(>256)的 Point 读取(真实多符号、跨 256 行的文件)才让 UAF 真正破坏堆并崩。准确表述:

> 触发条件 = **多次** `Point.row/column` 读取到非缓存值(>256),堆压力累积到 UAF 落地;单个高行号节点不够。">256"是必要条件,非充分。

这反而把"为什么单测/合成不崩、真实大文件崩 40%、跨平台同质"解释得更干净:单测符号少(不只是"<256 行");真实文件符号多且跨高行;绑定层 bug 自然平台无关。

**影响面(纯措辞)**:
- `m9_symbol_index_perf_results.md` 「根因二次修正」里"行/列号 >256 才触发" → 改"需大量 >256 的 Point 读取;单个高行号节点不触发"。
- plan 第 3 步 canary rationale"本次 bug 还要求行号 >256 的真实文件" → canary 采真实工作区文件本就正确(真实崩溃型组合必是多符号高行),去掉/软化">256"依据即可,**canary 设计不用改**。

**决策全部不变**:pin `<0.26`、canary 前 16 真实文件、上游 issue 取消。

## 其余核对(均通过)

- **B–H 正确吸收进更新后的 plan**:前 N≈16 采样(B)、全量 pytest(C)、残留风险 docstring(D)、单 feature PR + canary 独立 commit(E)、pin 放宽备忘具体化到"#466 发版"(F)、commit 不引 `.cursor/`(G)、indexer.close() 捆 canary(H)。I(INDEX_UPDATE)留 M10,一致。
- **step 2 路径改写已验证**:`gh api repos/Goldziher/tree-sitter-language-pack` 返回 `full_name=xberg-io/tree-sitter-language-pack`(非 fork、GitHub 重定向存活)→ 7 处 `Goldziher/`→`xberg-io/` 改写正确。
- **step 2 取消上游 issue**:正确。根因在 py-tree-sitter(不在 language-pack),且 #466 已修未发版、#472 已 closed as duplicate —— 再报任一方都是错误归因或重复。留痕方式(在 #472 补一条跨平台复现评论)可选、由作者自行决定。
- **commit / CHANGELOG 措辞**:plan 第 1 步已用修正后归因(`pin tree-sitter <0.26 (0.26.0 Point refcount use-after-free, py-tree-sitter#466)`)—— 准确。

## 最终结论

**计划可执行(execution-ready)。** 修正后的根因(py-tree-sitter 0.26 `Point` UAF)上游实锤 + 本 review 独立复核双重确认;B–H 全部正确吸收;step 2 路径改写与 issue 取消均核实。

唯一开工前顺手改的:results 文档 + canary rationale 里"行/列号 >256 才触发"这一句措辞(纯文字,不挡 PR3)。改完即可进 step 1:pin 收窄 → `uv sync` → 全量 pytest + `scan.py` 实弹 → CHANGELOG → 提交。
