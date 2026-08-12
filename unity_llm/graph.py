"""图谱构建与持久化(SQLite,零依赖)。

数据模型(「代码图 + 序列化图」的并集):

  assets  : guid -> 路径/类型(脚本、prefab、scene、ScriptableObject、贴图...)
  types   : C# 类型(class/struct/interface/enum),关联到脚本资产 guid
  members : C# 方法/字段,标记生命周期回调、序列化字段
  calls   : 方法 -> 调用目标。**带行号、接收者、接收者推断类型,以及
            建图期解析出的 resolved_owner + confidence**
  refs    : 资产 -> 资产 的序列化引用(prefab YAML 里的 guid 边,含字段名)
  events  : UnityEvent 持久化绑定(按钮 onClick 等)—— 代码里看不见的调用边

调用边解析(build 期一次做完,查询期只查表):
  接收者类型短名 -> 候选类型 -> 沿基类链找到真正声明该方法的类型。
  解析成功 = high;类型唯一但方法不在链上(基类在外部/正则漏了)= medium;
  接收者类型未知则退化为按名字匹配 —— 名字全项目唯一才给 medium,
  否则 low(Refresh/Init/Show 这类热名字会拉出成百上千噪声,必须降级)。

图存在 <项目>/.unity-llm/graph.db,构建是幂等的(全量重建)。
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Dict, List, Optional, Set, Tuple

from . import __version__
from .meta import build_guid_map, iter_assets, is_external
from .unity_yaml import parse_unity_yaml
from .csharp import parse_csharp, short_type

DB_DIR = ".unity-llm"
DB_NAME = "graph.db"

SCRIPT_EXTS = {".cs"}
YAML_ASSET_EXTS = {".prefab", ".unity", ".asset", ".controller", ".anim",
                   ".mat", ".physicMaterial", ".playable", ".mask", ".preset"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS assets (
    guid TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    ext TEXT NOT NULL,
    external INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    bases TEXT NOT NULL,
    modifiers TEXT NOT NULL,
    attributes TEXT NOT NULL,
    file TEXT NOT NULL,
    guid TEXT NOT NULL,
    line INTEGER NOT NULL,
    is_mono INTEGER NOT NULL DEFAULT 0,
    is_scriptable INTEGER NOT NULL DEFAULT 0,
    external INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,        -- 所属类型 full_name
    kind TEXT NOT NULL,         -- method / field
    name TEXT NOT NULL,
    signature TEXT NOT NULL,
    modifiers TEXT NOT NULL,
    attributes TEXT NOT NULL,
    extra TEXT NOT NULL,        -- 方法: return_type;字段: field_type
    line INTEGER NOT NULL,
    file TEXT NOT NULL,
    guid TEXT NOT NULL,
    is_lifecycle INTEGER NOT NULL DEFAULT 0,
    is_message INTEGER NOT NULL DEFAULT 0,
    serialized INTEGER NOT NULL DEFAULT 0,
    code_used INTEGER NOT NULL DEFAULT 0,   -- 字段:声明之外在类体里被读写过
    external INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_owner TEXT NOT NULL,    -- 调用发起方类型 full_name
    src_member TEXT NOT NULL,   -- 调用发起方方法名
    kind TEXT NOT NULL,         -- call / dotcall / new / api_generic / api_string / method_ref
    target TEXT NOT NULL,       -- 原文目标,如 "enemy.TakeDamage"
    name TEXT NOT NULL DEFAULT '',       -- 方法名 / 类型名
    recv TEXT NOT NULL DEFAULT '',       -- 接收者表达式原文
    recv_type TEXT NOT NULL DEFAULT '',  -- 接收者推断类型短名
    arg TEXT NOT NULL DEFAULT '',
    line INTEGER NOT NULL DEFAULT 0,
    file TEXT NOT NULL DEFAULT '',
    resolved_owner TEXT NOT NULL DEFAULT '',  -- 解析出的被调类型 full_name
    confidence TEXT NOT NULL DEFAULT 'low',
    external INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS refs (
    src_guid TEXT NOT NULL,     -- 引用发起方资产
    src_path TEXT NOT NULL,
    field TEXT NOT NULL,        -- 序列化字段名
    dst_guid TEXT NOT NULL,     -- 被引用资产
    dst_fileid INTEGER NOT NULL DEFAULT 0,
    context TEXT NOT NULL DEFAULT '' -- GameObject 名 / 文档类型等
);
CREATE TABLE IF NOT EXISTS events (
    src_guid TEXT NOT NULL,     -- 绑定所在 prefab/scene
    src_path TEXT NOT NULL,
    field TEXT NOT NULL,        -- m_OnClick / onValueChanged ...
    method TEXT NOT NULL,       -- 被绑定的方法名
    target_type TEXT NOT NULL,  -- m_TargetAssemblyTypeName(去程序集)
    target_guid TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS dotted_members (
    name TEXT NOT NULL,         -- 被 `x.Name` 形式访问过的成员名(非调用)
    file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dotted_name ON dotted_members(name);
CREATE INDEX IF NOT EXISTS idx_refs_dst ON refs(dst_guid);
CREATE INDEX IF NOT EXISTS idx_refs_src ON refs(src_guid);
CREATE INDEX IF NOT EXISTS idx_calls_target ON calls(target);
CREATE INDEX IF NOT EXISTS idx_calls_name ON calls(name);
CREATE INDEX IF NOT EXISTS idx_calls_resolved ON calls(resolved_owner);
CREATE INDEX IF NOT EXISTS idx_members_name ON members(name);
CREATE INDEX IF NOT EXISTS idx_members_owner ON members(owner);
CREATE INDEX IF NOT EXISTS idx_types_name ON types(name);
CREATE INDEX IF NOT EXISTS idx_events_method ON events(method);
"""

