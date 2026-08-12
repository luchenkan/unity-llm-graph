#!/usr/bin/env python
"""Claude Code PostToolUse hook:Edit/Write 之后增量刷新 unity-llm 图谱。

装法(`<Unity项目>/.claude/settings.json`):

    {
      "hooks": {
        "PostToolUse": [
          { "matcher": "Edit|Write",
            "hooks": [{ "type": "command",
                        "command": "python /path/to/unity-llm-graph/tools/hook_post_edit.py" }] }
        ]
      }
    }

工作方式:从 stdin 读 hook 事件 JSON,取 tool_input.file_path,向上找到
含 `.unity-llm/graph.db` 的目录当项目根,只对 Unity 资产后缀调 `update`。
任何异常都静默退出 0 —— hook 绝不能挡住编辑。

注意:调用边解析是全局的,每次 update 都会整体重跑一次该步骤。批量重构时
这个 hook 会重复付费;那种场景建议改用 git post-commit hook(见 README「让模型
真的用上它」)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

WATCH_EXTS = {".cs", ".prefab", ".unity", ".asset", ".controller", ".anim",
              ".playable", ".mask", ".preset"}
MARKER = os.path.join(".unity-llm", "graph.db")


def find_root(path: str) -> str:
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(d, MARKER)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    path = (event.get("tool_input") or {}).get("file_path") or ""
    if not path or os.path.splitext(path)[1].lower() not in WATCH_EXTS:
        return 0
    root = find_root(path)
    if not root:
        return 0  # 这个项目还没建图,不多事
    rel = os.path.relpath(os.path.abspath(path), root).replace("\\", "/")
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        subprocess.run(
            [sys.executable, "-m", "unity_llm", "update",
             "--project", root, "--files", rel],
            cwd=pkg_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=120)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
