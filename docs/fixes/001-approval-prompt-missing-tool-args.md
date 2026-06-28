# FIX-001: 审批提示未展示工具参数内容

> **状态**: ✅ 已修复
> **影响范围**: `src/krodo/sandbox/approval.py`
> **发现日期**: 2026-06-28
> **严重程度**: 低（功能正常，仅 UX 缺失）

## 问题描述

在 `auto_edit` 模式下，当用户需要审批 `run_shell`、`git_commit`、`apply_patch` 等工具调用时，终端提示仅显示工具名称，**不显示具体操作内容**（如要执行的 shell 命令、commit message 等），导致用户无法在审批前了解即将执行的操作细节。

### 实际表现

```
Approval requested: run_shell
  Approve? [y=once / n=deny / a=session / p=pattern / ?=help]:
```

### 期望表现

```
Approval requested: run_shell pytest tests/unit
  Approve? [y=once / n=deny / a=session / p=pattern / ?=help]:
```

## 根因分析

`approval.py` 的 `_prompt` 方法（第 184–191 行）硬编码了 `"path"` 和 `"cmd"` 两个键来提取摘要信息：

```python
path_hint = tool_call.arguments.get("path", "")
cmd_hint = tool_call.arguments.get("cmd", "")
```

但各工具的实际参数名与之不匹配：

| 工具 | 实际参数名 | `_prompt` 尝试的键 | 能展示？ |
|------|-----------|-------------------|:--------:|
| `write_file` | `path` | `path` ✅ | ✅ |
| `edit_file` | `path` | `path` ✅ | ✅ |
| `run_shell` | `command` | `cmd` ❌ | ❌ |
| `git_commit` | `message` | `path` / `cmd` ❌ | ❌ |
| `apply_patch` | `patch` | `path` / `cmd` ❌ | ❌ |

其中 `run_shell` 的参数名定义于 `tools/builtin/shell.py:RunShellParams.command`，`git_commit` 的参数名定义于 `tools/builtin/git.py:GitCommitParams.message`。

## 修复方案

用**优先级键列表**替代硬编码的 `"path"` / `"cmd"` 查找逻辑。

### 改动 1: 新增 `_HINT_KEYS` 常量和 `_tool_call_hint` 函数

```python
# 按优先级排列：path > command > message
# 不含 patch —— apply_patch 的完整内容由 _maybe_render_diff 在摘要行下方展示
_HINT_KEYS: tuple[str, ...] = ("path", "command", "message")


def _tool_call_hint(tool_call: ToolCall) -> str:
    """Extract a one-line hint from tool_call arguments for the approval prompt."""
    for key in _HINT_KEYS:
        val = tool_call.arguments.get(key)
        if val and isinstance(val, str):
            if len(val) > 120:
                return val[:120] + "…"
            return val
    return ""
```

设计决策：

- **优先级顺序 `path > command > message`**: `path` 覆盖 `write_file` / `edit_file`，`command` 覆盖 `run_shell`，`message` 覆盖 `git_commit`。
- **不含 `patch`**: `apply_patch` 的补丁文本通常很长，`_maybe_render_diff` 已经在审批提示下方渲染完整 diff 预览，摘要行无需重复。
- **120 字符截断**: 防止超长命令或 commit message 撑爆终端单行显示。
- **可扩展**: 未来新增工具只需在 `_HINT_KEYS` 中追加对应参数名。

### 改动 2: 替换 `_prompt` 方法中的提取逻辑

```diff
 async def _prompt(self, tool_call: ToolCall) -> Decision:
     from rich.console import Console
     console = Console()
-    path_hint = tool_call.arguments.get("path", "")
-    cmd_hint = tool_call.arguments.get("cmd", "")
-
-    summary_parts: list[str] = [f"[bold yellow]{tool_call.name}[/bold yellow]"]
-    if path_hint:
-        summary_parts.append(str(path_hint))
-    elif cmd_hint:
-        summary_parts.append(str(cmd_hint))
+    hint = _tool_call_hint(tool_call)
+    summary_parts: list[str] = [f"[bold yellow]{tool_call.name}[/bold yellow]"]
+    if hint:
+        summary_parts.append(hint)

     console.print(f"\nApproval requested: {' '.join(summary_parts)}")
```

### 修复后效果

| 工具 | 审批摘要行示例 |
|------|--------------|
| `run_shell` | `Approval requested: run_shell pytest tests/unit` |
| `git_commit` | `Approval requested: git_commit fix: resolve import error` |
| `apply_patch` | `Approval requested: apply_patch` （详情在下方 diff preview） |
| `write_file` | `Approval requested: write_file src/main.py` （不变） |
| `edit_file` | `Approval requested: edit_file src/main.py` （不变） |

## 验证计划

```bash
# 单元测试
uv run pytest tests/unit/test_approval.py -v

# 手动验证：触发 run_shell / git_commit 审批，确认摘要行内容
uv run krodo --root /tmp/krodo-test "run ls -la and then create a git commit"
```