# 同名方法在超过这么多类型里出现,就算「热名字」:接收者类型未知时
# 按名字匹配会拉出全项目噪声(实测 Refresh 一个名字能带出 513 个假依赖)。
HOT_NAME_MIN = 2


def db_path(project_root: str) -> str:
    return os.path.join(project_root, DB_DIR, DB_NAME)


def connect(project_root: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(project_root))
    conn.row_factory = sqlite3.Row
    return conn


def _asset_ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


# ------------------------------------------------------------------ 解析调用边

def _resolve_calls(cur) -> Dict[str, int]:
    """建图后一次性解析所有调用边,写回 resolved_owner / confidence。"""
    type_by_short: Dict[str, List[str]] = {}
    bases_of: Dict[str, List[str]] = {}
    for r in cur.execute("SELECT name, full_name, bases FROM types"):
        type_by_short.setdefault(r["name"], []).append(r["full_name"])
        bases_of[r["full_name"]] = [b for b in r["bases"].split(",") if b]

    declares: Set[Tuple[str, str]] = set()      # (type full_name, member name)
    owners_of: Dict[str, Set[str]] = {}         # member name -> 声明它的类型集合
    for r in cur.execute("SELECT owner, name FROM members"):
        declares.add((r["owner"], r["name"]))
        owners_of.setdefault(r["name"], set()).add(r["owner"])

    def chain(full: str, depth: int = 6) -> List[str]:
        """自身 + 基类链(短名转 full_name,遇歧义取第一个)。"""
        out = [full]
        cur_full = full
        while depth > 0:
            depth -= 1
            nxt = None
            for b in bases_of.get(cur_full, []):
                cands = type_by_short.get(b)
                if cands:
                    nxt = cands[0]
                    break
            if not nxt or nxt in out:
                break
            out.append(nxt)
            cur_full = nxt
        return out

    def resolve(kind: str, name: str, recv_type: str) -> Tuple[str, str]:
        # 类型级边:new X() / GetComponent<X>() —— 目标就是类型本身
        if kind in ("new", "api_generic"):
            cands = type_by_short.get(short_type(name) or name, [])
            if len(cands) == 1:
                return cands[0], "high"
            return "", "medium" if cands else "low"
        if kind == "api_string":
            # 字符串调用(SendMessage 等):目标类型天然不可知,但这是真耦合
            return "", "medium"
        if recv_type:
            cands = type_by_short.get(recv_type, [])
            if len(cands) == 1:
                for t in chain(cands[0]):
                    if (t, name) in declares:
                        return t, "high"
                # 类型确定但方法不在链上:基类可能在 Unity/第三方,仍算相关
                return cands[0], "medium"
            hits = [c for c in cands
                    for t in chain(c) if (t, name) in declares]
            if len(set(hits)) == 1:
                return hits[0], "high"
            if hits:
                return "", "low"
        # 接收者类型未知:退化为按名字匹配,名字唯一才可信
        owners = owners_of.get(name, set())
        if len(owners) == 1:
            return next(iter(owners)), "medium"
        if len(owners) >= HOT_NAME_MIN:
            return "", "low"
        return "", "low"

    updates = []
    counts = {"high": 0, "medium": 0, "low": 0}
    for r in cur.execute("SELECT id, kind, name, recv_type FROM calls"):
        owner, conf = resolve(r["kind"], r["name"], r["recv_type"])
        counts[conf] += 1
        updates.append((owner, conf, r["id"]))
    cur.executemany("UPDATE calls SET resolved_owner=?, confidence=? WHERE id=?",
                    updates)
    return counts


