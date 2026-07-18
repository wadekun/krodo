# M9 性能验收 — tree-sitter 符号索引基准测试

**对应验收项**(见 `.cursor/plans/m9_tree-sitter_符号索引_4d413543.plan.md`):

| 验收项 | 阈值 | 测量对象 |
|---|---|---|
| #1 查询延迟 | < 100ms | 5 个真实 Python 项目上 `SymbolBackend.find_symbol()` 后端调用(p95) |
| #2 冷构建 | < 30s | 10k 文件级仓库 `build_full()` |
| #2 单文件增量 | < 1s | 修改一个文件 → `invalidate` → 下次查询(含单文件重解析) |
| slow smoke(顺延项) | 记录即可 | 合成 1k 文件构建/查询计时 |

> 注意:测的是 **`SymbolBackend` 后端调用**,不是 M11 的 `find_symbol` 工具(M9 不注册工具)。

## 第 1 步:准备基准仓库

放在 krodo 仓库之外,避免污染工作区:

```bash
mkdir -p ~/bench && cd ~/bench
git clone --depth 1 https://github.com/fastapi/fastapi
git clone --depth 1 https://github.com/pallets/flask
git clone --depth 1 https://github.com/encode/httpx
git clone --depth 1 https://github.com/django/django
# 10k 文件级仓库(验收 #2)
git clone --depth 1 https://github.com/home-assistant/core ha-core
```

第 5 个 Python 项目使用 krodo 本仓库,无需 clone。

## 第 2 步:保存基准脚本

存为 `~/bench/bench_symbol_index.py`。**刻意放在 krodo 仓库外**:脚本用 `print` 输出,放进仓库会触发 ruff 的 T20 规则。

```python
"""M9 性能验收: TreeSitterSymbolIndex 构建/查询/增量基准."""

import sys
import time
from pathlib import Path

from krodo.indexer import TreeSitterSymbolIndex
from krodo.sandbox.ignore import KrodoIgnore

DB_DIR = Path("/tmp/krodo-bench")


def bench_repo(root: Path) -> None:
    name = root.name
    DB_DIR.mkdir(parents=True, exist_ok=True)
    db = DB_DIR / f"{name}.db"
    for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
        p.unlink(missing_ok=True)

    idx = TreeSitterSymbolIndex(db, root, ignore=KrodoIgnore(root))

    # 冷构建(验收 #2: <30s @ 10k 文件)
    t0 = time.perf_counter()
    stats = idx.build_full()
    cold_s = time.perf_counter() - t0

    # 热构建(增量 no-op, 只 stat 不重解析)
    t0 = time.perf_counter()
    idx.build_full()
    warm_ms = (time.perf_counter() - t0) * 1000

    # 查询延迟(验收 #1: <100ms): 随机抽 200 个真实符号名
    rows = idx._conn.execute(
        "SELECT DISTINCT name FROM symbols ORDER BY RANDOM() LIMIT 200"
    ).fetchall()
    names = [r["name"] for r in rows]
    lat = []
    for n in names:
        t0 = time.perf_counter()
        idx.find_symbol(n)
        lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    p50 = lat[len(lat) // 2] if lat else 0.0
    p95 = lat[int(len(lat) * 0.95)] if lat else 0.0
    mx = lat[-1] if lat else 0.0

    # 单文件增量(验收 #2: <1s): 改一个已索引文件 -> invalidate -> 下次查询含重解析
    inc_ms = 0.0
    row = idx._conn.execute("SELECT path FROM files ORDER BY path LIMIT 1").fetchone()
    if row and names:
        f = root / row["path"]
        original = f.read_bytes()
        f.write_bytes(original + b"\n# bench touch\n")
        idx.invalidate([row["path"]])
        t0 = time.perf_counter()
        idx.find_symbol(names[0])
        inc_ms = (time.perf_counter() - t0) * 1000
        f.write_bytes(original)  # 还原

    print(
        f"| {name} | {stats.files_indexed:,} | {stats.symbols:,} "
        f"| {cold_s:.1f}s | {warm_ms:.0f}ms "
        f"| {p50:.1f} | {p95:.1f} | {mx:.1f} | {inc_ms:.0f}ms |"
    )
    idx.close()


def synthetic_1k() -> None:
    """合成 1k 文件 smoke(plan 顺延项)."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for i in range(1000):
            body = "\n".join(f"def fn_{i}_{j}(x):\n    return x + {j}" for j in range(20))
            (root / f"m{i:04d}.py").write_text(body, encoding="utf-8")
        bench_repo(root)


if __name__ == "__main__":
    print("| repo | files | symbols | cold build | warm build | q p50(ms) | q p95(ms) | q max(ms) | incremental |")
    print("|---|---|---|---|---|---|---|---|---|")
    for arg in sys.argv[1:]:
        if arg == "--synthetic":
            synthetic_1k()
        else:
            bench_repo(Path(arg).resolve())
```

