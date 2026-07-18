# M9 性能验收 — 结果报告(tree-sitter 符号索引)

**日期**: 2026-07-18
**krodo commit**: `e5b69f7`(M9 PR1 `4b2b62e` + PR2 `e5b69f7`)
**操作文档**: `docs/benchmarks/m9_symbol_index_perf.md`

## 结论(先说结果)

> **更新(2026-07-18,实验 1 后):阻断已解除,验收全部通过。** 根因不是"macOS 26 太新",而是 **`tree-sitter` 0.26 C 运行时与 language-pack 预编译 grammar 的 ABI 不匹配** —— 把运行时降到 0.25.2(grammar 包不动)后,40% 崩溃归零,基准在本机直接跑通。**修复是一行 pin 收窄:`tree-sitter>=0.25,<0.26`**(替换 M9 现有 `tree-sitter>=0.25,<1`)。最终验收数字见文末「最终验收」。下文保留首次阻断的完整诊断与 Review 分析作为历史。

**首次运行(已过时,见上更新):** 性能验收曾在本机被 `tree-sitter` 0.26 的原生 SIGSEGV 阻断 —— `build_full()` 在任意真实仓库上被信号杀死。这不是 M9 代码缺陷(M9 的单测与 smoke 在同样依赖下对非崩溃输入全部通过),而是第三方运行时与 grammar 的 ABI 错位(证据见下)。


## 验收项对照

| 验收项 | 阈值 | 状态 | 实测(3 轮,tree-sitter 0.25.2) |
|---|---|---|---|
| #1 查询延迟 | < 100ms(p95) | ✅ 通过 | 全部仓库 p95 ≤ 0.1ms(ha-core 176k 符号) |
| #2 冷构建 | < 30s @ 10k 文件 | ✅ 通过 | ha-core 18k 文件:22.3 / 22.4 / 26.9s |
| #2 单文件增量 | < 1s | ✅ 通过 | 最大 15ms(fastapi),ha-core 3–5ms |
| slow smoke(1k 合成) | 记录即可 | ✅ 通过 | 1k 文件 / 20k 符号,冷构建 ~0.2–0.3s |

## 环境

| 项 | 值 |
|---|---|
| 机器 | Mac14,7 / Apple M2 |
| CPU | 8 核 |
| 内存 | 16 GB |
| OS | macOS 26.5.2(Darwin 25.5.0) |
| Python | 3.13.2(基准运行环境;3.12.9 亦验证同样结论) |
| tree-sitter(首次,崩溃) | **0.26.0**(M9 现 pin `>=0.25,<1` 解析到此) |
| tree-sitter(最终,通过) | **0.25.2**(pin 收窄到 `>=0.25,<0.26`) |
| tree-sitter-language-pack | **1.12.5**(M9 pin `>=1.12,<2`,全程不变) |

## 现象

1. 基准脚本对每个仓库都 `exit 139`(SIGSEGV)或偶发 `exit 138`(SIGBUS),无一幸免 —— 包括最小的 httpx(60 个 `.py`)。
2. 崩溃在 `build_full()` 的解析阶段(原生代码),Python 无法 `try/except` 捕获,进程直接被信号杀死。
3. 输出表头因 stdout 在信号死亡时不刷新而丢失,最初看似"无输出"。

## 诊断与证据

### 1. 崩溃是**内容相关**的,非"解析 N 次后累积"

用**每文件一个子进程**的方式逐个解析 httpx 的 60 个 `.py`(脚本 `~/bench/scan.py`):

```
60 files, 24 crashers (40%)
  CRASH (-11): httpx/_models.py
  CRASH (-11): httpx/_client.py
  CRASH (-11): httpx/_urls.py
  ... (共 24 个)
```

**40% 的真实 Python 文件会让 Python grammar 原生 SIGSEGV**。崩溃点是确定性的(同一文件每次都崩),所以 `build_full()` 在扫到第一个崩溃文件时即被杀死 —— 这解释了"任意仓库都崩"。崩溃文件跨纯 Python(httpx 是纯 Python,无 JS/TS),排除了"某语言 grammar"的猜测。

