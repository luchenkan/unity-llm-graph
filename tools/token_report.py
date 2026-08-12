#!/usr/bin/env python
"""统计 unity-llm 这一段时间省了多少 token,并把结果追加进一个 Markdown 台账。

用法(通常由 Claude Code 的 Stop hook 调用,也能手跑):

    python tools/token_report.py --project /path/to/YourUnityProject \
        --ledger "~/.claude/projects/<proj>/memory/unity-llm-token-savings.md"

口径(刻意保守,宁可少报):

    spent    = 本段内所有 MCP 工具调用返回体的 approx_tokens(实际花掉的)
    baseline = 这些调用涉及到的源文件全文 token 数(不用图谱时模型得整篇读进去),
               同一文件只算一次;`.unity-llm/graph.db` 里按类名/方法名/资产路径解析
    net      = baseline - spent

无法解析目标的调用(stats / deadcode / update ...)只计 spent,不计 baseline ——
它们不替代读文件,所以只会拉低净收益,不会虚增。

状态存在 `<项目>/.unity-llm/token_report.state`(读到 calls.log 的哪个字节 +
历史累计),所以每次只结算新增的调用;calls.log 轮转后自动从头读。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

CHARS_PER_TOKEN = 4
DB_DIR = ".unity-llm"
STATE = "token_report.state"
CALL_LOG = "calls.log"


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"offset": 0, "total_net": 0, "tasks": 0}


def _resolve_files(db: str, targets: list) -> set:
    """把工具参数里的 target 解析成源文件相对路径集合。"""
    files: set = set()
    if not os.path.exists(db):
        return files
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    except Exception:
        return files
    try:
        for t in targets:
            if not t:
                continue
            if "/" in t or t.lower().endswith((".cs", ".prefab", ".unity", ".asset")):
                files.add(t)
                continue
            owner, _, member = t.rpartition(".")
            rows = []
            if owner:
                rows = con.execute(
                    "SELECT file FROM members WHERE name=? AND (owner=? OR owner LIKE ?)",
                    (member, owner, "%." + owner)).fetchall()
            if not rows:
                rows = con.execute(
                    "SELECT file FROM types WHERE name=? OR full_name=?",
                    (t, t)).fetchall()
            if not rows:
                rows = con.execute(
                    "SELECT file FROM members WHERE name=? LIMIT 8", (t,)).fetchall()
            files.update(r[0] for r in rows if r[0])
    except Exception:
        pass
    finally:
        con.close()
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--ledger", default="")
    ap.add_argument("--min-net", type=int, default=1,
                    help="净收益低于这个数就不输出、不记账(默认 1)")
    a = ap.parse_args()

    root = os.path.abspath(a.project)
    log = os.path.join(root, DB_DIR, CALL_LOG)
    state_path = os.path.join(root, DB_DIR, STATE)
    if not os.path.exists(log):
        return 0

    st = _load_state(state_path)
    offset = int(st.get("offset", 0))
    size = os.path.getsize(log)
    if size < offset:      # 轮转过
        offset = 0
    if size == offset:     # 本次任务没调过工具
        return 0

    with open(log, encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        chunk = f.read()
        new_offset = size

    recs = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:
            pass
    if not recs:
        return 0

    spent = sum(int(r.get("approx_tokens", 0)) for r in recs)
    targets = [(r.get("args") or {}).get("target") for r in recs]
    files = _resolve_files(os.path.join(root, DB_DIR, "graph.db"),
                           [t for t in targets if t])

    baseline = 0
    counted = []
    for rel in sorted(files):
        p = os.path.join(root, rel.replace("/", os.sep))
        try:
            baseline += os.path.getsize(p) // CHARS_PER_TOKEN
            counted.append(rel)
        except Exception:
            pass

    net = baseline - spent
    st["offset"] = new_offset
    if net >= a.min_net:
        st["total_net"] = int(st.get("total_net", 0)) + net
        st["tasks"] = int(st.get("tasks", 0)) + 1
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass

    if net < a.min_net:
        return 0

    if a.ledger:
        _append_ledger(a.ledger, recs, counted, baseline, spent, net,
                       st.get("total_net", net))

    print("Unity-LLM已经帮你节省了Token：%d" % net)
    return 0


def _append_ledger(path: str, recs: list, files: list, baseline: int,
                   spent: int, net: int, total: int) -> None:
    tools = {}
    for r in recs:
        tools[r.get("tool", "?")] = tools.get(r.get("tool", "?"), 0) + 1
    row = "| %s | %s | %d | %d | **%d** | %d |\n" % (
        time.strftime("%Y-%m-%d %H:%M"),
        " ".join("%s×%d" % (k, v) for k, v in sorted(tools.items())),
        baseline, spent, net, total)
    try:
        if not os.path.exists(path):
            return  # 台账文件由人维护,不自动创建,避免写到奇怪地方
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        marker = "<!-- token-ledger -->"
        if marker in text:
            head, _, tail = text.partition(marker)
            text = head + marker + "\n" + row + tail.lstrip("\n")
        else:
            text = text.rstrip("\n") + "\n" + row
        if files:
            note = "<!-- last-files: %s -->" % ", ".join(files[:6])
            if "<!-- last-files:" in text:
                import re
                text = re.sub(r"<!-- last-files:.*?-->", note, text, count=1)
            else:
                text = text.rstrip("\n") + "\n\n" + note + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