def _guid_for_path(root: str, rel: str) -> str:
    """单个资产的 guid:权威映射表优先,其次读 .meta。"""
    from .meta import load_guid_override, _guid_of_meta
    for g, p in load_guid_override(root).items():
        if p == rel:
            return g
    return _guid_of_meta(os.path.join(root, rel + ".meta")) or ""


def _insert_csharp(cur, text: str, rel: str, guid: str, ext_flag: int) -> dict:
    """解析并写入一个 .cs 文件,返回 {types, members, calls} 计数。"""
    parsed = parse_csharp(text, rel)
    n = {"types": 0, "members": 0, "calls": 0}
    cur.executemany("INSERT INTO dotted_members(name, file) VALUES (?,?)",
                    [(d, rel) for d in parsed.dotted_members])
    for t in parsed.types:
        cur.execute(
            "INSERT INTO types(name, full_name, kind, namespace, bases,"
            " modifiers, attributes, file, guid, line, is_mono, is_scriptable,"
            " external) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.name, t.full_name, t.kind, t.namespace, ",".join(t.bases),
             t.modifiers, t.attributes, rel, guid, t.line,
             int(t.is_mono_behaviour), int(t.is_scriptable_object), ext_flag))
        n["types"] += 1
        for meth in t.methods:
            cur.execute(
                "INSERT INTO members(owner, kind, name, signature, modifiers,"
                " attributes, extra, line, file, guid, is_lifecycle, is_message,"
                " serialized, external) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.full_name, "method", meth.name, meth.signature,
                 meth.modifiers, meth.attributes, meth.return_type, meth.line,
                 rel, guid, int(meth.is_lifecycle), int(meth.is_message), 0,
                 ext_flag))
            n["members"] += 1
            cur.executemany(
                "INSERT INTO calls(src_owner, src_member, kind, target, name,"
                " recv, recv_type, arg, line, file, external)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(t.full_name, meth.name, c.kind, c.target, c.name, c.recv,
                  c.recv_type, c.arg, c.line, rel, ext_flag)
                 for c in meth.calls])
            n["calls"] += len(meth.calls)
        for fld in t.fields:
            cur.execute(
                "INSERT INTO members(owner, kind, name, signature, modifiers,"
                " attributes, extra, line, file, guid, is_lifecycle, is_message,"
                " serialized, code_used, external) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.full_name, "field", fld.name, f"{fld.type} {fld.name}",
                 fld.modifiers, fld.attributes, fld.type, fld.line, rel, guid,
                 0, 0, int(fld.serialized), int(fld.code_used), ext_flag))
            n["members"] += 1
    return n


def _insert_yaml(cur, text: str, rel: str, guid: str) -> dict:
    """解析并写入一个 Unity YAML 资产,返回 {refs, events} 计数。"""
    yf = parse_unity_yaml(text)
    n = {"refs": 0, "events": 0}
    for doc in yf.docs:
        ctx = doc.name or doc.class_name
        if doc.refs:
            cur.executemany(
                "INSERT INTO refs(src_guid, src_path, field, dst_guid,"
                " dst_fileid, context) VALUES (?,?,?,?,?,?)",
                [(guid, rel, r.field, r.guid, r.file_id, ctx)
                 for r in doc.refs])
            n["refs"] += len(doc.refs)
        if doc.events:
            cur.executemany(
                "INSERT INTO events(src_guid, src_path, field, method,"
                " target_type, target_guid, context) VALUES (?,?,?,?,?,?,?)",
                [(guid, rel, e.field, e.method, e.target_type,
                  e.target_guid or "", ctx) for e in doc.events])
            n["events"] += len(doc.events)
    return n


