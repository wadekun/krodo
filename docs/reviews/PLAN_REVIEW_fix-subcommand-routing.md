# Plan Review：`fix-subcommand-routing_3885bced`

> Reviewer: Code Review Agent | Date: 2026-05-28 | Plan: `.cursor/plans/fix-subcommand-routing_3885bced.plan.md`

---

## 总体评价

**问题诊断准确，但核心修复方案不可行。** 需要替换 `--` 注入策略为 args 拆分策略后才能实施。

---

## 1. 问题诊断 ✅

Plan 对根因的描述完全正确：

- `main.py` 主 callback 的 `prompt: typer.Argument(None)` 位置参数与 `undo`/`resume`/`doctor` 子命令冲突
- Click 解析时位置参数优先级高于子命令匹配
- 两种症状均识别准确：
  - `coda resume --root /X` → `No such command '--root'`
  - `coda --root /X resume` → 静默失败（prompt="resume" 传入 LLM）

补充说明：此 bug 从 M4 引入 PROMPT 位置参数时就存在，M5 新增 `resume` 才暴露。

---

## 2. 核心方案：`--` 注入 ❌ 不可行

### Plan 的策略

```python
args = list(args[:idx]) + ["--", *args[idx:]]
```

在子命令 token 前注入 `--`，期望阻止 PROMPT Argument 消费该 token。

### 为什么不可行

通过阅读 Click 8.3.3 源码（`click/parser.py`），`_OptionParser.parse_args` 的执行顺序为：

```python
# parser.py:294-310
def parse_args(self, args):
    state = _ParsingState(args)
    self._process_args_for_options(state)   # 步骤 1：处理 --options
    self._process_args_for_args(state)       # 步骤 2：处理位置参数
    return state.opts, state.largs, state.order
```

**步骤 1**（`parser.py:323-337`）：`--` 确实会立即 return，停止 option 解析。剩余 token 留在 `state.rargs`。

**步骤 2**（`parser.py:312-321`）：这是关键——`_process_args_for_args` 从 `state.largs + state.rargs` 中分配位置参数：

```python
def _process_args_for_args(self, state):
    pargs, args = _unpack_args(
        state.largs + state.rargs, [x.nargs for x in self._args]
    )
    for idx, arg in enumerate(self._args):
        arg.process(pargs[idx], state)
    state.largs = args
    state.rargs = []
```

`--` 只阻止了 **option** 解析，**不阻止位置参数分配**。PROMPT Argument 仍然会从 `state.rargs` 中消费子命令 token。

### 完整追踪

**Case 1：`coda resume --root /tmp/test`**

注入后 args = `['--', 'resume', '--root', '/tmp/test']`：

| 步骤 | 状态 |
|------|------|
| `_process_args_for_options` | pop `'--'` → return；`rargs = ['resume', '--root', '/tmp/test']` |
| `_process_args_for_args` | `_unpack_args(['resume', '--root', '/tmp/test'], [1])` |
| PROMPT 赋值 | `prompt = 'resume'` ← **仍然被吞** |
| `state.largs` | `['--root', '/tmp/test']` |
| `Command.parse_args` returns | `['--root', '/tmp/test']` |
| `Group.parse_args` | `_protected_args = ['--root']` |
| `resolve_command('--root')` | ❌ `No such command '--root'` |

**Case 2：`coda --root /tmp/test resume`**

注入后 args = `['--root', '/tmp/test', '--', 'resume']`：

| 步骤 | 状态 |
|------|------|
| `_process_args_for_options` | `--root /tmp/test` → parsed；`'--'` → return；`rargs = ['resume']` |
| `_process_args_for_args` | `_unpack_args(['resume'], [1])` |
| PROMPT 赋值 | `prompt = 'resume'` ← **同样被吞** |
| `Command.parse_args` returns | `[]` |
| `Group.parse_args` | `_protected_args = []`（无子命令） |
| `invoke_without_command=True` | 主 callback 以 `prompt='resume'` 执行 → **静默失败** |

### 结论

`--` 只影响 option 解析阶段，对位置参数分配无效。此方案在 Click 8.x 的解析架构下不可能生效。

---

## 3. 父 context 继承 `--root` ✅ 方向正确

Plan 正确识别了 `coda --root /X resume` 的静默失败问题，并提出通过 `ctx.find_root().params` 继承父级参数。

**评价**：方向正确，解决了真实存在的 foot-gun。但此修复依赖子命令路由先能工作，应作为第二步实施。

**建议**：考虑使用 `typer.Context` 的 `default_map` 或在 `_build_session_components` 中统一处理，而非在每个子命令 callback 中重复 fallback 逻辑。

---

## 4. `_find_first_positional` 设计 ✅

Plan 提出"通过 `self.params` 区分 `is_flag` 来判断 option 是否带值"，比纯字符串启发式更可靠。

