"""查询层:影响面 / 引用 / Unity 感知死代码 / 组件清单 / 悬空引用校验。

置信度语义(建图期算好,查询期只是转述):
  high   : 接收者类型已确定,且沿基类链找到了声明该方法的类型
  medium : 类型确定但方法在链外(基类在引擎/第三方),或方法名全项目唯一,
           或字符串调用(SendMessage 之类,目标天然不可知)
  low    : 接收者类型未知且方法名在多个类型里重名 —— 名字匹配噪声,默认不返回

默认只返回 high + medium。`include_low=True` 才会带上 low(通常是几百条噪声)。
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Set

from .graph import connect, has_graph
from .unity_yaml import CLASS_NAMES

DEFAULT_LIMIT = 60
GOOD_CONF = ("high", "medium")
_CONF_ORDER = {"high": 0, "medium": 1, "low": 2}


def _require_graph(project_root: str) -> None:
    if not has_graph(project_root):
        raise RuntimeError(
            "图谱不存在,请先运行: python -m unity_llm build --project <path>")


def _cap(items: List, limit: int, key=None) -> Dict:
    """输出封顶 + 保留总数,避免一次返回几千条把上下文冲爆。"""
    total = len(items)
    if key:
        items = sorted(items, key=key)
    if total <= limit:
        return {"items": items, "truncated": None}
    return {"items": items[:limit],
            "truncated": {"total": total, "shown": limit}}


# ---------------------------------------------------------------- resolve

# 模糊路径匹配时优先选「有内容可分析」的资产,别掉进同名的贴图目录里
_PREFERRED_EXTS = (".cs", ".prefab", ".unity", ".asset", ".controller")


def _type_candidates(conn, t: str, limit: int = 6) -> List[Dict]:
    """前缀同名的类型候选(Shop -> ShopPage/ShopController/ShopView...)。"""
    rows = conn.execute(
        "SELECT full_name, kind, file FROM types WHERE name LIKE ?"
        " ORDER BY external, length(name) LIMIT ?", (t + "%", limit)).fetchall()
    return [{"full_name": r["full_name"], "kind": r["kind"], "file": r["file"]}
            for r in rows]


def _asset_node(row, **extra) -> Dict:
    out = {"kind": "asset", "guid": row["guid"], "path": row["path"],
           "ext": row["ext"]}
    out.update(extra)
    return out


def _guids_of_type(conn, full_name: str) -> List[str]:
    """一个类型所有 partial 文件的 guid(prefab 挂的是带 MonoBehaviour 的那份)。"""
    seen = []
    for r in conn.execute(
            "SELECT DISTINCT guid FROM types WHERE full_name=? AND guid!=''",
            (full_name,)):
        if r["guid"] not in seen:
            seen.append(r["guid"])
    return seen


def _pick_primary_type_row(rows) -> Dict:
    """partial 多行里挑展示用的主文件:有基类 / is_mono 的优先。"""
    def score(r):
        return (int(r["is_mono"] or 0), int(bool(r["bases"])),
                -len(r["file"] or ""))
    return max(rows, key=score)


def _type_node_from_rows(rows) -> Dict:
    primary = _pick_primary_type_row(rows)
    files, guids = [], []
    for r in rows:
        if r["file"] and r["file"] not in files:
            files.append(r["file"])
        if r["guid"] and r["guid"] not in guids:
            guids.append(r["guid"])
    node = {"kind": "type", "id": primary["id"], "name": primary["name"],
            "full_name": primary["full_name"], "file": primary["file"],
            "guid": primary["guid"], "is_mono": int(any(r["is_mono"] for r in rows)),
            "bases": primary["bases"] or next((r["bases"] for r in rows if r["bases"]), ""),
            "guids": guids, "files": files}
    if len(files) > 1:
        node["partial"] = True
    return node


def resolve_target(conn, target: str) -> Dict:
    """把用户输入的目标解析成图里的节点(guid / 路径 / 类名 / 方法名)。

    解析优先级:精确 guid > 精确路径 > 精确类型 > 精确方法 > 模糊路径。
    模糊路径命中时会标 `fuzzy: True` 并附上同前缀的类型候选 —— 否则
    「Shop 解析成同名贴图目录、返回 0 依赖」会被模型误读成「没人用,能删」。
    """
    t = target.strip()
    row = conn.execute("SELECT * FROM assets WHERE guid=?", (t,)).fetchone()
    if row:
        return _asset_node(row)
    row = conn.execute("SELECT * FROM assets WHERE path=?", (t,)).fetchone()
    if row:
        return _asset_node(row)
    # 增量删除保留 tombstone,使旧 guid/路径仍可解释已有的悬空入边。
    if _has_table(conn, "deleted_assets"):
        row = conn.execute(
            "SELECT * FROM deleted_assets WHERE guid=? OR path=?"
            " ORDER BY deleted_at DESC LIMIT 1", (t, t)).fetchone()
        if row:
            return _asset_node(row, deleted=True)
    # 类型优先于模糊路径,避免 "Enemy" 被匹配成 Enemy.cs
    # 同 full_name 多行 = partial,合并而不是当成 ambiguous
    rows = conn.execute(
        "SELECT * FROM types WHERE full_name=? ORDER BY is_mono DESC, length(file)",
        (t,)).fetchall()
    if rows:
        return _type_node_from_rows(rows)
    rows = conn.execute(
        "SELECT * FROM types WHERE name=? ORDER BY external, length(full_name)",
        (t,)).fetchall()
    if rows:
        by_fn: Dict[str, List] = {}
        for r in rows:
            by_fn.setdefault(r["full_name"], []).append(r)
        if len(by_fn) == 1:
            return _type_node_from_rows(next(iter(by_fn.values())))
        return {"kind": "ambiguous", "of": "type",
                "candidates": list(by_fn.keys())}
    # 方法名 / 字段名:允许 Owner.Method 或 Owner.Field
    owner_filter, mname = None, t
    if "." in t:
        owner_filter, mname = t.rsplit(".", 1)
    rows = conn.execute("SELECT * FROM members WHERE name=? AND kind='method'",
                        (mname,)).fetchall()
    if owner_filter:
        rows = [r for r in rows
                if r["owner"] == owner_filter or r["owner"].endswith("." + owner_filter)]
    if len(rows) == 1:
        r = rows[0]
        return {"kind": "method", "owner": r["owner"], "name": r["name"],
                "signature": r["signature"], "file": r["file"], "guid": r["guid"]}
    if rows:
        # 同一 owner 的同名方法(partial 重复插入极少见)去重后再判
        owners = list(dict.fromkeys(r["owner"] for r in rows))
        if len(owners) == 1:
            r = rows[0]
            return {"kind": "method", "owner": r["owner"], "name": r["name"],
                    "signature": r["signature"], "file": r["file"],
                    "guid": r["guid"]}
        return {"kind": "ambiguous", "of": "method",
                "candidates": [f"{r['owner']}.{r['name']}" for r in rows][:40]}
    if owner_filter:
        frows = conn.execute(
            "SELECT * FROM members WHERE name=? AND kind='field'",
            (mname,)).fetchall()
        frows = [r for r in frows
                 if r["owner"] == owner_filter
                 or r["owner"].endswith("." + owner_filter)]
        if frows:
            r = frows[0]
            return {"kind": "field", "owner": r["owner"], "name": r["name"],
                    "signature": r["signature"], "file": r["file"],
                    "guid": r["guid"]}

    # 模糊路径:同名候选可能有好几个(Texture/Shop、Script/Shop...)
    matched_by = "path_suffix"
    cands = conn.execute(
        "SELECT * FROM assets WHERE path LIKE ? ORDER BY external, length(path)"
        " LIMIT 20", (f"%/{t}",)).fetchall()
    if not cands and not t.startswith("Assets"):
        matched_by = "path_substring"
        cands = conn.execute(
            "SELECT * FROM assets WHERE path LIKE ? ORDER BY external,"
            " length(path) LIMIT 20", (f"%/{t}%",)).fetchall()
    types = _type_candidates(conn, t)
    if cands:
        best = next((r for r in cands if r["ext"] in _PREFERRED_EXTS), cands[0])
        others = [r["path"] for r in cands if r["path"] != best["path"]][:8]
        node = _asset_node(best, fuzzy=True, matched_by=matched_by)
        if others:
            node["other_path_matches"] = others
        if types:
            node["type_candidates"] = types
        node["hint"] = _fuzzy_hint(t, best, types, matched_by, others)
        return node
    if types:
        return {"kind": "ambiguous", "of": "type_prefix",
                "candidates": [c["full_name"] for c in types],
                "hint": f"没有叫 `{t}` 的类型/资产,但有 {len(types)} 个同前缀类型;"
                        "请挑一个具体的再查。"}
    return {"kind": "unknown", "input": t}


def _fuzzy_hint(t: str, best, types: List[Dict], matched_by: str,
                others: List[str]) -> str:
    parts = []
    how = "子串" if matched_by == "path_substring" else "路径尾段"
    if not best["ext"]:
        parts.append(f"`{t}` 没有精确匹配,退化成{how}模糊匹配,命中的是**目录** "
                     f"`{best['path']}` —— 目录本身几乎不会有序列化入边,"
                     "结果为 0 不代表没人使用。")
    else:
        parts.append(f"`{t}` 没有精确匹配,{how}模糊命中了 `{best['path']}`。")
    if others:
        parts.append(f"同样匹配的还有 {len(others)} 个路径。")
    if types:
        names = ", ".join(c["full_name"] for c in types[:5])
        parts.append(f"你要找的可能是这些类型之一: {names}"
                     f"{' 等' if len(types) > 5 else ''}。")
    return " ".join(parts)


def _type_scope(conn, node: Dict):
    """目标涉及的 (类型 full_name 集合, 方法/字段名集合, guid 集合)。"""
    owners: Set[str] = set()
    names: Set[str] = set()
    guids: Set[str] = set()
    short: Set[str] = set()
    if node["kind"] == "asset":
        guids.add(node["guid"])
        for r in conn.execute("SELECT name, full_name FROM types WHERE guid=?",
                              (node["guid"],)):
            owners.add(r["full_name"])
            short.add(r["name"])
    elif node["kind"] == "type":
        owners.add(node["full_name"])
        short.add(node["name"])
        for g in node.get("guids") or ([node["guid"]] if node.get("guid") else []):
            if g:
                guids.add(g)
        if not guids:
            guids.update(_guids_of_type(conn, node["full_name"]))
    elif node["kind"] in ("method", "field"):
        owners.add(node["owner"])
        names.add(node["name"])
        short.add(node["owner"].split(".")[-1])
        guids.update(_guids_of_type(conn, node["owner"]))
        if node.get("guid"):
            guids.add(node["guid"])
        return owners, names, guids, short
    for o in list(owners):
        for m in conn.execute(
                "SELECT name FROM members WHERE owner=? AND kind='method'"
                " AND is_lifecycle=0 AND is_message=0", (o,)):
            names.add(m["name"])
    return owners, names, guids, short


# ---------------------------------------------------------------- impact

def _unresolved_hint(node: Dict) -> str:
    if node.get("hint"):
        return node["hint"]
    if node["kind"] == "ambiguous":
        return (f"目标有多个候选({node.get('of', '')}),请传更精确的名字"
                "(带命名空间的 full_name 或 Owner.Method)。")
    return (f"图里找不到 `{node.get('input', '')}`。先用 unity_find 搜名字,"
            "或确认图谱是否需要 unity_update / unity_rebuild。")


def _present_node(node: Dict) -> Dict:
    """对外只返回决策所需身份,隐藏查询内部 id/guid 并集等膨胀字段。"""
    keep = {
        "asset": ("kind", "path", "guid", "ext", "deleted", "fuzzy",
                  "matched_by", "other_path_matches", "type_candidates"),
        "type": ("kind", "name", "full_name", "file", "bases", "is_mono",
                 "partial"),
        "method": ("kind", "owner", "name", "signature", "file"),
        "field": ("kind", "owner", "name", "signature", "file"),
        "ambiguous": ("kind", "of", "candidates"),
        "unknown": ("kind", "input"),
    }.get(node.get("kind"), tuple(node))
    return {k: node[k] for k in keep if k in node}


def impact(project_root: str, target: str, depth: int = 3,
           include_low: bool = False, include_external: bool = False,
           limit: int = DEFAULT_LIMIT) -> Dict:
    """「改了这个东西,会炸到谁」—— 代码 + 序列化 + UnityEvent 三通道反向依赖。

    代码依赖分「外部调用方」和「内部自调用」:外部才是「改了会炸到别人」,
    内部自调用只是这个类自己的方法互调。截断时优先保留外部。
    """
    conn = connect(project_root)
    node = resolve_target(conn, target)
    out: Dict = {"target": target, "resolved": _present_node(node),
                 "code_dependents": [],
                 "asset_dependents": [], "event_bindings": [],
                 "summary": {}}
    if node["kind"] not in ("asset", "type", "method", "field"):
        conn.close()
        out["hint"] = _unresolved_hint(node)
        return out
    if node.get("hint"):
        out["hint"] = node["hint"]

    owners, names, guids, short = _type_scope(conn, node)
    confs = GOOD_CONF + ("low",) if include_low else GOOD_CONF
    conf_marks = ",".join("?" * len(confs))

    def scope_of(src_owner: str) -> str:
        return "internal" if src_owner in owners else "external"

    # --- 代码依赖:解析出的调用边(resolved_owner 命中目标类型)
    code: List[Dict] = []
    seen = set()
    for o in owners:
        sql = ("SELECT DISTINCT src_owner, src_member, kind, name, arg, file, line,"
               " confidence, external FROM calls WHERE resolved_owner=?"
               f" AND confidence IN ({conf_marks})")
        for c in conn.execute(sql, (o,) + confs):
            if c["external"] and not include_external:
                continue
            if node["kind"] in ("method", "field") and c["name"] not in names:
                continue
            key = (c["src_owner"], c["src_member"], c["name"], c["kind"])
            if key in seen:
                continue
            seen.add(key)
            called = (f"{c['name']}.{c['arg']}"
                      if c["kind"] in ("field_call", "relay") and c["arg"]
                      else c["name"])
            code.append({"caller": f"{c['src_owner']}.{c['src_member']}",
                         "calls": called, "via": c["kind"],
                         "at": f"{c['file']}:{c['line']}",
                         "confidence": c["confidence"],
                         "scope": scope_of(c["src_owner"])})
    # 字符串调用:SendMessage("Foo") / StartCoroutine("Foo")
    for name in names:
        for c in conn.execute(
                "SELECT DISTINCT src_owner, src_member, kind, file, line FROM calls"
                " WHERE kind='api_string' AND arg=?", (name,)):
            code.append({"caller": f"{c['src_owner']}.{c['src_member']}",
                         "calls": name, "via": c["kind"],
                         "at": f"{c['file']}:{c['line']}",
                         "confidence": "medium",
                         "scope": scope_of(c["src_owner"])})
    # 继承
    for s in short:
        for t in conn.execute(
                "SELECT full_name, file, line FROM types"
                " WHERE (',' || bases || ',') LIKE ?", (f"%,{s},%",)):
            code.append({"caller": t["full_name"], "calls": s, "via": "inherits",
                         "at": f"{t['file']}:{t['line']}", "confidence": "high",
                         "scope": "external"})

    # --- 资产依赖:引用这些 guid 的资产(传递闭包)
    assets: List[Dict] = []
    visited: Set[str] = set()
    frontier = list(guids)
    d = 0
    while frontier and d < depth:
        d += 1
        nxt = []
        for g in frontier:
            if g in visited:
                continue
            visited.add(g)
            for r in conn.execute(
                    "SELECT DISTINCT src_guid, src_path, field, context FROM refs"
                    " WHERE dst_guid=?", (g,)):
                assets.append({"asset": r["src_path"], "field": r["field"],
                               "context": r["context"], "depth": d})
                if r["src_guid"] and r["src_guid"] not in visited:
                    nxt.append(r["src_guid"])
        frontier = nxt

    # --- UnityEvent 绑定(按钮 onClick 之类,代码里看不见的调用者)
    events: List[Dict] = []
    for name in names:
        for e in conn.execute(
                "SELECT DISTINCT src_path, field, method, target_type, context"
                " FROM events WHERE method=?", (name,)):
            if e["target_type"] and owners and not any(
                    e["target_type"] == o or e["target_type"].endswith("." + o)
                    or o.endswith("." + e["target_type"]) for o in owners):
                continue
            events.append({"asset": e["src_path"], "event": e["field"],
                           "method": e["method"], "on": e["context"]})

    from .meta import load_config, path_matches
    noise = list(load_config(project_root).get("dead_code_exclude") or [])
    dropped_assets = 0
    if noise:
        kept = []
        for a in assets:
            if path_matches(a["asset"], noise):
                dropped_assets += 1
            else:
                kept.append(a)
        assets = kept
        events = [e for e in events if not path_matches(e["asset"], noise)]

    conn.close()
    # 截断时外部调用方优先(内部自调用信息价值低,别占预算)
    ck = _cap(code, limit, key=lambda x: (0 if x["scope"] == "external" else 1,
                                          _CONF_ORDER.get(x["confidence"], 3),
                                          x["caller"]))
    ak = _cap(assets, limit, key=lambda x: (x["depth"], x["asset"]))
    ek = _cap(events, limit)
    out["code_dependents"] = ck["items"]
    out["asset_dependents"] = ak["items"]
    out["event_bindings"] = ek["items"]
    ext_code = [c for c in code if c["scope"] == "external"]
    out["summary"] = {
        "code_dependents": len(code),
        "external_callers": len(ext_code),
        "internal_calls": len(code) - len(ext_code),
        "high_confidence": len([c for c in code if c["confidence"] == "high"]),
        "direct_asset_dependents": len({
            a["asset"] for a in assets if a["depth"] == 1}),
        "transitive_assets": len({
            a["asset"] for a in assets if a["depth"] > 1}),
        "event_bindings": len(events),
    }
    if dropped_assets:
        out["summary"]["excluded_noise_assets"] = dropped_assets
    if code and not ext_code:
        out["summary"]["note_scope"] = (
            "代码依赖全部是内部自调用(这个类自己的方法互调),"
            "没有外部调用方 —— 但序列化引用/UnityEvent 通道仍可能有耦合。")
    for k, v in (("code_dependents", ck), ("asset_dependents", ak),
                 ("event_bindings", ek)):
        if v["truncated"]:
            out["summary"].setdefault("truncated", {})[k] = v["truncated"]
    return out


# ---------------------------------------------------------------- refs

def find_refs(project_root: str, target: str, include_low: bool = False,
              limit: int = DEFAULT_LIMIT) -> Dict:
    """「谁引用了这个资产 / 脚本」—— 序列化引用 + 代码引用,不展开传递。"""
    conn = connect(project_root)
    node = resolve_target(conn, target)
    out: Dict = {"target": target, "resolved": _present_node(node),
                 "serialized_refs": [], "code_refs": [], "summary": {}}
    if node["kind"] not in ("asset", "type", "method", "field"):
        conn.close()
        out["hint"] = _unresolved_hint(node)
        return out
    if node.get("hint"):
        out["hint"] = node["hint"]
    owners, names, guids, short = _type_scope(conn, node)
    confs = GOOD_CONF + ("low",) if include_low else GOOD_CONF
    conf_marks = ",".join("?" * len(confs))

    srefs = []
    for g in guids:
        for r in conn.execute(
                "SELECT src_path, field, context FROM refs WHERE dst_guid=?"
                " ORDER BY src_path", (g,)):
            srefs.append(dict(r))
    crefs = []
    # 目标是方法/字段时必须按名字过滤:否则会把整个类型的边都端上来。
    name_clause = " AND name=?" if node["kind"] in ("method", "field") else ""
    name_args = (node["name"],) if node["kind"] in ("method", "field") else ()
    for o in owners:
        for c in conn.execute(
                "SELECT DISTINCT src_owner, src_member, kind, name, file, line,"
                f" confidence FROM calls WHERE resolved_owner=?"
                f" AND confidence IN ({conf_marks}){name_clause}",
                (o,) + confs + name_args):
            crefs.append({"caller": f"{c['src_owner']}.{c['src_member']}",
                          "calls": c["name"], "kind": c["kind"],
                          "at": f"{c['file']}:{c['line']}",
                          "confidence": c["confidence"],
                          "scope": ("internal" if c["src_owner"] in owners
                                    else "external")})
    conn.close()
    sk = _cap(srefs, limit)
    ck = _cap(crefs, limit,
              key=lambda x: (0 if x["scope"] == "external" else 1,
                             _CONF_ORDER.get(x["confidence"], 3)))
    out["serialized_refs"], out["code_refs"] = sk["items"], ck["items"]
    out["summary"] = {
        "serialized_refs": len(srefs),
        "code_refs": len(crefs),
        "external_code_refs": len([c for c in crefs
                                   if c["scope"] == "external"]),
    }
    for k, v in (("serialized_refs", sk), ("code_refs", ck)):
        if v["truncated"]:
            out["summary"].setdefault("truncated", {})[k] = v["truncated"]
    return out


# ---------------------------------------------------------------- components

def _component_label(row) -> str:
    """一条组件对象记录的展示名:MonoBehaviour 显示脚本文件,其余显示类名。"""
    if row["class_id"] == 114:  # MonoBehaviour
        if row["path"]:
            return "MonoBehaviour: " + os.path.basename(row["path"])
        return "MonoBehaviour: " + (row["script_guid"][:8] if row["script_guid"] else "?")
    return CLASS_NAMES.get(row["class_id"], f"Class{row['class_id']}")


# 参与层级建树的变换节点:Transform 与它的子类 RectTransform(UI 项目绝大多数节点)。
TRANSFORM_CLASS_IDS = (4, 224)


def _build_hierarchy_text(conn, src_guid: str):
    """把 prefab/scene 的 fileID 对象图还原成 GameObject 层级树(缩进文本)。

    层级关系来自 Transform/RectTransform 的 m_Father(fileID=0 是根),GameObject
    名来自 m_Name,组件通过 m_GameObject 反挂到所属 GameObject。这是 grep / AST
    都看不见、只能靠 Editor MCP 实时列出来的一层 —— 这里从静态 YAML 还原。
    嵌套 prefab 的子节点 Transform 在别的文件里,本文件视角下它们的父级不可见,
    会作为根列出(不丢节点),嵌套数通过 summary.nested_prefabs 提示。
    返回 (缩进文本, GameObject 数)。对象图缺失(旧 graph.db 未重建)时返回空。
    """
    if not _has_table(conn, "objects"):
        return "", 0
    rows = conn.execute(
        "SELECT o.fileid, o.class_id, o.name, o.go_fileid, o.father_fileid,"
        " o.script_guid, a.path FROM objects o"
        " LEFT JOIN assets a ON a.guid=o.script_guid WHERE o.src_guid=?",
        (src_guid,)).fetchall()
    if not rows:
        return "", 0
    go_name: Dict[int, str] = {}
    transforms: Dict[int, tuple] = {}   # 变换节点 fileid -> (go_fileid, father_fileid)
    comps: Dict[int, List[str]] = {}
    for r in rows:
        if r["class_id"] == 1:
            go_name[r["fileid"]] = r["name"] or "(unnamed)"
        elif r["class_id"] in TRANSFORM_CLASS_IDS:
            transforms[r["fileid"]] = (r["go_fileid"], r["father_fileid"])
        elif r["go_fileid"]:
            comps.setdefault(r["go_fileid"], []).append(_component_label(r))

    children: Dict[int, List[int]] = {}
    transform_go = {go for go, _ in transforms.values()}
    roots: Set[int] = set()
    for go, father in transforms.values():
        if go not in go_name:
            continue  # 孤儿变换节点(m_GameObject 指向的 GO 在嵌套 prefab 里)
        if father == 0:
            roots.add(go)
            continue
        parent = transforms.get(father)
        if parent is None or parent[0] not in go_name:
            # 父变换节点不在本文件(嵌套 prefab):本文件视角下它是根,不丢节点
            roots.add(go)
        else:
            children.setdefault(parent[0], []).append(go)
    # 变换节点在子 prefab 里的 GameObject:本文件内没有它的变换信息,也是根
    for go in go_name:
        if go not in transform_go:
            roots.add(go)

    for k in children:
        children[k].sort(key=lambda g: go_name.get(g, ""))
    lines: List[str] = []

    def render(go: int, indent: int):
        pad = "  " * indent
        lines.append(pad + go_name.get(go, "?"))
        for label in sorted(set(comps.get(go, []))):
            lines.append(pad + "  - " + label)
        for child in children.get(go, []):
            render(child, indent + 1)

    for go in sorted(roots, key=lambda g: go_name.get(g, "")):
        render(go, 0)
    return "\n".join(lines), len(go_name)


def components(project_root: str, target: str, limit: int = DEFAULT_LIMIT) -> Dict:
    """prefab/scene 上挂了哪些脚本(m_Script 边),或某个脚本被谁挂载。"""
    conn = connect(project_root)
    node = resolve_target(conn, target)
    out: Dict = {"target": target, "resolved": _present_node(node)}
    if node["kind"] not in ("asset", "type", "method", "field"):
        conn.close()
        out["hint"] = _unresolved_hint(node)
        return out
    if node.get("hint"):
        out["hint"] = node["hint"]
    if node["kind"] == "asset" and node["ext"] in (".prefab", ".unity"):
        rows = conn.execute(
            "SELECT DISTINCT r.dst_guid g, r.context ctx, a.path p FROM refs r"
            " LEFT JOIN assets a ON a.guid=r.dst_guid"
            " WHERE r.src_guid=? AND r.field='m_Script'", (node["guid"],)).fetchall()
        items = [{"script": r["p"] or f"<unresolved guid {r['g']}>",
                  "on": r["ctx"]} for r in rows]
        cap = _cap(items, limit)
        out["scripts"] = cap["items"]
        hierarchy, go_count = _build_hierarchy_text(conn, node["guid"])
        nested = 0
        if _has_table(conn, "objects"):
            nested = conn.execute(
                "SELECT count(*) FROM objects WHERE src_guid=? AND class_id=1001",
                (node["guid"],)).fetchone()[0]
        out["summary"] = {"scripts": len(items), "game_objects": go_count}
        if nested:
            out["summary"]["nested_prefabs"] = nested
        if hierarchy:
            out["hierarchy"] = hierarchy
        if cap["truncated"]:
            out["summary"]["truncated"] = cap["truncated"]
    else:
        _, _, guids, _ = _type_scope(conn, node)
        items = []
        for g in guids:
            for r in conn.execute(
                    "SELECT DISTINCT src_path, context FROM refs"
                    " WHERE dst_guid=? AND field='m_Script' ORDER BY src_path", (g,)):
                items.append({"asset": r["src_path"], "on": r["context"]})
        cap = _cap(items, limit)
        out["mounted_on"] = cap["items"]
        out["summary"] = {"mounted_on": len(items)}
        if cap["truncated"]:
            out["summary"]["truncated"] = cap["truncated"]
    conn.close()
    return out


# ---------------------------------------------------------------- dead code

def _dir_bucket(path: str, segs: int = 3) -> str:
    parts = path.split("/")[:segs]
    return "/".join(parts)


def _has_table(conn, name: str) -> bool:
    """老版本 db 可能没有新表:查不到就当空,不炸。"""
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


def dead_code(project_root: str, include_external: bool = False,
              limit: int = DEFAULT_LIMIT, exclude=None) -> Dict:
    """Unity 感知的死代码候选。

    排除:生命周期/消息回调、public/protected(可能是 API)、字符串调用、
    **UnityEvent 绑定的方法(prefab/scene 里指名调用)**、
    第三方目录、以及 `.unity-llm.json` 里 `dead_code_exclude` 配的路径
    (美术试验田 / Demo 这类项目内噪声目录,查询期过滤,不用重建图谱)。

    明细按目录聚合到 `by_directory`:总数上千时,分布比前 60 条明细有用。
    """
    from .meta import load_config, path_matches
    cfg = load_config(project_root)
    patterns = list(cfg.get("dead_code_exclude") or [])
    patterns += list(exclude or [])

    conn = connect(project_root)
    called = set()
    for c in conn.execute("SELECT DISTINCT name, arg, kind FROM calls"):
        if c["name"]:
            called.add(c["name"])
        if c["kind"] in ("api_string", "field_call", "relay") and c["arg"]:
            called.add(c["arg"].split("(")[0])
    bound = {r["method"] for r in conn.execute("SELECT DISTINCT method FROM events")}
    serialized_fields = {r["field"] for r in
                         conn.execute("SELECT DISTINCT field FROM refs") if r["field"]}

    excluded_counts: Dict[str, int] = {}

    def skip(path: str) -> bool:
        if not path_matches(path, patterns):
            return False
        excluded_counts[_dir_bucket(path)] = \
            excluded_counts.get(_dir_bucket(path), 0) + 1
        return True

    suspects = []
    for m in conn.execute(
            "SELECT * FROM members WHERE kind='method' AND is_lifecycle=0"
            " AND is_message=0"):
        if m["external"] and not include_external:
            continue
        mods = m["modifiers"]
        if "private" not in mods and "internal" not in mods:
            continue
        if m["name"] in called or m["name"] in bound:
            continue
        if m["name"].startswith(("get_", "set_")) or m["name"] == ".ctor":
            continue
        if skip(m["file"]):
            continue
        suspects.append({"method": f"{m['owner']}.{m['signature']}",
                         "file": f"{m['file']}:{m['line']}",
                         "modifiers": mods})
    unused_fields = []
    has_code_used = any(r[1] == "code_used" for r in
                        conn.execute("PRAGMA table_info(members)"))
    dotted = {r[0] for r in conn.execute("SELECT DISTINCT name FROM dotted_members")} \
        if _has_table(conn, "dotted_members") else set()
    for m in conn.execute("SELECT * FROM members WHERE kind='field' AND serialized=1"):
        if m["external"] and not include_external:
            continue
        if m["name"] in serialized_fields or m["name"] in called:
            continue
        # 字段在自己类体里被读写过(count++ / hp = 0):调用图看不见,但不是死字段
        if has_code_used and m["code_used"]:
            continue
        # 被别的类点过(other.hp):同样不是死字段(按名字判定,宁可漏报不误报)
        if m["name"] in dotted:
            continue
        if skip(m["file"]):
            continue
        unused_fields.append({"field": f"{m['owner']}.{m['name']}",
                              "file": f"{m['file']}:{m['line']}"})
    conn.close()
    sk, fk = _cap(suspects, limit), _cap(unused_fields, limit)

    def buckets(items, key):
        c: Dict[str, int] = {}
        for it in items:
            c[_dir_bucket(it[key].split(":")[0])] = \
                c.get(_dir_bucket(it[key].split(":")[0]), 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1])[:15])

    summary = {"dead_methods": len(suspects),
               "maybe_unused_fields": len(unused_fields),
               "truncated": {k: v["truncated"] for k, v in
                             (("dead_methods", sk),
                              ("maybe_unused_fields", fk))
                             if v["truncated"]} or None}
    out = {"dead_methods": sk["items"], "maybe_unused_fields": fk["items"],
           "by_directory": {"dead_methods": buckets(suspects, "file"),
                            "maybe_unused_fields": buckets(unused_fields, "file")},
           "summary": summary,
           "disclaimer": "已排除生命周期/消息回调、字符串调用、UnityEvent(Inspector)"
                         "绑定;但反射、热更框架(xLua/ILRuntime/HybridCLR)、"
                         "Timeline/Animation Event 入口静态分析不可见,结果需人工复核。"}
    if patterns:
        summary["excluded_patterns"] = patterns
        summary["excluded_hits"] = sum(excluded_counts.values())
    elif len(suspects) > 300:
        from .meta import CONFIG_FILE, SUGGESTED_EXCLUDES
        top = list(out["by_directory"]["dead_methods"])[:3]
        summary["tune_hint"] = (
            f"候选 {len(suspects)} 条,基本不可能全是真死代码。"
            f"在项目根 {CONFIG_FILE} 里配 dead_code_exclude 过滤噪声目录"
            f"(当前最多的三个:{', '.join(top)};常见值:"
            f"{', '.join(SUGGESTED_EXCLUDES[:4])})。")
    return out


# ---------------------------------------------------------------- validate

BUILTIN_GUIDS = {"0000000000000000e000000000000000",
                 "0000000000000000f000000000000000"}


def _guid_health(conn) -> Dict:
    """非 32 位十六进制 guid 的资产,分「真影响」和「可忽略」。

    只有同时满足「在 Assets/ 下(是你自己的资产)」或「确实有序列化入边」时
    才算真问题。Packages/Library/LocalPackages 里那些被改写过 meta 的包资产
    没有入边,报出来纯属噪声 —— 尤其是已经用 guidmap.tsv 的项目,
    再劝一遍「去导出 guidmap」是明确的假警报。
    """
    rows = conn.execute(
        "SELECT guid, path FROM assets WHERE length(guid)!=32").fetchall()
    out = {"nonstandard": len(rows)}
    if not rows:
        return out
    risky = []
    for r in rows:
        n = conn.execute("SELECT count(*) FROM refs WHERE dst_guid=?",
                         (r["guid"],)).fetchone()[0]
        if r["path"].startswith("Assets/") or n:
            risky.append({"path": r["path"], "inbound_refs": n})
    out["risky"] = len(risky)
    out["examples"] = [x["path"] for x in risky[:5]]
    if risky:
        out["message"] = (
            f"{len(risky)} 个资产的 .meta guid 不是 32 位十六进制且在 Assets/ 下"
            "或确实被引用。常见原因:guidmap.tsv 导出后又新增了资产(已过期)。"
            "请在 Unity 里重新执行 Tools/unity-llm/Dump GUID Map 覆盖 "
            ".unity-llm/guidmap.tsv 再 build,否则这些资产拿不到序列化入边。")
    else:
        out["message"] = (
            f"{len(rows)} 个非标准 guid 资产全部在 Assets/ 之外"
            "(Packages/Library/LocalPackages 等)且没有任何序列化入边,可忽略。")
    return out


def validate(project_root: str, limit: int = DEFAULT_LIMIT) -> Dict:
    """悬空引用体检:prefab/scene 指向的 guid 在项目里找不到对应资产。"""
    conn = connect(project_root)
    rows = conn.execute(
        "SELECT r.dst_guid g, count(*) c, min(r.src_path) p, min(r.field) f"
        " FROM refs r LEFT JOIN assets a ON a.guid=r.dst_guid"
        " WHERE a.guid IS NULL GROUP BY r.dst_guid ORDER BY c DESC").fetchall()
    dangling = [{"guid": r["g"], "ref_count": r["c"], "example": r["p"],
                 "field": r["f"]} for r in rows if r["g"] not in BUILTIN_GUIDS]
    total_refs = conn.execute("SELECT count(*) FROM refs").fetchone()[0]
    health = _guid_health(conn)
    src = conn.execute("SELECT value FROM meta_kv WHERE key='guid_source'").fetchone()
    conn.close()
    cap = _cap(dangling, limit)
    out = {"dangling_guids": cap["items"],
           "summary": {"distinct_dangling": len(dangling),
                       "total_refs": total_refs,
                       "guid_source": src[0] if src else "meta"}}
    if cap["truncated"]:
        out["summary"]["truncated"] = cap["truncated"]
    if health["nonstandard"]:
        out["summary"]["nonstandard_guid_assets"] = health["nonstandard"]
        out["summary"]["nonstandard_with_refs"] = health["risky"]
        key = "hint" if health["risky"] else "note"
        out["summary"][key] = health["message"]
        if health["risky"]:
            out["summary"]["nonstandard_examples"] = health["examples"]
    return out


# ---------------------------------------------------------------- find/stats

def find_symbols(project_root: str, pattern: str, limit: int = 12,
                 kind: str = None) -> Dict:
    """按名字搜类型/成员/资产。默认精简索引,避免一次倒出几千 token。"""
    conn = connect(project_root)
    like = f"%{pattern}%"
    want = {kind} if kind in ("type", "member", "asset") else {
        "type", "member", "asset"}
    out: Dict = {}
    if "type" in want:
        rows = conn.execute(
            "SELECT name, full_name, kind, file FROM types"
            " WHERE name LIKE ? OR full_name LIKE ? ORDER BY external, length(name)"
            " LIMIT ?", (like, like, limit * 3)).fetchall()
        seen = set()
        types = []
        for r in rows:
            if r["full_name"] in seen:
                continue
            seen.add(r["full_name"])
            types.append({"name": r["name"], "full_name": r["full_name"],
                          "kind": r["kind"], "file": r["file"]})
            if len(types) >= limit:
                break
        out["types"] = types
    if "member" in want:
        members = [{"owner": r["owner"], "kind": r["kind"], "name": r["name"],
                    "file": r["file"]}
                   for r in conn.execute(
                       "SELECT owner, kind, name, file FROM members"
                       " WHERE name LIKE ? OR owner LIKE ?"
                       " ORDER BY external LIMIT ?",
                       (like, like, limit))]
        out["members"] = members
    if "asset" in want:
        assets = [{"path": r["path"], "ext": r["ext"]}
                  for r in conn.execute(
                      "SELECT path, ext FROM assets WHERE path LIKE ?"
                      " ORDER BY CASE ext"
                      " WHEN '.cs' THEN 0 WHEN '.prefab' THEN 1"
                      " WHEN '.unity' THEN 2 WHEN '.asset' THEN 3 ELSE 9 END,"
                      " external, length(path) LIMIT ?",
                      (like, limit))]
        out["assets"] = assets
    conn.close()
    return out


def stats(project_root: str) -> Dict:
    conn = connect(project_root)

    def one(sql):
        return conn.execute(sql).fetchone()[0]
    out = {
        "assets": one("SELECT count(*) FROM assets"),
        "scripts": one("SELECT count(*) FROM assets WHERE ext='.cs'"),
        "types": one("SELECT count(*) FROM types"),
        "members": one("SELECT count(*) FROM members"),
        "calls": one("SELECT count(*) FROM calls"),
        "serialized_refs": one("SELECT count(*) FROM refs"),
        "unity_events": one("SELECT count(*) FROM events"),
        "mono_behaviours": one("SELECT count(*) FROM types WHERE is_mono=1"),
        "scriptable_objects": one("SELECT count(*) FROM types WHERE is_scriptable=1"),
        "lifecycle_methods": one("SELECT count(*) FROM members WHERE is_lifecycle=1"),
        "prefabs_scenes": one(
            "SELECT count(*) FROM assets WHERE ext IN ('.prefab','.unity')"),
        "external_types": one("SELECT count(*) FROM types WHERE external=1"),
    }
    out["deleted_asset_tombstones"] = (
        one("SELECT count(*) FROM deleted_assets")
        if _has_table(conn, "deleted_assets") else 0)
    out["call_confidence"] = {
        r["confidence"]: r["c"] for r in conn.execute(
            "SELECT confidence, count(*) c FROM calls GROUP BY confidence")}
    for key in ("built_at", "guid_source", "version"):
        row = conn.execute("SELECT value FROM meta_kv WHERE key=?", (key,)).fetchone()
        out[key] = row[0] if row else None
    bad = _guid_health(conn)
    if bad["nonstandard"]:
        out["nonstandard_guid_assets"] = bad["nonstandard"]
        if bad["risky"]:
            out["guid_warning"] = bad["message"]
            out["guid_warning_examples"] = bad["examples"]
        else:
            out["guid_note"] = bad["message"]
    conn.close()
    return out


def to_json(obj, compact: bool = False) -> str:
    if compact:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(obj, ensure_ascii=False, indent=2)
