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

图存在 <项目>/.unity-llm/graph.db。全量构建先写临时库,成功后原子替换,
失败时不会破坏上一份可用图。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

from . import __version__
from .meta import (GUID_MAP_FILE, HEX_GUID_RE, PROJECT_VERSION_FILE,
                   _guid_of_meta, build_guid_map, detect_engine,
                   guid_format_report, iter_assets, is_external)
from .unity_yaml import parse_unity_yaml
from .visual_assets import (parse_animator, parse_timeline,
                            parse_shadergraph, parse_vfx)
from .csharp import parse_csharp, short_type

DB_DIR = ".unity-llm"
DB_NAME = "graph.db"

SCRIPT_EXTS = {".cs"}
YAML_ASSET_EXTS = {".prefab", ".unity", ".asset", ".controller", ".anim",
                   ".mat", ".physicMaterial", ".playable", ".mask", ".preset"}
# JSON 序列化的视觉资产(走 visual_assets 的 JSON 分支,不吃 YAML parser)
JSON_ASSET_EXTS = {".shadergraph", ".vfx"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS assets (
    guid TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    ext TEXT NOT NULL,
    external INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deleted_assets (
    guid TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    ext TEXT NOT NULL,
    deleted_at INTEGER NOT NULL
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
    kind TEXT NOT NULL,         -- call / dotcall / new / api_generic / api_string / method_ref / field_call
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
CREATE TABLE IF NOT EXISTS objects (
    src_guid TEXT NOT NULL,     -- 所属资产(prefab/scene/asset)guid
    fileid INTEGER NOT NULL,    -- 本地 fileID(单个文件内唯一)
    class_id INTEGER NOT NULL,  -- 1=GameObject 4=Transform 114=MonoBehaviour ...
    name TEXT NOT NULL DEFAULT '',        -- GameObject 的 m_Name(组件通常为空)
    go_fileid INTEGER NOT NULL DEFAULT 0, -- 组件/Transform 所属 GameObject 的 fileID
    father_fileid INTEGER NOT NULL DEFAULT -1, -- Transform 的父 Transform fileID(0=根,-1=非 Transform)
    script_guid TEXT NOT NULL DEFAULT ''  -- MonoBehaviour 的 m_Script guid
);
CREATE TABLE IF NOT EXISTS animator_states (
    src_guid TEXT NOT NULL,       -- 所属 .controller 资产 guid
    state_fileid TEXT NOT NULL,   -- AnimatorState 的 fileID(字符串,可能为负)
    name TEXT NOT NULL DEFAULT '',
    motion_guid TEXT NOT NULL DEFAULT '',  -- m_Motion 指向的 clip guid
    layer TEXT NOT NULL DEFAULT '',        -- 所属层(子状态机为 `层名/子机名`)
    is_default INTEGER NOT NULL DEFAULT 0  -- 是否所在状态机的 m_DefaultState
);
CREATE TABLE IF NOT EXISTS animator_transitions (
    src_guid TEXT NOT NULL,
    from_fileid TEXT NOT NULL,    -- 源 state fileID
    to_fileid TEXT NOT NULL DEFAULT '',   -- m_DstState 的 fileID
    conditions TEXT NOT NULL DEFAULT '',  -- JSON 数组 [{event,mode,threshold}]
    layer TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS timeline_tracks (
    src_guid TEXT NOT NULL,       -- 所属 .playable 资产 guid
    track_fileid TEXT NOT NULL,
    track_type TEXT NOT NULL DEFAULT '',  -- m_Script guid(查询期 join 成脚本路径)
    display_name TEXT NOT NULL DEFAULT '',
    parent_fileid TEXT NOT NULL DEFAULT ''  -- GroupTrack 父轨道 fileID(空=顶层)
);
CREATE TABLE IF NOT EXISTS timeline_clips (
    src_guid TEXT NOT NULL,
    track_fileid TEXT NOT NULL,
    clip_fileid TEXT NOT NULL,
    start REAL NOT NULL DEFAULT 0,
    duration REAL NOT NULL DEFAULT 0,
    display_name TEXT NOT NULL DEFAULT '',
    asset_guid TEXT NOT NULL DEFAULT '',  -- clip 引用的外部资产(AnimationClip 等)
    asset_kind TEXT NOT NULL DEFAULT ''   -- playable asset 的脚本 guid(内联 clip 靠它判类型)
);
CREATE TABLE IF NOT EXISTS visual_refs (
    src_guid TEXT NOT NULL,
    kind TEXT NOT NULL,           -- subgraph / vfx_ref
    name TEXT NOT NULL DEFAULT '',
    dst_guid TEXT NOT NULL DEFAULT ''
);
"""

INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_dotted_name ON dotted_members(name);
CREATE INDEX IF NOT EXISTS idx_refs_dst ON refs(dst_guid);
CREATE INDEX IF NOT EXISTS idx_refs_src ON refs(src_guid);
CREATE INDEX IF NOT EXISTS idx_calls_target ON calls(target);
CREATE INDEX IF NOT EXISTS idx_calls_name ON calls(name);
CREATE INDEX IF NOT EXISTS idx_calls_resolved ON calls(resolved_owner);
CREATE INDEX IF NOT EXISTS idx_calls_kind_arg ON calls(kind, arg);
CREATE INDEX IF NOT EXISTS idx_members_name ON members(name);
CREATE INDEX IF NOT EXISTS idx_members_owner ON members(owner);
CREATE INDEX IF NOT EXISTS idx_types_name ON types(name);
CREATE INDEX IF NOT EXISTS idx_events_method ON events(method);
CREATE INDEX IF NOT EXISTS idx_deleted_path ON deleted_assets(path);
CREATE INDEX IF NOT EXISTS idx_objects_src ON objects(src_guid);
CREATE INDEX IF NOT EXISTS idx_objects_go ON objects(src_guid, go_fileid);
CREATE INDEX IF NOT EXISTS idx_animator_states_src ON animator_states(src_guid);
CREATE INDEX IF NOT EXISTS idx_timeline_tracks_src ON timeline_tracks(src_guid);
CREATE INDEX IF NOT EXISTS idx_timeline_clips_track ON timeline_clips(track_fileid);
"""

# 同名方法在超过这么多类型里出现,就算「热名字」:接收者类型未知时
# 按名字匹配会拉出全项目噪声(实测 Refresh 一个名字能带出 513 个假依赖)。
HOT_NAME_MIN = 2


def db_path(project_root: str) -> str:
    return os.path.join(project_root, DB_DIR, DB_NAME)


def _connect_path(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


BUILD_TIME_HINT = ("耗时随项目规模/机器/磁盘波动很大 —— 本项目实测 1~9 分钟都出现过,"
                   "stderr 有分阶段进度,没输出才是卡住")


GUIDMAP_EXPORT_HINT = (
    "先在 Unity 里导出 guid 映射表,否则脚本挂载点会全查成 0:\n"
    "  菜单 Tools → unity-llm → Dump GUID Map(tools/UnityLlmGuidDump.cs 复制到 Assets/Editor/),\n"
    "  或用 Unity MCP 的 execute_code 直接跑:遍历 AssetDatabase.GetAllAssetPaths(),\n"
    "  把 AssetPathToGUID(path) + '\\t' + path 写进 <项目>/.unity-llm/guidmap.tsv。")


def guidmap_missing(project_root: str, sample: int = 400) -> bool:
    """本项目的 .meta guid 被重写过、且还没导出 guidmap.tsv?

    抽样判断,别为了一句提示去全量扫 3 万个 .meta。
    """
    root = os.path.abspath(project_root)
    if os.path.exists(os.path.join(root, GUID_MAP_FILE)):
        return False
    seen = nonstd = 0
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, "Assets")):
        for fn in filenames:
            if not fn.endswith(".meta"):
                continue
            g = _guid_of_meta(os.path.join(dirpath, fn))
            if g:
                seen += 1
                if not HEX_GUID_RE.match(g):
                    nonstd += 1
            if seen >= sample:
                return nonstd > seen * 0.02
    return seen > 0 and nonstd > seen * 0.02


def guidmap_hint(project_root: str) -> str:
    """建图前的 guidmap 提示;不需要就返回空串,不占上下文。"""
    return GUIDMAP_EXPORT_HINT if guidmap_missing(project_root) else ""


def build_command(project_root: str) -> str:
    """给用户的建图命令。

    用 sys.executable + 当前 PYTHONPATH,保证复制即可跑:
    unity_llm 常常只在 MCP 那个 venv 里可导入,提示 `python -m unity_llm`
    会让用户撞上 No module named unity_llm。--project 用相对路径 `.`。
    """
    py = sys.executable or "python"
    pp = os.environ.get("PYTHONPATH", "")
    # 命令里带了 cd,PYTHONPATH 必须转绝对,否则相对项(".")换目录后就失效
    if pp:
        pp = os.pathsep.join(os.path.abspath(p) for p in pp.split(os.pathsep) if p)
    prefix = f'PYTHONPATH="{pp}" ' if pp else ""
    return (f'cd "{os.path.abspath(project_root)}" && '
            f'{prefix}"{py}" -m unity_llm build --project .')


def connect(project_root: str) -> sqlite3.Connection:
    path = db_path(os.path.abspath(project_root))
    if not os.path.exists(path):
        raise RuntimeError(
            "图谱不存在,请先运行: " + build_command(project_root))
    return _connect_path(path)


def ensure_schema(project_root: str) -> None:
    """给旧 graph.db 补兼容表/索引,不要求为小型 schema 追加做全量重建。"""
    if not has_graph(project_root):
        return
    conn = connect(project_root)
    try:
        conn.executescript(SCHEMA)
        conn.executescript(INDEX_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _asset_ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


class _Progress:
    """全量 build 的 stderr 心跳。默认关闭,避免库调用/测试被刷屏。

    中型项目初次建图要扫完全部 .meta,期间如果没有任何输出,用户会以为卡住。
    写 stderr + flush,不污染 stdout 的最终 JSON。
    """

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.t0 = time.time()
        self._last = self.t0

    def log(self, msg: str) -> None:
        if not self.enabled:
            return
        elapsed = time.time() - self.t0
        print(f"build: {msg}  ({elapsed:.0f}s)", file=sys.stderr, flush=True)
        self._last = time.time()

    def maybe(self, n: int, total: int, label: str, every_sec: float = 5.0) -> None:
        if not self.enabled:
            return
        now = time.time()
        if (now - self._last) < every_sec:
            return
        extra = f"{n}/{total}" if total else str(n)
        self.log(f"{label} {extra}")


# ------------------------------------------------------------------ 解析调用边

def _resolve_calls(cur, progress: Optional[_Progress] = None) -> Dict[str, int]:
    """建图后一次性解析所有调用边,写回 resolved_owner / confidence。"""
    type_by_short: Dict[str, List[str]] = {}
    bases_of: Dict[str, List[str]] = {}
    for r in cur.execute("SELECT name, full_name, bases FROM types"):
        bucket = type_by_short.setdefault(r["name"], [])
        if r["full_name"] not in bucket:
            bucket.append(r["full_name"])
        bases_of[r["full_name"]] = [b for b in r["bases"].split(",") if b]

    declares: Set[Tuple[str, str]] = set()      # (type full_name, member name)
    owners_of: Dict[str, Set[str]] = {}         # member name -> 声明它的类型集合
    for r in cur.execute("SELECT owner, name FROM members"):
        declares.add((r["owner"], r["name"]))
        owners_of.setdefault(r["name"], set()).add(r["owner"])

    def namespace_of(full: str) -> str:
        return full.rpartition(".")[0]

    def preferred(cands: List[str], src_owner: str) -> Optional[str]:
        """短名重名时只接受唯一候选或调用方同 namespace 的唯一候选。"""
        if len(cands) == 1:
            return cands[0]
        src_ns = namespace_of(src_owner)
        local = [c for c in cands if namespace_of(c) == src_ns]
        return local[0] if len(local) == 1 else None

    def chain(full: str, depth: int = 6) -> List[str]:
        """自身 + 可无歧义解析的基类/接口图;绝不随意取重名候选。"""
        out = []
        queue = [(full, 0)]
        while queue:
            cur_full, level = queue.pop(0)
            if cur_full in out:
                continue
            out.append(cur_full)
            if level >= depth:
                continue
            for b in bases_of.get(cur_full, []):
                cands = type_by_short.get(b, [])
                nxt = preferred(cands, cur_full)
                if nxt and nxt not in out:
                    queue.append((nxt, level + 1))
        return out

    def resolve(kind: str, name: str, recv_type: str,
                src_owner: str) -> Tuple[str, str]:
        # 类型级边:new X() / GetComponent<X>() —— 目标就是类型本身
        if kind in ("new", "api_generic"):
            cands = type_by_short.get(short_type(name) or name, [])
            picked = preferred(cands, src_owner)
            if picked:
                return picked, "high"
            return "", "medium" if cands else "low"
        if kind == "api_string":
            # 字符串调用(SendMessage 等):目标类型天然不可知,但这是真耦合
            return "", "medium"
        if kind in ("field_call", "relay"):
            # Type.Field.Method():recv_type 是类型,name 是字段
            # relay 是 0.5.0 旧名,读老 graph.db 时仍能解析
            cands = type_by_short.get(recv_type, [])
            picked = preferred(cands, src_owner)
            if picked:
                return picked, "high"
            return "", "medium" if cands else "low"
        if recv_type:
            cands = type_by_short.get(recv_type, [])
            picked = preferred(cands, src_owner)
            if picked:
                for t in chain(picked):
                    if (t, name) in declares:
                        return t, "high"
                # 类型确定但方法不在链上:基类可能在 Unity/第三方,仍算相关
                return picked, "medium"
            hits = {t for c in cands for t in chain(c)
                    if (t, name) in declares}
            if len(hits) == 1:
                # 没有 using 信息时,重名接收者即使只有一个候选声明该方法,
                # 也不能升级成 high。
                return next(iter(hits)), "medium"
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
    total = cur.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    n = 0
    for r in cur.execute(
            "SELECT id, src_owner, kind, name, recv_type FROM calls"):
        owner, conf = resolve(r["kind"], r["name"], r["recv_type"],
                              r["src_owner"])
        counts[conf] += 1
        updates.append((owner, conf, r["id"]))
        n += 1
        if progress is not None:
            progress.maybe(n, total, "[6/6] 解析调用边")
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
    n = {"refs": 0, "events": 0, "objects": 0}
    for doc in yf.docs:
        go_name = yf.game_object_name(doc.go_fileid) if doc.go_fileid else ""
        ctx = go_name or doc.name or doc.class_name
        # fileID 对象图:每个 doc 都是一条本地对象记录,供层级树/组件挂载查询
        cur.execute(
            "INSERT INTO objects(src_guid, fileid, class_id, name, go_fileid,"
            " father_fileid, script_guid) VALUES (?,?,?,?,?,?,?)",
            (guid, doc.file_id, doc.class_id, doc.name,
             doc.go_fileid or 0, doc.father_fileid if doc.father_fileid is not None else -1,
             doc.script_guid or ""))
        n["objects"] += 1
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


def _insert_visual_structure(cur, text: str, rel: str, guid: str) -> dict:
    """解析并写入视觉资产的内部结构(状态机/轨道/clip 时序/subgraph 引用)。

    结构解析失败只吞掉异常、不影响主图 —— 这些是增量能力,不该拖垮建图。
    """
    n = {"states": 0, "transitions": 0, "tracks": 0, "clips": 0, "visual_refs": 0}
    ext = _asset_ext(rel)
    try:
        if ext == ".controller":
            a = parse_animator(text)
            for s in a["states"]:
                cur.execute(
                    "INSERT INTO animator_states(src_guid, state_fileid, name,"
                    " motion_guid, layer, is_default) VALUES (?,?,?,?,?,?)",
                    (guid, s["fileid"], s["name"], s["motion_guid"],
                     s.get("layer", ""), int(s.get("is_default", False))))
                n["states"] += 1
            for t in a["transitions"]:
                cur.execute(
                    "INSERT INTO animator_transitions(src_guid, from_fileid,"
                    " to_fileid, conditions, layer) VALUES (?,?,?,?,?)",
                    (guid, t["from"], t["to"],
                     json.dumps(t["conditions"], ensure_ascii=False),
                     t.get("layer", "")))
                n["transitions"] += 1
        elif ext == ".playable":
            tl = parse_timeline(text)
            for tk in tl["tracks"]:
                cur.execute(
                    "INSERT INTO timeline_tracks(src_guid, track_fileid,"
                    " track_type, display_name, parent_fileid) VALUES (?,?,?,?,?)",
                    (guid, tk["fileid"], tk["script_guid"], tk["display_name"],
                     tk.get("parent_fileid", "")))
                n["tracks"] += 1
                for c in tk["clips"]:
                    cur.execute(
                        "INSERT INTO timeline_clips(src_guid, track_fileid,"
                        " clip_fileid, start, duration, display_name, asset_guid,"
                        " asset_kind) VALUES (?,?,?,?,?,?,?,?)",
                        (guid, tk["fileid"], c["fileid"], c["start"], c["duration"],
                         c["display_name"], c["asset_guid"], c.get("asset_kind", "")))
                    n["clips"] += 1
        elif ext == ".shadergraph":
            for sub in parse_shadergraph(text)["subgraphs"]:
                cur.execute(
                    "INSERT INTO visual_refs(src_guid, kind, name, dst_guid)"
                    " VALUES (?,?,?,?)",
                    (guid, "subgraph", sub["name"], sub["guid"]))
                n["visual_refs"] += 1
        elif ext == ".vfx":
            for r in parse_vfx(text)["refs"]:
                cur.execute(
                    "INSERT INTO visual_refs(src_guid, kind, name, dst_guid)"
                    " VALUES (?,?,?,?)",
                    (guid, "vfx_ref", "", r["guid"]))
                n["visual_refs"] += 1
    except Exception as e:
        n["error"] = str(e)
    return n


def _propagate_mono(cur) -> int:
    """沿基类链把 is_mono 传给 ShopPage : BasePage : GameBehaviour : MonoBehaviour。

    解析期只看直接基类名,自定义 UI 基类(BasePage)的子类会漏标。
    只做 0→1,迭代到不动为止。
    """
    marked = 0
    changed = True
    while changed:
        changed = False
        rows = list(cur.execute("SELECT id, bases, is_mono FROM types"))
        mono_short = {r[0] for r in cur.execute(
            "SELECT name FROM types WHERE is_mono=1")}
        mono_short.update(("MonoBehaviour", "NetworkBehaviour"))
        for r in rows:
            if r["is_mono"]:
                continue
            bases = [b for b in (r["bases"] or "").split(",") if b]
            if any(b in mono_short or b.endswith("Behaviour") for b in bases):
                cur.execute("UPDATE types SET is_mono=1 WHERE id=?", (r["id"],))
                changed = True
                marked += 1
    return marked


def _swap_db(building_path: str, final_path: str) -> None:
    """把新库换上去。

    Windows 上如果别的进程(常见是还在跑的 MCP server)开着 graph.db,rename 会
    `PermissionError: [WinError 5]`,几分钟的 build 白跑。退路是用 sqlite 的备份 API
    把新库内容原地灌进旧文件 —— 旧句柄仍然有效,读者下次查询就看到新数据。
    """
    try:
        os.replace(building_path, final_path)
        return
    except OSError:
        pass
    src = sqlite3.connect(building_path)
    dst = sqlite3.connect(final_path, timeout=30)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        src.close()
        dst.close()
    try:
        os.remove(building_path)
    except OSError:
        pass


def build(project_root: str, verbose: bool = False,
          include_external_code: bool = True,
          allow_degraded_guid: bool = False) -> dict:
    """全量构建图谱。返回统计信息 dict。"""
    root = os.path.abspath(project_root)
    os.makedirs(os.path.join(root, DB_DIR), exist_ok=True)
    t0 = time.time()
    prog = _Progress(verbose)
    prog.log("开始全量建图(" + BUILD_TIME_HINT + ")")

    # 建表前先播报引擎:团结引擎和国际版 Unity 的 .meta guid 规则不同,
    # 这直接决定要不要先导 guidmap.tsv,得让人在等 1 分钟之前就知道。
    engine = detect_engine(root)
    if engine["engine"] == "tuanjie":
        prog.log(f"[0/6] 引擎: 团结引擎 {engine['version']}"
                 f"(Tuanjie {engine.get('tuanjie_version', '?')})"
                 " —— .meta guid 可能被重写,依赖 guidmap.tsv")
    else:
        prog.log(f"[0/6] 引擎: Unity {engine['version'] or '未识别'}(国际版)")

    prog.log("[1/6] 扫描 .meta 建立 guid 映射")
    guid_map, guid_source = build_guid_map(
        root, on_tick=lambda n: prog.maybe(n, 0, "[1/6] .meta"))
    prog.log(f"[1/6] guid 映射完成 {len(guid_map)} 条")
    guid_fmt = guid_format_report(guid_map)
    # 团结引擎/加密 .meta 的项目:meta 里的 guid 被重写成非 hex,而 prefab 的
    # m_Script guid 仍是明文 hex,两边对不上 -> 脚本挂载点会静默查成 0。
    # 一个挂载点全空的图比没有图更危险(会得出「这脚本没人用」的错误结论),
    # 所以这里直接拒绝出图,除非调用方明确要一个降级的图。
    if guid_fmt["nonstandard"] and "guidmap.tsv" not in guid_source:
        ratio = guid_fmt["nonstandard"] / max(1, guid_fmt["total"])
        msg = (f"{guid_fmt['nonstandard']}/{guid_fmt['total']} 个 .meta 的 guid "
               "不是 32 位 hex(.meta 被加密/重写,常见于团结引擎)。"
               "prefab 里的 guid 仍是明文,两边对不上,脚本挂载点会查成 0。\n"
               + GUIDMAP_EXPORT_HINT)
        if ratio > 0.02 and not allow_degraded_guid:
            raise RuntimeError(
                "拒绝建图:" + msg +
                "\n(确实想要一个挂载点不可用的降级图谱:build --allow-degraded-guid)")
        prog.log("[1/6] 警告: " + msg)

    final_path = db_path(root)
    building_path = final_path + ".building"
    old_tombstones = []
    if os.path.exists(final_path):
        try:
            old = _connect_path(final_path)
            if old.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='deleted_assets'").fetchone():
                old_tombstones = list(old.execute(
                    "SELECT guid,path,ext,deleted_at FROM deleted_assets"))
            old.close()
        except sqlite3.Error:
            old_tombstones = []
    if os.path.exists(building_path):
        os.remove(building_path)
    conn = _connect_path(building_path)
    cur = conn.cursor()
    # 只作用于随时可丢弃的临时库;最终库仍由原子替换保证完整性。
    cur.execute("PRAGMA journal_mode=OFF")
    cur.execute("PRAGMA synchronous=OFF")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.executescript(SCHEMA)

    stats = {"assets": 0, "scripts": 0, "types": 0, "members": 0,
             "calls": 0, "yaml_assets": 0, "refs": 0, "events": 0, "errors": 0}

    # 1) 资产表
    prog.log(f"[2/6] 写入资产表 {len(guid_map)}")
    path_to_guid = {}
    for guid, rel in guid_map.items():
        path_to_guid[rel] = guid
        cur.execute(
            "INSERT OR REPLACE INTO assets(guid, path, ext, external)"
            " VALUES (?,?,?,?)",
            (guid, rel, _asset_ext(rel), int(is_external(rel, root))))
    stats["assets"] = len(guid_map)
    for r in old_tombstones:
        if r["guid"] not in guid_map:
            cur.execute(
                "INSERT OR REPLACE INTO deleted_assets"
                "(guid,path,ext,deleted_at) VALUES (?,?,?,?)",
                (r["guid"], r["path"], r["ext"], r["deleted_at"]))

    cs_total = 0
    yaml_total = 0
    for rel in path_to_guid:
        ext = _asset_ext(rel)
        if ext in SCRIPT_EXTS:
            if include_external_code or not is_external(rel, root):
                cs_total += 1
        elif ext in YAML_ASSET_EXTS:
            yaml_total += 1

    # 2) C# 代码图
    prog.log(f"[3/6] 解析 C# ({cs_total} 脚本)")
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
        prog.maybe(stats["scripts"], cs_total, "[3/6] C#")
    prog.log(f"[3/6] C# 完成 {stats['scripts']} 脚本")

    # 3) 序列化引用图 + UnityEvent 绑定
    prog.log(f"[4/6] 解析 YAML ({yaml_total} prefab/scene/asset)")
    for abspath, rel in iter_assets(root):
        ext = _asset_ext(rel)
        if ext not in YAML_ASSET_EXTS:
            continue
        guid = path_to_guid.get(rel, "")
        try:
            with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            n = _insert_yaml(cur, text, rel, guid)
            _insert_visual_structure(cur, text, rel, guid)
        except Exception:
            stats["errors"] += 1
            continue
        stats["yaml_assets"] += 1
        stats["refs"] += n["refs"]
        stats["events"] += n["events"]
        prog.maybe(stats["yaml_assets"], yaml_total, "[4/6] YAML")
    prog.log(f"[4/6] YAML 完成 {stats['yaml_assets']} 资产")

    # 3.5) 视觉资产 JSON(ShaderGraph / VFX)
    for abspath, rel in iter_assets(root):
        ext = _asset_ext(rel)
        if ext not in JSON_ASSET_EXTS:
            continue
        guid = path_to_guid.get(rel, "")
        try:
            with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                _insert_visual_structure(cur, f.read(), rel, guid)
        except Exception:
            stats["errors"] += 1
            continue

    # 4) 自定义 MB 基类链(BasePage : GameBehaviour : MonoBehaviour)
    prog.log("[5/6] 传播 MonoBehaviour 标记")
    _propagate_mono(cur)

    # 5) 调用边解析(接收者类型 -> 基类链 -> 真正的被调类型)
    prog.log("[6/6] 解析调用边")
    stats["confidence"] = _resolve_calls(cur, progress=prog)

    for k, v in (("built_at", str(int(time.time()))), ("version", __version__),
                 ("guid_source", guid_source),
                 ("engine", engine["engine"]),
                 ("engine_version", engine["version"])):
        cur.execute("INSERT OR REPLACE INTO meta_kv(key, value) VALUES (?,?)",
                    (k, v))
    # 批量插入后再建索引,避免每行维护 B-tree。
    prog.log("建索引并落盘")
    cur.executescript(INDEX_SCHEMA)
    conn.commit()
    conn.close()
    _swap_db(building_path, final_path)
    stats["guid_source"] = guid_source
    stats["engine"] = engine["engine"]
    stats["engine_version"] = engine["version"]
    stats["guid_nonstandard"] = guid_fmt["nonstandard"]
    stats["guid_total"] = guid_fmt["total"]
    stats["seconds"] = round(time.time() - t0, 2)
    prog.log(
        f"完成 {stats['seconds']}s  assets={stats['assets']} "
        f"scripts={stats['scripts']} yaml={stats['yaml_assets']}")
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
    cur.executescript(SCHEMA)
    cur.executescript(INDEX_SCHEMA)
    # 老版本建的库缺 code_used 等列,增量插入会直接报 SQL 错;提前给人话
    cols = {r[1] for r in cur.execute("PRAGMA table_info(members)")}
    if "code_used" not in cols:
        conn.close()
        raise RuntimeError(
            "graph.db 是旧版本 schema(缺 members.code_used),"
            "增量更新不可用。先跑一次 unity-llm build 全量重建。")
    # 视觉结构表 0.7.2 加了列;CREATE TABLE IF NOT EXISTS 不会补,老库要显式 ALTER
    for table, col, decl in (("animator_states", "layer", "TEXT NOT NULL DEFAULT ''"),
                             ("animator_states", "is_default", "INTEGER NOT NULL DEFAULT 0"),
                             ("animator_transitions", "layer", "TEXT NOT NULL DEFAULT ''"),
                             ("timeline_tracks", "parent_fileid", "TEXT NOT NULL DEFAULT ''"),
                             ("timeline_clips", "asset_kind", "TEXT NOT NULL DEFAULT ''")):
        have = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
        if have and col not in have:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    out = {"updated": [], "deleted": [], "errors": []}

    for rel in rel_paths:
        rel = rel.replace("\\", "/").lstrip("/")
        abspath = os.path.join(root, rel.replace("/", os.sep))
        try:
            if os.path.commonpath((root, os.path.abspath(abspath))) != root:
                raise ValueError("路径越出项目根目录")
        except (ValueError, OSError) as e:
            out["errors"].append({"file": rel, "error": str(e)})
            continue
        ext = _asset_ext(rel)
        old_asset = cur.execute(
            "SELECT guid, path, ext FROM assets WHERE path=?", (rel,)).fetchone()
        # 先清掉这个文件的旧数据
        for table, col in (("types", "file"), ("members", "file"),
                           ("calls", "file"), ("refs", "src_path"),
                           ("events", "src_path"), ("dotted_members", "file")):
            cur.execute(f"DELETE FROM {table} WHERE {col}=?", (rel,))
        if old_asset:
            cur.execute("DELETE FROM objects WHERE src_guid=?",
                        (old_asset["guid"],))
            # 视觉结构(状态机/轨道/clip)也按 guid 清,不然改过的 controller/playable
            # 会留下旧状态 + 新状态两份
            for table in ("animator_states", "animator_transitions",
                          "timeline_tracks", "timeline_clips", "visual_refs"):
                cur.execute(f"DELETE FROM {table} WHERE src_guid=?",
                            (old_asset["guid"],))
        cur.execute("DELETE FROM assets WHERE path=?", (rel,))
        if not os.path.exists(abspath):
            if old_asset:
                # 保留最小 tombstone:查询旧路径/guid 仍能解释悬空入边,
                # 而 validate 仍会把它视为缺失资产。
                cur.execute(
                    "INSERT OR REPLACE INTO deleted_assets"
                    "(guid,path,ext,deleted_at) VALUES (?,?,?,?)",
                    (old_asset["guid"], old_asset["path"], old_asset["ext"],
                     int(time.time())))
            out["deleted"].append(rel)
            continue
        guid = _guid_for_path(root, rel)
        if guid:
            cur.execute("DELETE FROM deleted_assets WHERE guid=?", (guid,))
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
                _insert_visual_structure(cur, text, rel, guid)
            elif ext in JSON_ASSET_EXTS:
                _insert_visual_structure(cur, text, rel, guid)
            out["updated"].append(rel)
        except Exception as e:
            out["errors"].append({"file": rel, "error": str(e)})

    _propagate_mono(cur)
    out["confidence"] = _resolve_calls(cur)
    cur.execute("INSERT OR REPLACE INTO meta_kv(key,value) VALUES ('version',?)",
                (__version__,))
    cur.execute(
        "INSERT OR REPLACE INTO meta_kv(key,value) VALUES ('updated_at',?)",
        (str(int(time.time())),))
    conn.commit()
    conn.close()
    out["seconds"] = round(time.time() - t0, 2)
    return out


def has_graph(project_root: str) -> bool:
    return os.path.exists(db_path(os.path.abspath(project_root)))