### 2. 版本矩阵(在隔离子进程里测)

| tree-sitter-language-pack | tree-sitter | 稳定性(303 次解析) | M9 符号提取 | 可用性 |
|---|---|---|---|---|
| 1.11.0 | 0.26.0 | ✅ 稳定 | ❌ 0 符号 | 不可用(见 §3) |
| 1.12.0 | 0.26.0 | ✅ 稳定 | ❌ 0 符号 | 同上 |
| 1.12.1 | 0.26.0 | ✅ 稳定 | ❌ 0 符号 | 同上 |
| 1.12.2 | 0.26.0 | ✅ 稳定 | ❌ 0 符号 | 同上 |
| 1.12.3 / 1.12.4 | — | ⚠️ `uv` 解析冲突(unsatisfiable) | — | 装不上 |
| **1.12.5(pinned)** | 0.26.0 | ❌ **SIGSEGV(40% 文件)** | ✅ 正确(非崩溃输入) | **崩溃** |

> 三者都拉 `tree-sitter==0.26.0`,所以 **tree-sitter 本身不是差异点**;差异在 language-pack 自带的 grammar `.so` / Parser 包装。

### 3. 为什么稳定版本给 0 符号(API 漂移)

1.12.3+ 起 `get_parser()` 返回**标准 `tree_sitter.Parser`**(`parse()` 收 **bytes**)—— M9 的 `extract.py` 按这个写(`parser.parse(source: bytes)`),在 1.12.5 上符号提取完全正确(单测 + smoke 全绿)。

1.12.2 及更早返回 language-pack 自定义的 `builtins.Parser` 包装:

```
>>> parser.parse(b"def f(x): ...")
TypeError: 'bytes' object is not an instance of 'str'   # 要 str,不是 bytes
>>> parser.language
AttributeError: 'builtins.Parser' object has no attribute 'language'
```

M9 的 `extract_symbols` 把 bytes 传进去 → `TypeError` → 被 `try/except Exception` 吞掉 → 返回空 → **0 符号**。且该包装没有 `.language`,无法在外部重建 `Query`,所以"写个 str-input shim 绕过"也无法忠实复刻 M9 的提取路径(会变成针对另一套 API 的重实现,数字不再代表 M9)。M9 plan 自己也标注过这个 API 变更点("1.12.3+ 的 get_parser() 返回标准 Parser,parse() 入参 str vs bytes")。

### 4. 这不是 M9 代码问题

- M9 单测(`test_symbol_extract.py` / `test_symbol_index.py`,744 passed)在同样的 1.12.5 依赖下全绿 —— 对**非崩溃**输入,提取与索引逻辑完全正确。
- 崩溃发生在第三方 native 代码(tree-sitter-language-pack 的 grammar `.so`),`extract.py` 已有的鲁棒性(1MB / NUL / `try/except`)**挡不住 SIGSEGV**(信号绕过 Python 异常机制)。
- M9 的依赖契约(`tree-sitter>=0.25,<1` + `tree-sitter-language-pack>=1.12,<2`)在"正常平台"上应解析到可用的 1.12.x + 标准 Parser;本机是特例。

## 根因推断

最可能:**macOS 26.5.2(Darwin 25.5.0)是极新的大版本**;`tree-sitter-language-pack` 的预编译 wheel 按更旧的 macOS SDK 构建,在新系统上 native grammar 行为异常 → 内容相关 SIGSEGV。佐证:

- 同一 wheel(1.12.5)、同一 `tree-sitter` 0.26.0,40% 真实 Python 文件崩 —— 这种规模的崩溃若是 wheel 本身的普遍 bug,上游 issue 会铺天盖地,故更可能是**平台特定**(Apple Silicon + 极新 macOS)。
- M9 plan 早已记录 "tree-sitter-language-pack 旧线在 macOS arm64 有 native 死锁",说明该包在 Apple Silicon 上的 native 稳定性历史上有问题。