**评价**：这是该 Plan 中最有价值的工程细节。利用 Click 的参数定义来判断 flag vs value-taking option，避免了误判短选项值（如 `-m anthropic/gpt-4o` 中的 `anthropic/gpt-4o` 不会被误认为位置参数）。

**注意**：需确认 `TyperGroup` 是否暴露 `self.params` 或需要通过 `self.get_params(ctx)` 获取。

---

## 5. 测试覆盖 ✅

Plan 列出的测试场景全面：

| 测试 | 覆盖 |
|------|------|
| `coda resume --root /tmp/X` | 子命令 + options |
| `coda --root /tmp/X resume` | 全局 option + 子命令 |
| `coda "resume the work"` | 引号 prompt 不撞子命令 |
| `coda undo` / `coda doctor` | 所有子命令 |
| 回归测试 13 处现有 invoke | 不破坏已有 e2e |

**建议补充**：
- `coda --model openai/gpt-4o resume --list` — 全局 + 子命令 options 混合
- `coda` (无参数) — REPL 模式不回归
- `coda --help` — help 输出不变

---

## 6. 推荐的替代方案：args 拆分

正确的修复方式是在 `parse_args` 中**将参数在子命令 token 处拆分**，只把子命令前的 args 交给 Click 解析：

```python
class CodaGroup(TyperGroup):
    def parse_args(self, ctx, args):
        if args and self.commands:
            cmd_name, cmd_index = self._find_subcommand_token(args)
            if cmd_name is not None:
                group_args = args[:cmd_index]       # 子命令前的 group options
                subcmd_args = args[cmd_index + 1:]  # 子命令后的参数
                Command.parse_args(self, ctx, group_args)  # 只解析 group options
                ctx._protected_args = [cmd_name]    # 手动设置子命令分发
                ctx.args = subcmd_args
                return ctx.args
        return super().parse_args(ctx, args)
```

**追踪 `coda resume --root /tmp/test`：**

```
args = ['resume', '--root', '/tmp/test']
_find_subcommand_token → ('resume', 0)
group_args = []                       ← 无 group options
subcmd_args = ['--root', '/tmp/test']
Command.parse_args(self, ctx, [])     ← PROMPT = None（默认值）
ctx._protected_args = ['resume']
ctx.args = ['--root', '/tmp/test']
→ resolve_command('resume') → resume 子命令处理 --root /tmp/test ✅
```

**追踪 `coda --root /tmp/test resume abc123`：**

```
args = ['--root', '/tmp/test', 'resume', 'abc123']
_find_subcommand_token → ('resume', 2)
group_args = ['--root', '/tmp/test']  ← group options
subcmd_args = ['abc123']              ← 子命令参数
Command.parse_args(self, ctx, ['--root', '/tmp/test'])  ← root=/tmp/test
ctx._protected_args = ['resume']
ctx.args = ['abc123']
→ resolve_command('resume') → resume 获得 session_id=abc123 ✅
```

**追踪 `coda "fix the bug"`：**

```
args = ['fix the bug']
_find_subcommand_token → (None, None)  ← 'fix the bug' 不在 commands 中
→ super().parse_args() → PROMPT = "fix the bug" ✅
```

---

## 7. 改动范围评估 ✅

Plan 的改动范围合理：

| 文件 | 改动 | 评价 |
|------|------|------|
| 新增 `src/coda/cli/group.py` | ~50 行 | ✅ 独立模块，职责清晰 |
| `src/coda/cli/main.py` | 1 行（`cls=CodaGroup`） | ✅ 最小侵入 |
| `src/coda/cli/resume.py` | 增 ctx 参数 | ✅ 必要改动 |
| `src/coda/cli/undo.py` | 增 ctx 参数 | ✅ 必要改动 |
| `src/coda/cli/doctor.py` | 增 ctx 参数 | ✅ 必要改动 |
| 新增测试文件 | 2 个 | ✅ 覆盖充分 |

---

## 8. 综合评分

| 维度 | 评分 | 说明 |
|------|------|------|
| 问题诊断 | 9/10 | 根因准确，两种症状均识别 |
| 核心修复方案 | 3/10 | `--` 注入不可行，需替换为 args 拆分 |
| 父 context 继承 | 8/10 | 方向正确，细节待定 |
| `_find_first_positional` 设计 | 9/10 | 利用 Click 参数定义，比启发式可靠 |
| 测试覆盖 | 8/10 | 场景全面，建议补充 2-3 个边界用例 |
| 改动范围 | 9/10 | 最小侵入，不动 undo/resume/doctor 核心逻辑 |
| **综合** | **7/10** | 修复核心方案需替换后即可实施 |

---

## 9. 建议行动

1. **替换核心方案**：将 `--` 注入改为 args 拆分（见第 6 节）
2. **保留其余设计**：`_find_first_positional`、父 context 继承、测试、文档均可复用
3. **实施顺序**：先修子命令路由 → 再加父 context 继承 → 最后文档更新