def build(project_root: str, verbose: bool = False,
          include_external_code: bool = True) -> dict:
    """全量构建图谱。返回统计信息 dict。"""
    root = os.path.abspath(project_root)
    os.makedirs(os.path.join(root, DB_DIR), exist_ok=True)
    t0 = time.time()

    guid_map, guid_source = build_guid_map(root)

    if os.path.exists(db_path(root)):
        os.remove(db_path(root))
    conn = connect(root)
    cur = conn.cursor()
    cur.executescript(SCHEMA)

    stats = {"assets": 0, "scripts": 0, "types": 0, "members": 0,
             "calls": 0, "yaml_assets": 0, "refs": 0, "events": 0, "errors": 0}

    # 1) 资产表
    path_to_guid = {}
    for guid, rel in guid_map.items():
        path_to_guid[rel] = guid
        cur.execute(
            "INSERT OR REPLACE INTO assets(guid, path, ext, external)"
            " VALUES (?,?,?,?)",
            (guid, rel, _asset_ext(rel), int(is_external(rel, root))))
    stats["assets"] = len(guid_map)

    # 2) C# 代码图
    for abspath, rel in iter_assets(root):
        ext = _asset_ext(rel)
        if ext not in SCRIPT_EXTS:
            continue
        ext_flag = int(is_external(rel, root))
        if ext_flag and not include_external_code:
            continue
        guid = path_to_guid.get(rel, "")
        try:
            with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                n = _insert_csharp(cur, f.read(), rel, guid, ext_flag)
        except Exception:
            stats["errors"] += 1
            continue
        stats["scripts"] += 1
        stats["types"] += n["types"]
        stats["members"] += n["members"]
        stats["calls"] += n["calls"]

    # 3) 序列化引用图 + UnityEvent 绑定
    for abspath, rel in iter_assets(root):
        ext = _asset_ext(rel)
        if ext not in YAML_ASSET_EXTS:
            continue
        guid = path_to_guid.get(rel, "")
        try:
            with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                n = _insert_yaml(cur, f.read(), rel, guid)
        except Exception:
            stats["errors"] += 1
            continue
        stats["yaml_assets"] += 1
        stats["refs"] += n["refs"]
        stats["events"] += n["events"]

    # 4) 调用边解析(接收者类型 -> 基类链 -> 真正的被调类型)
    stats["confidence"] = _resolve_calls(cur)

    for k, v in (("built_at", str(int(time.time()))), ("version", __version__),
                 ("guid_source", guid_source)):
        cur.execute("INSERT OR REPLACE INTO meta_kv(key, value) VALUES (?,?)",
                    (k, v))
    conn.commit()
    conn.close()
    stats["guid_source"] = guid_source
    stats["seconds"] = round(time.time() - t0, 2)
    return stats


def update_files(project_root: str, rel_paths) -> dict:
    """增量更新:只重建指定文件(项目相对路径)的图数据。

    日常开发改了几个脚本/prefab 后用这个,不用全量 build。
    支持:修改 / 新增 / 删除(文件不存在则清除其图数据)。
    调用边解析是全局的(名字索引跨文件),每次 update 后整体重跑一次
    —— 实测大项目约几秒,远快于全量重建。
    """
    root = os.path.abspath(project_root)
    t0 = time.time()
    conn = connect(root)
    cur = conn.cursor()
    # 老版本建的库缺 code_used 等列,增量插入会直接报 SQL 错;提前给人话
    cols = {r[1] for r in cur.execute("PRAGMA table_info(members)")}
    if "code_used" not in cols:
        conn.close()
        raise RuntimeError(
            "graph.db 是旧版本 schema(缺 members.code_used),"
            "增量更新不可用。先跑一次 unity-llm build 全量重建。")
    out = {"updated": [], "deleted": [], "errors": []}

    for rel in rel_paths:
        rel = rel.replace("\\", "/").lstrip("/")
        abspath = os.path.join(root, rel.replace("/", os.sep))
        ext = _asset_ext(rel)
        # 先清掉这个文件的旧数据
        for table, col in (("types", "file"), ("members", "file"),
                           ("calls", "file"), ("refs", "src_path"),
                           ("events", "src_path"), ("dotted_members", "file")):
            cur.execute(f"DELETE FROM {table} WHERE {col}=?", (rel,))
        cur.execute("DELETE FROM assets WHERE path=?", (rel,))
        if not os.path.exists(abspath):
            out["deleted"].append(rel)
            continue
        guid = _guid_for_path(root, rel)
        if guid:
            cur.execute(
                "INSERT OR REPLACE INTO assets(guid, path, ext, external)"
                " VALUES (?,?,?,?)", (guid, rel, ext, int(is_external(rel, root))))
        try:
            with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            if ext in SCRIPT_EXTS:
                _insert_csharp(cur, text, rel, guid, int(is_external(rel, root)))
            elif ext in YAML_ASSET_EXTS:
                _insert_yaml(cur, text, rel, guid)
            out["updated"].append(rel)
        except Exception as e:
            out["errors"].append({"file": rel, "error": str(e)})

    out["confidence"] = _resolve_calls(cur)
    conn.commit()
    conn.close()
    out["seconds"] = round(time.time() - t0, 2)
    return out


def has_graph(project_root: str) -> bool:
    return os.path.exists(db_path(os.path.abspath(project_root)))