(无法在本机证伪 —— 需要换平台对照。)

## 复现(本机)

```bash
# 5 个项目 + 合成 1k 已就绪:
ls ~/bench/{fastapi,flask,httpx,django,ha-core}
# 基准脚本:
cat ~/bench/bench_symbol_index.py
# 复现崩溃(任一仓库都会 SIGSEGV):
cd <krodo 根>
uv run python ~/bench/bench_symbol_index.py ~/bench/httpx   # exit 139
# 量化崩溃率(每文件一个子进程):
uv run python ~/bench/scan.py ~/bench/httpx                  # 40% crashers
```

基准脚本与诊断脚本(`diag.py` / `scan.py` / `thresh.py` / `one.py`)均刻意放在 `~/bench`(krodo 仓库外),用 `print` 输出,不触发仓库的 ruff T20。

## 建议的下一步(按推荐度)

1. **换平台重跑(最优先)**。在 Linux x86_64 容器,或稳定的 macOS 15(Darwin 24)上跑同一基准脚本。若数字达标 → 确认是本机平台问题,M9 验收通过;把那份结果替换本文件。
2. **向上游反馈**。带 `~/bench/scan.py` 的复现(40% httpx 文件 SIGSEGV)报到 [xberg-io/tree-sitter-language-pack](https://github.com/xberg-io/tree-sitter-language-pack),注明 Apple M2 + macOS 26.5.2 + 1.12.5。(**已过时**:根因二次修正后确认问题不在此仓库,见文末「根因二次修正」——本条历史记录保留,不代表最终行动项。)
3. **M9 提取器加进程隔离兜底(可选防御性增强,不修 wheel)**。把"每文件解析"放进子进程,单文件 SIGSEGV 只丢该文件、不杀整个 `build_full()`。这能让 M9 在有崩溃型 grammar 的平台上**降级可用**(跳过崩溃文件继续索引),代价是构建变慢(不适合做 <30s 冷构建基准,但能让查询/增量功能在坏平台上不整盘崩)。属于 M9 鲁棒性增强,可单独一个 PR,不阻塞 M9 本身。
4. **等 wheel 修复版**(1.12.6+ 或上游修 macOS 26 兼容)后,在原 pin `>=1.12,<2` 下重跑。

## 附:操作过程中排除的干扰项(避免后来者重踩)

- **`/tmp/bisect.py` 影子化 stdlib `bisect`**:诊断中途我曾把脚本命名为 `/tmp/bisect.py`,从 `/tmp` 运行脚本时 `sys.path[0]=/tmp`,`random/tempfile/importlib` 链式 `import bisect` 命中我的文件而非 stdlib,制造了一批假崩溃与 `ImportError`。已删除。**教训:别在会进 `sys.path` 的目录里放与 stdlib 同名的脚本**(`bisect`/`random`/`test`/`string` 等)。
- **`uv run ... | tail` 的退出码**:zsh 下 `echo $?` 取的是管道最后一个命令(tail)的码,不是 python 的;诊断退出码时必须用重定向 `> file 2>&1; echo $?` 或 `$PIPESTATUS`。

---

# Review 分析结论(2026-07-18 追加)

**Reviewer**: Claude(基于本报告 + M9 代码 review 上下文)

对报告诊断方法与"非 M9 代码缺陷"的结论**认可**(子进程隔离量化、版本矩阵、干扰项排除均规范;SIGSEGV 绕过 Python 异常机制,`extract.py` 的鲁棒性防线本来就挡不住信号级崩溃)。在此之上补充两个报告未覆盖的关键点,并修订行动顺序。

## 关键点一:这是生产可用性风险,不只是基准阻断

报告把问题框定为"性能验收被阻断",但往前推一步:

- `symbol_backend` 默认值是 `treesitter`(**默认开**);
- `build_full()` 在 session 组装期**同进程**运行;
- `_build_symbol_index` 的"build 失败降级为无索引" `try/except` **对 SIGSEGV 无效**——信号直接杀进程。

因此在受影响的平台上,**任何用户 `krodo` 一启动就会在建索引阶段整个进程被杀死**。40% 崩溃率意味着几乎所有真实 Python 项目都会命中——这台机器不是特例样本,而是 v0.2 发布后部分真实用户环境的预演。

**结论:无论根因归到哪里,M9 关闭前需要一道信号级防线**,从轻到重:

1. **金丝雀探测(建议 M9 内做)**:首次 `build_full` 前,先在子进程里解析少量代表性文件(如工作区最大的 3 个 `.py`);子进程非零退出 → 警告 + 本 session 禁用索引。一次 fork ~100ms,保住"默认开"的安全性。
2. **worker 池隔离(根因短期修不掉时再做)**:解析放常驻子进程,崩溃时重启 worker、跳过毒文件继续。**不要**做"每文件一个子进程"——10k 文件 × fork 开销会报销 <30s 验收目标;池化只在崩溃时付重启成本。

## 关键点二:版本矩阵漏了 tree-sitter 运行时维度(5 分钟判别实验)

矩阵五行全部固定 `tree-sitter==0.26.0`,只变 language-pack。但崩溃的结构值得注意:**1.12.2 的 vendored Parser(Rust 运行时)解析同样文件稳定;1.12.5 的标准 Parser(py-tree-sitter 0.26 的 C 运行时驱动 pack 编译的 grammar)崩溃**——变化的不只是 wheel,还有"谁在驱动 grammar"。

这指向与"macOS 26 太新"并列的第二假设:**pack 预编译 grammar 与 tree-sitter 0.26 C 运行时的 ABI 不匹配**(Python grammar 带 C 外部扫描器,ABI 错位的典型症状正是内容相关段错误)。旁证:pack 1.12.x 在 PyPI 声明的约束宽至 `tree-sitter>=0.23`,显然未对每个运行时版本做过矩阵验证;M9 的 pin `>=0.25,<1` 让 uv 解析到了 0.26.0。

**判别实验(本机,5 分钟)**:

```bash
# 强制降级运行时,grammar 包不动
uv pip install "tree-sitter>=0.25.2,<0.26"
uv run python ~/bench/scan.py ~/bench/httpx
```

| 实验结果 | 结论 | 修复 |
|---|---|---|
| 崩溃消失 | ABI 假设成立(0.26 运行时 × pack grammar) | 一行 pin 收窄 `tree-sitter>=0.25,<0.26`,本机即可重跑基准,不用换平台/等上游 |
| 照崩 | 平台假设(macOS 26)概率大增 | 走原报告路线:Linux 重跑 + 上游 issue |

两个假设的预言恰好相反(ABI 假设预言 Linux 同样会崩;平台假设预言 Linux 正常),所以**实验 1 + Linux 重跑合起来即可完全定案**。

## 修订后的行动顺序(替代原报告"建议的下一步")

1. **tree-sitter 0.25 降级实验**(本机,5 分钟)——两假设的判别实验,成功路径最短。
2. **Linux 重跑**(容器或 CI)——无论实验 1 结果如何都做:要么拿验收数字,要么完成假设判别。可顺手在 CI 加手动触发的 benchmark job,供 M10/M11 改热路径后复跑。
3. **上游 issue**(带 `scan.py` 复现 + 实验 1 结果)——若 ABI 假设命中,"0.26 兼容性"就是给上游的关键信息。
4. **金丝雀防线进 M9**——即使实验 1 修好本机,"pack grammar × 未来运行时版本"这一风险类别依然存在,默认开的功能需要这道保险。
5. **M9 里程碑保持开放**,验收数字拿到后再关;PR1/PR2 两个 commit 留在 feature 分支不动。

---

# 实验 1 结果与最终验收(2026-07-18)

执行 Review 修订行动顺序的第 1 步:language-pack **1.12.5 不变**,仅把 `tree-sitter` 运行时从 0.26.0 降到 0.25.2,重跑。

## 判别实验:scan.py on httpx

```
language-pack 1.12.5 + tree-sitter 0.26.0  ->  60 files, 24 crashers (40%)   ❌
language-pack 1.12.5 + tree-sitter 0.25.2  ->  60 files,  0 crashers (0%)    ✅
```

**(API 完整性已先验:** 0.25.2 下 `extract_symbols` 对样例返回 2 defs + 1 ref,与 0.26.0 一致 —— 故"0 崩溃"不是 API 漂移假阳性,grammar 确实不再崩。**)**

→ **ABI 假设确认;平台("macOS 26")假设证伪。** 根因是 pack 预编译 grammar(含 Python 的 C 外部扫描器)与 tree-sitter 0.26 C 运行时的 ABI 错位,典型症状正是内容相关段错误。这也预言 Linux + 0.26 会同样崩 —— 故**单纯换平台不可靠**,正解是收窄 tree-sitter pin。

## 最终验收数字(3 轮,tree-sitter 0.25.2 + language-pack 1.12.5)

| repo | files | symbols | cold build | warm build | q p50(ms) | q p95(ms) | q max(ms) | incremental |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| krodo | 101 | 1,556 | 0.1–0.2s | 14–15ms | 0.0 | 0.0 | 0.1–0.2 | 1ms |
| flask | 83 | 1,754 | 0.1s | 7ms | 0.0 | 0.0–0.1 | 0.1–0.5 | 1ms |
| httpx | 60 | 1,335 | 0.1s | 3–4ms | 0.0 | 0.0 | 0.1–0.4 | 1ms |
| fastapi | 1,131 | 6,774 | 0.7–1.0s | 77–78ms | 0.0 | 0.1 | 0.4–3.7 | 7–15ms |
| django | 3,038 | 48,901 | 4.3–5.1s | 299–321ms | 0.0 | 0.0–0.1 | 0.2–0.6 | 0–1ms |
| **ha-core** | **18,000** | **176,434** | **22.3 / 22.4 / 26.9s** | 785–842ms | 0.0 | 0.1 | 0.2–1.2 | 3–5ms |
| synthetic 1k | 1,000 | 20,000 | 0.2–0.3s | 21–22ms | 0.0 | 0.0–0.1 | 0.1 | 0ms |

**四项验收全部达标**(对照表见文首「验收项对照」)。ha-core 冷构建 3 轮均 < 30s(最差 26.9s,首次冷缓存);查询 p95 全部 ≤ 0.1ms(name 索引命中);单文件增量全部 < 20ms。

## 修复(待批准)

`pyproject.toml` 一行收窄(替换现有 `tree-sitter>=0.25,<1`):

```diff
- "tree-sitter>=0.25,<1",
+ "tree-sitter>=0.25,<0.26",
```

按工程规则 #7,依赖版本改动需显式批准 —— 本报告即为此批准请求。理由:(1) ABI 错位使 0.26 在受影响平台不可用;(2) language-pack 1.12.5 声明 `tree-sitter>=0.23`,0.25.2 完全兼容;(3) tree-sitter 仅被 indexer(M9)使用,无其它消费者受影响;(4) 收窄后基准在本机即达标,无需换平台。

## 仍建议(独立于本修复)

- **金丝雀防线进 M9**:即便 pin 收窄修好当前版本,"pack grammar × 未来 tree-sitter 运行时"这类原生崩溃风险类别仍在,而 `symbol_backend` 默认开、`build_full` 同进程跑、`try/except` 挡不住 SIGSEGV —— 默认开的功能需要一道子进程级金丝雀(首次建索引前 fork 探测少量文件,失败则本 session 禁用索引)。可单独 PR。
- **上游 issue**:带 `~/bench/scan.py` 复现 + 本实验数据(0.26 崩 / 0.25.2 不崩)报 [xberg-io/tree-sitter-language-pack](https://github.com/xberg-io/tree-sitter-language-pack),"0.26 运行时兼容性"是给上游的关键信息。(**已过时并取消**:根因二次修正后确认问题在 py-tree-sitter 而非此仓库,且上游已 root-cause 并修复,见文末「根因二次修正」。)
- **CI benchmark job**:Review 行动顺序第 2 点 —— 加手动触发的 benchmark workflow,M10/M11 改热路径后复跑(届时 tree-sitter pin 已是 0.25.x)。

---

# 根因二次修正(2026-07-18,closeout plan review 后实测)

上文"ABI 假设确认"的结论**归因有误,再修正一层**。closeout review 质疑"grammar ABI"措辞后,补做了判别实验:

## 判别实验(macOS 本机,tree-sitter 0.26.0)

```
parse                                    ✅ OK
Query 编译 + QueryCursor.captures        ✅ OK(8 组捕获正常返回)
迭代捕获节点(.start_point/.text/.parent) ❌ 中途 SIGSEGV(exit 139,3/3)

官方 tree-sitter-python 0.25.0 grammar + 0.26   ❌ 同样 3/3 崩   ← 排除 language-pack
官方 tree-sitter-python 0.25.0 grammar + 0.25.2 ✅ 3/3 过
同一迭代逻辑:内联 python -c 崩 / 脚本文件不崩 / cursor 保活不崩  ← 堆布局敏感
```

grammar 换成官方发行版崩溃依旧 → **language-pack 不是肇事方**;崩溃点随堆布局漂移 → 不是确定性 query bug,是**堆内存损坏(use-after-free)**。

## 上游实锤

- [py-tree-sitter#466](https://github.com/tree-sitter/py-tree-sitter/pull/466):0.26.0 将 `Point` 重构为 tuple 子类时,`Point.row/column` getter 返回借用引用,行/列号 int 被过早释放 → 堆损坏 → 崩溃落点漂移。**2026-07-08 已合入 master,未发版**(PyPI 最新仍为 0.26.0)。
- [py-tree-sitter#472](https://github.com/tree-sitter/py-tree-sitter/issues/472):同症状 issue(符号索引负载、Linux x86_64、0.26 崩 / 0.25.2 干净),已 root-cause 并 closed as duplicate;原话 "Pinning `tree-sitter>=0.25,<0.26` is a complete workaround"。
- 触发条件(二次 review 判别表 + 独立复跑修正):需**大量**非缓存(行/列号 >256,逃出 CPython 小整数缓存)的 `Point.row/column` 读取,UAF 堆损坏累积后才崩;**单个高行号节点不触发**——">256" 是必要非充分条件。判别表:1 个 def @row 300 ✅ 不崩;250 个 def 全 ≤row 256 ✅ 不崩;600 个 def 跨 row 0–599 ❌ 3/3 崩(0.25.2 对照 ✅)。这解释了"真实多符号大文件才崩(40%)、单测/合成小文件不崩(符号少,不只是行数短)、跨平台跨架构同质复现"(纯绑定层 bug,与平台、架构、grammar 无关)。

## 对既有结论的影响

| 结论 | 修正后状态 |
|---|---|
| pin 收窄 `tree-sitter>=0.25,<0.26` | ✅ 不变,仍是唯一正确修复(上游确认的 complete workaround) |
| 性能验收数字(0.25.2) | ✅ 不变,全部有效 |
| "language-pack grammar ABI 错位" | ❌ 归因错误 → py-tree-sitter 0.26.0 `Point` refcount use-after-free |
| "1.12.2 稳因 vendored Rust wrapper 绕开 ABI" | 重新解释:绕开的是 py-tree-sitter 的 `Point`/`Node` 绑定代码路径 |
| Linux 重跑"验证 ABI 假设跨平台" | 数据仍有效,含义改为:证明 bug 平台无关(绑定层 bug 的自然推论) |
| 向 xberg-io 报 issue | ❌ 取消(错误归因);py-tree-sitter 侧已修复待发版,亦无需重复上报 |
| pin 放宽条件 | py-tree-sitter 发版包含 #466(0.26.1+)后,`scan.py` 验证通过即可放宽到 `<0.27` |

---

# Linux 跨平台验证(2026-07-18,scan.py 对照)

按 `docs/benchmarks/m9_symbol_index_linux_rerun.md` 跑 `scan.py` 崩溃率对照,验证 ABI 假设是否跨平台成立(非性能基准,非 M9 验收项 —— 仅供上游 issue 引用)。

## 结果(httpx 60 个 `.py`)

| 环境 | language-pack | tree-sitter | crashers | 与 macOS 一致? |
|---|---|---|---|---|
| macOS arm64(宿主机) | 1.12.5 | **0.26.0** | **24 / 40%** | —(基线) |
| macOS arm64(宿主机) | 1.12.5 | **0.25.2** | **0 / 0%** | —(基线) |
| **Linux arm64(原生)** | 1.12.5 | **0.26.0** | **24 / 40%** | ✅ |
| **Linux arm64(原生)** | 1.12.5 | **0.25.2** | **0 / 0%** | ✅ |
| **Linux amd64(QEMU 模拟)** | 1.12.5 | **0.26.0** | **24 / 40%** | ✅ |
| **Linux amd64(QEMU 模拟)** | 1.12.5 | **0.25.2** | **0 / 0%** | ✅ |

四个对照格全部命中 ABI 假设预言(0.26 崩、0.25.2 不崩),且**崩溃文件清单与 macOS 逐字一致**(同一批 24 个 httpx 文件)。Linux arm64 原生与 Linux amd64(模拟)结果相同 → ABI 错位与 CPU 架构无关。

> amd64 在 Apple Silicon 上走 QEMU 模拟;按 rerun 文档约定以 arm64 原生为准,amd64 标注"模拟环境"供参考 —— 实际两者一致,故结论不受模拟层影响。

## 结论

**ABI 假设获得完全的跨平台确认**:tree-sitter-language-pack 1.12.5 的预编译 grammar 与 tree-sitter **0.26** C 运行时 ABI 不匹配,在 macOS arm64 / Linux arm64 / Linux amd64 三个平台上**同质复现**(40% 真实 Python 文件 SIGSEGV),降到 0.25.2 即消失。"macOS 26 太新"假设彻底证伪。

## 环境

- 宿主机:Mac14,7 / Apple M2 / macOS 26.5.2(Darwin 25.5.0)
- 容器镜像:`python:3.12-slim`(digest `sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`)
- Docker:29.6.1
- 挂载:`~/bench` → `/Users/liangck/bench:ro`(匹配 `scan.py` 硬编码路径);`src/` → `/krodo-src:ro`(`PYTHONPATH=/krodo-src`)
- 容器内依赖:`tree-sitter==0.26.0|0.25.2` + `tree-sitter-language-pack==1.12.5` + `pathspec` + `pydantic`

## 对上游 issue 的意义

从"一台 macOS 26 机器上的报告"升级为**"跨平台、跨架构可复现的 0.26 运行时兼容性 bug"**:
- 复现面:macOS arm64 + Linux arm64 + Linux amd64,三平台同 40% 崩溃率、同崩溃文件集。
- 复现脚本:`~/bench/scan.py`(每文件一个子进程,确定性)。
- 触发条件:Python grammar 的 C 外部扫描器与 0.26 C 运行时 ABI 错位。
- 规避:运行时降到 0.25.x。

对 PR3(pin 收窄到 `tree-sitter>=0.25,<0.26`)的决策**无影响**(本机证据已足够),但为该 pin 提供了跨平台佐证。
