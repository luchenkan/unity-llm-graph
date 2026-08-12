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

CHARS_PER_TOKEN = 4  # 粗略估计:1 token ≈ 4 字符(中英文混合偏保守)


def _fmt_impact_section(imp: Dict, budget_chars: int) -> str:
    lines: List[str] = []
    used = 0

    def emit(s: str) -> bool:
        nonlocal used
        if used + len(s) > budget_chars:
            return False
        lines.append(s)
        used += len(s)
        return True

    node = imp["resolved"]
    emit(f"## 目标: {imp['target']}")
    if node["kind"] == "type":
        emit(f"- 类型 `{node['full_name']}` ({node['file']})"
             + (f", 基类: {node['bases']}" if node.get("bases") else ""))
    elif node["kind"] == "asset":
        emit(f"- 资产 `{node['path']}` (guid: {node['guid']})")
    elif node["kind"] == "method":
        emit(f"- 方法 `{node['owner']}.{node['name']}` ({node['file']})")
    else:  # unknown / ambiguous:没解析出节点,只能给提示
        emit(f"- ❌ 未能解析成图里的节点(kind={node['kind']})")
        for c in node.get("candidates", [])[:15]:
            emit(f"  - 候选: `{c}`")

    if imp.get("hint"):
        emit(f"\n> ⚠️ **解析提示**: {imp['hint']}")

    if imp["code_dependents"]:
        ext = [c for c in imp["code_dependents"] if c.get("scope") != "internal"]
        internal = [c for c in imp["code_dependents"] if c.get("scope") == "internal"]

        def dump(items):
            for c in items:
                if not emit(f"- `{c['caller']}` — {c['via']} `{c['calls']}`"
                            f" @ {c['at']} ({c['confidence']})"):
                    emit("- ...(超出预算,截断)")
                    return
        if ext:
            emit("\n### 外部调用方(改了会炸到别人)")
            dump(ext)
        if internal:
            emit("\n### 内部自调用(这个类自己的方法互调)")
            dump(internal)
        if not ext:
            emit("\n> 没有外部调用方 —— 但序列化引用/UnityEvent 通道仍可能有耦合。")
    if imp["asset_dependents"]:
        emit("\n### 序列化依赖(哪些 prefab/scene 引用了它)")
        for a in imp["asset_dependents"]:
            if not emit(f"- `{a['asset']}` 字段 `{a['field']}`"
                        f" (GameObject: {a['context']}, depth {a['depth']})"):
                emit("- ...(超出预算,截断)")
                break
    if imp.get("event_bindings"):
        emit("\n### UnityEvent 绑定(Inspector 里指名调用,代码里搜不到)")
        for e in imp["event_bindings"]:
            if not emit(f"- `{e['asset']}` 的 `{e['event']}` -> `{e['method']}`"
                        f" (GameObject: {e['on']})"):
                break
    s = imp["summary"]
    if s:
        emit(f"\n> 合计: 代码依赖 {s.get('code_dependents', 0)} 处"
             f"(外部 {s.get('external_callers', 0)} / 内部 {s.get('internal_calls', 0)}"
             f", high {s.get('high_confidence', 0)}), "
             f"直接资产引用 {s.get('direct_asset_dependents', 0)} 个, "
             f"传递影响资产 {s.get('transitive_assets', 0)} 个, "
             f"UnityEvent 绑定 {s.get('event_bindings', 0)} 处。")
    return "\n".join(lines)


def build_context(project_root: str, target: str, budget_tokens: int = 2000,
                  include_source: bool = False) -> str:
    """针对一个目标生成精简上下文(markdown)。"""
    conn = connect(project_root)
    node = resolve_target(conn, target)
    budget_chars = budget_tokens * CHARS_PER_TOKEN

    parts = [f"# Unity 上下文包: {target}\n",
             f"(预算 ~{budget_tokens} tokens, 由 unity-llm-graph 生成)\n"]

    # 类型签名区
    if node["kind"] in ("type", "method", "asset"):
        files = []
        if node["kind"] == "type":
            files = [node["file"]]
        elif node["kind"] == "method":
            files = [node["file"]]
        elif node.get("ext") == ".cs":
            files = [node["path"]]
        for f in files:
            rows = conn.execute(
                "SELECT full_name, kind, bases, is_mono FROM types WHERE file=?",
                (f,)).fetchall()
            for r in rows:
                header = f"## `{r['full_name']}`"
                if r["bases"]:
                    header += f" : {r['bases']}"
                parts.append(header)
                mems = conn.execute(
                    "SELECT kind, signature, modifiers, is_lifecycle, serialized"
                    " FROM members WHERE owner=? ORDER BY kind DESC, line",
                    (r["full_name"],)).fetchall()
                for m in mems:
                    tag = ""
                    if m["is_lifecycle"]:
                        tag = " ⚡lifecycle"
                    elif m["serialized"]:
                        tag = " 🔗serialized"
                    parts.append(f"- [{m['kind']}] `{m['signature']}`{tag}")
    conn.close()

    body = "\n".join(parts)
    if len(body) > budget_chars:
        body = body[:budget_chars] + "\n\n...(类型签名区超出预算,截断)"

    imp = impact(project_root, target)
    imp_text = _fmt_impact_section(imp, max(400, budget_chars - len(body)))
    return body + "\n\n" + imp_text