脚本说明:

- 每个仓库用全新 db(连 `-wal`/`-shm` 一起清掉),保证冷构建数字真实。
- 查询样本从 `symbols` 表随机抽 200 个真实符号名(白盒访问 `idx._conn`,基准脚本可接受)。
- 增量测量会临时在被测仓库的一个文件末尾追加注释再**字节级还原**;一次性 clone 的仓库无影响。

## 第 3 步:运行

在 **krodo 仓库根目录**执行(`uv run` 保证依赖环境):

```bash
cd <krodo 仓库根目录>
uv run python ~/bench/bench_symbol_index.py \
  . ~/bench/fastapi ~/bench/flask ~/bench/httpx ~/bench/django \
  ~/bench/ha-core \
  --synthetic
```

环境注意事项:

- 关掉重负载应用,笔记本**插电**运行(macOS 电池模式降频会使数字失真)。
- 跑 2-3 遍取稳定值。第一遍 ha-core 受文件系统冷缓存影响可能偏慢;如果只有第一遍超 30s、后续均达标,两个数字都记录并注明。
- krodo 本仓库参与增量测量前先确认工作区干净(`git status`),跑完再确认无 diff。

## 第 4 步:对照阈值,记录结果

脚本输出即 markdown 表格,直接粘贴到 PR description 作为验收证据:

| 验收项 | 阈值 | 看哪列 |
|---|---|---|
| #1 查询延迟 | < 100ms | 5 个 Python 项目行的 `q p95` |
| #2 冷构建 | < 30s | ha-core 行的 `cold build` |
| #2 单文件增量 | < 1s | ha-core 行的 `incremental` |
| slow smoke | 记录即可 | synthetic 行 |

同时记录环境信息:机器型号 / CPU / 内存 / 磁盘类型 / macOS 版本 / Python 版本(`uv run python -V`)。

## 超标时的排查方向

- **ha-core 冷构建超 30s**:优先看 `KrodoIgnore.is_ignored` 在每文件热路径上的开销(pathspec 匹配是 O(规则数));其次看是否误入未被剪枝的大目录(`stats.files_indexed` 异常大是信号)。
- **查询 p95 超 100ms**:确认 `idx_symbols_name` 索引生效(`EXPLAIN QUERY PLAN SELECT ... WHERE name = ?` 应显示 SEARCH ... USING INDEX);排查是否 `_flush_dirty`/`_refresh_if_stale` 在查询路径上重解析了意外多的文件。
- **增量超 1s**:单文件 parse 应为毫秒级,超标通常意味着 invalidate 的路径归一化失败导致整目录被误标脏,或被测文件本身超大。

## 结果存档

跑完后把结果表追加到本文档末尾(带日期与 commit SHA),作为历史基线;后续 M10/M11 若改动索引热路径,重跑对比。

### 2026-07-18 运行 — ✅ 通过(tree-sitter 运行时需收窄到 `<0.26`)

首次运行被原生 SIGSEGV 阻断,经判别实验定位根因:**`tree-sitter` 0.26 C 运行时与 language-pack 预编译 grammar 的 ABI 不匹配**(非"macOS 26 太新")。把运行时降到 0.25.2 后,40% 崩溃归零,基准在本机直接跑通,四项验收全部达标(ha-core 18k 文件冷构建 22–27s,查询 p95 ≤ 0.1ms)。

**修复**:`pyproject.toml` 一行收窄 `tree-sitter>=0.25,<1` → `tree-sitter>=0.25,<0.26`。完整诊断、判别实验、3 轮验收数字与建议见 **`docs/benchmarks/m9_symbol_index_perf_results.md`**。

