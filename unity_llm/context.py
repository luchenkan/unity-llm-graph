"""LLM 上下文打包:把「一个任务相关的代码 + 序列化依赖」压进 token 预算。

用法场景:
  - 改 Bug 前:`context --target Enemy.TakeDamage` 拿到方法签名、调用者、
    挂了该脚本的 prefab、引用了它的其它资产 —— 而不是把整个仓库喂给模型。
  - 不支持 MCP 的模型(网页版 Kimi/豆包/DeepSeek):生成 .md 直接粘贴。
"""
from __future__ import annotations

from typing import Dict, List

from .graph import connect
from .queries import resolve_target, impact
from .tokens import estimate_tokens, truncate_tokens


def _fmt_impact_section(imp: Dict) -> str:
    """先放 Unity 独占证据,再放普通 C# 调用边。"""
    lines: List[str] = []

    if imp.get("hint"):
        lines.extend(("## 解析提示", imp["hint"]))
    if imp.get("event_bindings"):
        lines.append("## UnityEvent 绑定")
        for e in imp["event_bindings"]:
            lines.append(f"- `{e['asset']}` `{e['event']}` -> `{e['method']}`"
                         f" @ `{e['on']}`")
    if imp["asset_dependents"]:
        lines.append("## 序列化依赖")
        for a in imp["asset_dependents"]:
            lines.append(f"- `{a['asset']}` `{a['field']}` @ `{a['context']}`"
                         f" (depth {a['depth']})")
    code = imp["code_dependents"]
    ext = [c for c in code if c.get("scope") != "internal"]
    internal = [c for c in code if c.get("scope") == "internal"]
    if ext:
        lines.append("## 外部调用方")
        for c in ext:
            lines.append(f"- `{c['caller']}` {c['via']} `{c['calls']}`"
                         f" @ {c['at']} ({c['confidence']})")
    if internal:
        lines.append("## 内部调用")
        for c in internal:
            lines.append(f"- `{c['caller']}` {c['via']} `{c['calls']}`"
                         f" @ {c['at']} ({c['confidence']})")
    s = imp["summary"]
    if s:
        lines.append(
            "> total: code "
            f"{s.get('code_dependents', 0)}"
            f" (external {s.get('external_callers', 0)},"
            f" internal {s.get('internal_calls', 0)}), direct assets "
            f"{s.get('direct_asset_dependents', 0)}, transitive assets "
            f"{s.get('transitive_assets', 0)}, UnityEvent "
            f"{s.get('event_bindings', 0)}")
    return "\n".join(lines)


def _identity(node: Dict) -> str:
    kind = node["kind"]
    if kind == "type":
        suffix = f" : {node['bases']}" if node.get("bases") else ""
        partial = " (partial)" if node.get("partial") else ""
        return f"- type `{node['full_name']}`{suffix}{partial}\n- file `{node['file']}`"
    if kind == "asset":
        deleted = " [deleted]" if node.get("deleted") else ""
        return f"- asset `{node['path']}`{deleted}\n- guid `{node['guid']}`"
    if kind in ("method", "field", "property"):
        return (f"- {kind} `{node['owner']}.{node['name']}`\n"
                f"- signature `{node['signature']}`\n- file `{node['file']}`")
    candidates = "\n".join(f"- candidate `{c}`"
                           for c in node.get("candidates", [])[:8])
    return f"- unresolved ({kind})" + (f"\n{candidates}" if candidates else "")


def _member_lines(conn, node: Dict) -> List[str]:
    if node["kind"] == "type":
        owner, only_name = node["full_name"], None
    elif node["kind"] in ("method", "field", "property"):
        owner, only_name = node["owner"], node["name"]
    elif node["kind"] == "asset" and node.get("ext") == ".cs":
        row = conn.execute(
            "SELECT full_name FROM types WHERE file=? ORDER BY is_mono DESC LIMIT 1",
            (node["path"],)).fetchone()
        if not row:
            return []
        owner, only_name = row["full_name"], None
    else:
        return []
    sql = (
        "SELECT kind,name,signature,modifiers,is_lifecycle,serialized"
        " FROM members WHERE owner=?")
    args = [owner]
    if only_name:
        sql += " AND name=?"
        args.append(only_name)
    sql += (
        " ORDER BY CASE WHEN is_lifecycle=1 THEN 0 WHEN serialized=1 THEN 1"
        " WHEN modifiers LIKE '%public%' THEN 2"
        " WHEN modifiers LIKE '%protected%' THEN 3 ELSE 4 END,line")
    lines = []
    for m in conn.execute(sql, tuple(args)):
        tags = []
        if m["is_lifecycle"]:
            tags.append("lifecycle")
        if m["serialized"]:
            tags.append("serialized")
        tag = f" [{','.join(tags)}]" if tags else ""
        lines.append(f"- {m['kind']} `{m['signature']}`{tag}")
    return lines


def build_context(project_root: str, target: str, budget_tokens: int = 2000,
                  include_source: bool = False) -> str:
    """针对一个目标生成 evidence-first 上下文,严格不超过估算 token 预算。"""
    budget_tokens = max(1, int(budget_tokens))
    query_limit = max(3, min(12, budget_tokens // 120))
    # impact 会在目标文件 stale 时先增量补图,签名必须读补齐后的库
    imp = impact(project_root, target, limit=query_limit)
    conn = connect(project_root)
    node = resolve_target(conn, target)
    members = _member_lines(conn, node)
    conn.close()

    critical_count = (len(members)
                      if node["kind"] in ("method", "field", "property") else 4)
    critical, rest = members[:critical_count], members[critical_count:]
    parts = [
        f"# Unity context: {target}",
        f"> budget {budget_tokens}; estimated with CJK-aware heuristic",
        "## Target",
        _identity(node),
    ]
    if critical:
        parts.extend(("## Key signatures", "\n".join(critical)))
    evidence = _fmt_impact_section(imp)
    if evidence:
        parts.append(evidence)
    if rest:
        parts.extend(("## Additional signatures", "\n".join(rest)))
    text = "\n\n".join(parts)
    result = truncate_tokens(text, budget_tokens,
                             marker="\n...(token budget truncated)")
    assert estimate_tokens(result) <= budget_tokens
    return result
