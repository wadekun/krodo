# M9 Linux 重跑 — ABI 假设跨平台验证(scan.py 对照实验)

> **归因已修正(2026-07-18)**:本文档所述"ABI 假设"后被证伪——真正根因是 py-tree-sitter 0.26.0 的 `Point` 引用计数 use-after-free(上游 [#466](https://github.com/tree-sitter/py-tree-sitter/pull/466),已修未发版),与 language-pack grammar 无关。本实验数据仍有效,含义改为"证明该 bug 平台/架构无关"。详见 `m9_symbol_index_perf_results.md` 末节「根因二次修正」。

**状态**: ✅ 已执行(2026-07-18)。四格全部命中 ABI 假设预言(macOS arm64 / Linux arm64 原生 / Linux amd64 模拟,0.26 均 40% 崩、0.25.2 均 0 崩,崩溃文件集逐字一致)。结果记入 `docs/benchmarks/m9_symbol_index_perf_results.md` 末尾「Linux 跨平台验证」。

**日期**: 2026-07-18
**前置文档**: `docs/benchmarks/m9_symbol_index_perf_results.md`(实验 1 已在本机确认 ABI 假设)
**预计耗时**: ~10 分钟(arm64 原生);amd64 模拟另加 ~10 分钟(可选)

## 目的与定位

**这不是性能基准重跑,也不是 M9 验收项。** M9 四项验收已用本机(macOS + tree-sitter 0.25.2)数字闭合;Linux 容器里跑出来的时延数字既不代表真实 Linux 服务器也不代表 macOS,没有基线价值。本实验只跑 `scan.py` 崩溃率对照,产出物有两个:

1. **强化上游 issue**。ABI 假设预言 Linux + tree-sitter 0.26 同样崩。验证成立 → issue 从"一台 macOS 26 机器上的报告"升级为"跨平台可复现的 0.26 运行时兼容性 bug";如果 Linux 上竟然不崩 → 说明 ABI 错位有平台相关触发条件,这本身也是要写进 issue 的重要信息。
2. **补 CI 盲区的一次性实弹检验**。单测文件太简单踩不中崩溃路径,CI 绿不代表真实语料安全;用 httpx 真实语料在 Linux 上验证 0.25.2,等于给"pin 收窄后的另一个支持平台"做一次真实语料检验。

**时序**:与 PR3(pin 收窄)平行,不阻塞;结果供上游 issue 引用。

## 预期结果(ABI 假设的预言)

| 组合 | 预期 |
|---|---|
| Linux + language-pack 1.12.5 + tree-sitter **0.26.0** | ❌ httpx 出现 crashers(与 macOS 的 40% 同量级) |
| Linux + language-pack 1.12.5 + tree-sitter **0.25.2** | ✅ 0 crashers |

两行都符合预期 → ABI 假设获得跨平台确认。任何一行偏离预期 → 记录实际数字,连同环境信息一起写进上游 issue(偏离本身就是关键线索)。

## 前置条件

- Docker Desktop(或 OrbStack 等)正常运行。
- `~/bench/` 下已有(首次基准时已备好):
  - `httpx/`(shallow clone 的真实语料)
  - `scan.py`(每文件一个子进程的崩溃率统计)
  - `one.py`(单文件提取脚本,被 `scan.py` 以绝对路径 `/Users/liangck/bench/one.py` 调用)
- krodo 仓库本地路径:`/Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo`

> **挂载路径不要改**:`scan.py` 硬编码了 `/Users/liangck/bench/one.py`,所以容器内必须把 `~/bench` 挂到**完全相同的路径**,脚本才能零改动运行(下面的命令已按此写好)。

## 第 1 步:启动容器(arm64 原生,M2 上速度正常)

```bash
docker run --rm -it \
  -v "$HOME/bench":/Users/liangck/bench:ro \
  -v /Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/src:/krodo-src:ro \
  python:3.12-slim bash
```

两个挂载都是只读(`:ro`),容器不会碰宿主机文件。`scan.py` 本身不写任何东西(纯读 + 子进程解析)。

## 第 2 步:容器内 — 0.26.0 对照(预期:崩)

```bash
pip install -q "tree-sitter==0.26.0" "tree-sitter-language-pack==1.12.5" pathspec pydantic

export PYTHONPATH=/krodo-src
python -c "import tree_sitter, tree_sitter_language_pack; print(tree_sitter.__version__)"  # 应输出 0.26.0

python /Users/liangck/bench/scan.py /Users/liangck/bench/httpx
```

记下最后一行,形如 `60 files, N crashers (X%)`。

依赖说明:`scan.py` 的 import 闭包(`krodo.indexer` → `krodo.sandbox.ignore` → `krodo.core.types`)只需要上面 4 个三方包;若仍报 `ModuleNotFoundError`,按提示 `pip install` 补装即可,不影响实验有效性。

## 第 3 步:容器内 — 降级 0.25.2 重跑(预期:0 crashers)

```bash
pip install -q "tree-sitter==0.25.2"   # 只换运行时,language-pack 1.12.5 不动
python -c "import tree_sitter; print(tree_sitter.__version__)"  # 应输出 0.25.2

python /Users/liangck/bench/scan.py /Users/liangck/bench/httpx
```

## 第 4 步(可选):x86_64 再跑一遍

上游 wheel 按架构分发,amd64 的 grammar 二进制与 arm64 不是同一份;如果想让 issue 覆盖两种架构,退出当前容器后加 `--platform linux/amd64` 重复第 1–3 步:

```bash
docker run --rm -it --platform linux/amd64 \
  -v "$HOME/bench":/Users/liangck/bench:ro \
  -v /Volumes/lck_mac_ext/source_code/opensource/ai-agents/krodo/src:/krodo-src:ro \
  python:3.12-slim bash
```

注意:amd64 在 Apple Silicon 上走模拟(Rosetta/QEMU),速度慢属正常;模拟层理论上可能改变原生崩溃的表现形式,若 amd64 结果与 arm64 不一致,以 arm64(原生)为准,amd64 结果标注"模拟环境"后仍可写进 issue 供参考。

## 第 5 步:记录结果

把四行(或不做 amd64 则两行)结果追加到 `m9_symbol_index_perf_results.md` 末尾,格式对齐已有的判别实验记录:

```
[Linux arm64] language-pack 1.12.5 + tree-sitter 0.26.0  ->  60 files, ?? crashers (??%)
[Linux arm64] language-pack 1.12.5 + tree-sitter 0.25.2  ->  60 files, ?? crashers (??%)
[Linux amd64] language-pack 1.12.5 + tree-sitter 0.26.0  ->  60 files, ?? crashers (??%)   (可选)
[Linux amd64] language-pack 1.12.5 + tree-sitter 0.25.2  ->  60 files, ?? crashers (??%)   (可选)
```

同时记录:容器镜像(`python:3.12-slim` 的 digest 或拉取日期)、Docker 运行时(Docker Desktop / OrbStack 及版本)、宿主机(Mac14,7 / Apple M2 / macOS 26.5.2)。

## 结果解读

| 结果 | 结论 | 下一步 |
|---|---|---|
| 0.26 崩、0.25.2 不崩(与 macOS 一致) | ABI 假设跨平台确认 | 上游 issue 写"macOS + Linux 双平台复现,0.26 运行时兼容性 bug";PR3 pin 收窄照常 |
| 0.26 不崩 | ABI 错位有平台相关触发条件 | issue 里如实报告差异;pin 收窄仍然正确(macOS 证据已充分) |
| 0.25.2 也崩 | 出现新未知因素(与本机实验矛盾) | 先排除环境问题(依赖版本、挂载、语料完整性),复核后再下结论 |

无论哪种结果,**都不改变 PR3(pin 收窄到 `>=0.25,<0.26`)的决策**——本机证据已足够支撑该修复。

## 长期形态(不在本实验范围)

这类验证的可持续形态是 CI 里手动触发的 benchmark / grammar-stability job(ubuntu runner + 真实语料 scan + 小基准),供 M10/M11 改索引热路径后复跑。已列入 M10 计划待办,见 `phase_2_overview.plan.md`。
