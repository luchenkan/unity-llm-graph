"""视觉资产结构解析:AnimatorController(.controller)、Timeline(.playable)、ShaderGraph(.shadergraph)。

unity_yaml.py 只抽「跨资产 guid 边」;这些资产内部的语义结构(状态机、轨道、clip 时序)
是靠文件内 fileID 互相引用的,parser 会把无 guid 的内部引用 continue 掉,所以这里做第二遍解析。

注意:fileID 一律按字符串处理 —— 团结引擎/国际版都可能是负数(如 &-2764362647030779139),
而且它只用来做对象定位,不做数值排序。
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple

from .unity_yaml import DOC_HEADER_RE

_FID_RE = re.compile(r"\{fileID:\s*(-?\d+)")
_GUID_RE = re.compile(r"guid:\s*([0-9a-fA-F]{32})")
_SCRIPT_GUID_RE = re.compile(r"m_Script:\s*\{fileID:\s*-?\d+,\s*guid:\s*([0-9a-fA-F]{32})")
# 内容引用:fileID != 11500000(11500000 是 m_Script 专用)的跨资产 guid
_CONTENT_GUID_RE = re.compile(r"\{fileID:\s*(?!11500000\b)-?\d+,\s*guid:\s*([0-9a-fA-F]{32})")

# AnimatorController 相关 classID
CID_CONTROLLER = 91
CID_STATE_MACHINE = 1107
CID_STATE = 1102
CID_TRANSITION = 1101


def _split_docs(text: str) -> List[Tuple[int, str, str]]:
    """切成 (class_id, file_id_str, body) 列表。"""
    docs = []
    headers = list(DOC_HEADER_RE.finditer(text))
    for i, h in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        docs.append((int(h.group(1)), str(int(h.group(2))), text[h.end():end]))
    return docs


def _unquote(raw: str) -> str:
    """Unity 对非 ASCII 名字写成 `"\\u9AA8\\u67B6|Idle"`,不解码就是一串乱码给 LLM。"""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw.strip('"')
    return raw


def _field_str(body: str, key: str) -> str:
    m = re.search(rf"^\s*{re.escape(key)}:\s*(.*?)\s*$", body, re.MULTILINE)
    return _unquote(m.group(1)) if m else ""


def _first_guid(body: str) -> str:
    m = _GUID_RE.search(body)
    return m.group(1) if m else ""


def _list_fileids(body: str, key: str) -> List[str]:
    """取 `key:` 之后、缩进回退之前那段里的所有 `{fileID: X}`。

    Unity 序列化里列表项是 `  - {fileID: 123}` 内联形式(Animator 的 m_Transitions、
    Timeline 的 m_Clips/m_Tracks 都是)。key 本身带缩进(文档体内一般 2 空格),
    缩进回退判据:下一个「0~2 空格起头的 key」。
    """
    m = re.search(rf"^[ \t]*{re.escape(key)}:[ \t]*$", body, re.MULTILINE)
    if not m:
        return []
    seg = body[m.end():]
    cut = len(seg)
    for mm in re.finditer(r"^\s{0,2}[A-Za-z_]\w*:", seg, re.MULTILINE):
        cut = mm.start()
        break
    return _FID_RE.findall(seg[:cut])


def _to_float(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _layers(controller_body: str) -> List[Dict]:
    """AnimatorController 的 m_AnimatorLayers:每层 (名字, 根 state machine fileID)。

    列表项形如 `- serializedVersion: 5 / m_Name: Base Layer / m_StateMachine: {fileID: N}`,
    按出现顺序扫:遇到 m_Name 记住,遇到 m_StateMachine 就配成一层。
    """
    m = re.search(r"^  m_AnimatorLayers:\s*$", controller_body, re.MULTILINE)
    if not m:
        return []
    seg = controller_body[m.end():]
    nxt = re.search(r"^  [A-Za-z_]\w*:", seg, re.MULTILINE)
    if nxt:
        seg = seg[:nxt.start()]
    out: List[Dict] = []
    pending = ""
    for mm in re.finditer(r"m_Name:\s*(.*)$|m_StateMachine:\s*\{fileID:\s*(-?\d+)",
                          seg, re.MULTILINE):
        if mm.group(1) is not None:
            pending = _unquote(mm.group(1))
        else:
            out.append({"name": pending, "sm_fileid": str(int(mm.group(2)))})
            pending = ""
    return out


def _walk_state_machine(by_id: Dict[str, Tuple[int, str]], sm_fid: str,
                        layer: str, seen: set) -> Tuple[Dict[str, str], set]:
    """从一个 state machine 出发,返回 (state fileID → 层路径, 默认 state fileID 集合)。

    子状态机(m_ChildStateMachines)递归进去,层路径用 `层名/子机名` 表示。
    """
    layer_of: Dict[str, str] = {}
    defaults: set = set()
    if sm_fid in seen or sm_fid not in by_id:
        return layer_of, defaults
    seen.add(sm_fid)
    body = by_id[sm_fid][1]
    for sfid in _list_fileids(body, "m_ChildStates"):
        layer_of.setdefault(sfid, layer)
    dm = re.search(r"m_DefaultState:\s*\{fileID:\s*(-?\d+)", body)
    if dm and int(dm.group(1)) != 0:
        defaults.add(str(int(dm.group(1))))
    for child in _list_fileids(body, "m_ChildStateMachines"):
        if child not in by_id:
            continue
        sub = f"{layer}/{_field_str(by_id[child][1], 'm_Name')}" if layer else \
            _field_str(by_id[child][1], "m_Name")
        cl, cd = _walk_state_machine(by_id, child, sub, seen)
        for k, v in cl.items():
            layer_of.setdefault(k, v)
        defaults |= cd
    return layer_of, defaults


def parse_animator(text: str) -> Dict:
    """解析 AnimatorController 内部结构。

    返回:
      {
        "name": "controller 名",
        "layers": [{"name", "sm_fileid"}],
        "states": [{"fileid", "name", "motion_guid", "layer", "is_default"}],
        "transitions": [{"from", "to", "layer", "conditions": [{"event","mode","threshold"}]}],
      }
    """
    docs = _split_docs(text)
    by_id = {fid: (cid, body) for cid, fid, body in docs}

    name = ""
    layers: List[Dict] = []
    for fid, (cid, body) in by_id.items():
        if cid == CID_CONTROLLER:
            name = _field_str(body, "m_Name")
            layers = _layers(body)
            break

    # state fileID → 层路径 / 默认状态
    layer_of: Dict[str, str] = {}
    defaults: set = set()
    seen: set = set()
    for lyr in layers:
        lo, df = _walk_state_machine(by_id, lyr["sm_fileid"], lyr["name"], seen)
        for k, v in lo.items():
            layer_of.setdefault(k, v)
        defaults |= df
    # 兜底:layers 解析不到(旧格式/子机孤儿)时,剩下的 state machine 也走一遍
    for fid, (cid, body) in by_id.items():
        if cid == CID_STATE_MACHINE and fid not in seen:
            lo, df = _walk_state_machine(by_id, fid, _field_str(body, "m_Name"), seen)
            for k, v in lo.items():
                layer_of.setdefault(k, v)
            defaults |= df

    states: List[Dict] = []
    for fid, (cid, body) in by_id.items():
        if cid != CID_STATE:
            continue
        motion_guid = ""
        mm = re.search(r"m_Motion:\s*\{[^}]*\}", body)
        if mm:
            motion_guid = _first_guid(mm.group(0))
        states.append({"fileid": fid, "name": _field_str(body, "m_Name"),
                       "motion_guid": motion_guid,
                       "layer": layer_of.get(fid, ""),
                       "is_default": fid in defaults})

    # 转移元数据:目标状态 + 条件
    trans_meta: Dict[str, Dict] = {}
    for fid, (cid, body) in by_id.items():
        if cid != CID_TRANSITION:
            continue
        dst = ""
        dm = re.search(r"m_DstState:\s*\{fileID:\s*(-?\d+)", body)
        if dm:
            dst = str(int(dm.group(1)))
        trans_meta[fid] = {"to": dst, "conditions": _conditions(body)}

    # from:每个 state 的 m_Transitions 列表指向哪些 transition
    transitions: List[Dict] = []
    for st in states:
        st_body = by_id[st["fileid"]][1]
        for tfid in _list_fileids(st_body, "m_Transitions"):
            if tfid in trans_meta:
                transitions.append({"from": st["fileid"],
                                    "to": trans_meta[tfid]["to"],
                                    "layer": st["layer"],
                                    "conditions": trans_meta[tfid]["conditions"]})

    # AnyState 转移挂在 state machine 上,没有源 state —— from 记 "AnyState"
    for fid, (cid, body) in by_id.items():
        if cid != CID_STATE_MACHINE:
            continue
        sm_layer = next((l["name"] for l in layers if l["sm_fileid"] == fid),
                        _field_str(body, "m_Name"))
        for tfid in _list_fileids(body, "m_AnyStateTransitions"):
            if tfid in trans_meta:
                transitions.append({"from": "AnyState",
                                    "to": trans_meta[tfid]["to"],
                                    "layer": sm_layer,
                                    "conditions": trans_meta[tfid]["conditions"]})

    return {"name": name, "layers": layers, "states": states,
            "transitions": transitions}


def _conditions(body: str) -> List[Dict]:
    """提取 m_Conditions 列表(参数名 + 比较模式 + 阈值)。"""
    m = re.search(r"m_Conditions:\s*\n(.*?)(?=^\s{0,2}[A-Za-z_]\w*:|\Z)",
                  body, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    out = []
    for cm in re.finditer(
            r"- m_ConditionMode:\s*(\d+)\s*\n\s*m_ConditionEvent:\s*(\S+)\s*\n\s*m_EventTreshold:\s*(-?\d+\.?\d*)",
            m.group(1)):
        out.append({"event": cm.group(2), "mode": cm.group(1),
                    "threshold": cm.group(3)})
    return out


def _block_after(body: str, key: str) -> str:
    """取 `  key:` 之后、到下一个 2 空格 key 为止的文本(Timeline 的 m_Clips 块)。"""
    m = re.search(rf"^  {re.escape(key)}:\s*\n", body, re.MULTILINE)
    if not m:
        return ""
    seg = body[m.end():]
    for mm in re.finditer(r"^  [A-Za-z_]\w*:", seg, re.MULTILINE):
        return seg[:mm.start()]
    return seg


def _parse_clips(block: str, by_id: Dict[str, Tuple[int, str]], track_fid: str) -> List[Dict]:
    """解析 m_Clips 块:每个 clip 以 `  - m_Version:` 开头,是内联对象(非 fileID 引用)。"""
    clips: List[Dict] = []
    for idx, item in enumerate(block.split("  - m_Version:")[1:], start=1):
        if "m_Start:" not in item:
            continue
        asset_guid, asset_kind = "", ""
        am = re.search(r"m_Asset:\s*\{fileID:\s*(-?\d+)", item)
        if am:
            asset_fid = str(int(am.group(1)))
            if asset_fid in by_id:
                asset_body = by_id[asset_fid][1]
                cm = _CONTENT_GUID_RE.search(asset_body)
                asset_guid = cm.group(1) if cm else ""
                # 大多数 clip 的内容是文件内联的(录制动画),没有外部 guid;
                # 这时 playable asset 的 m_Script guid 是唯一能说清「这 clip 播什么」的东西
                sm = _SCRIPT_GUID_RE.search(asset_body)
                asset_kind = sm.group(1) if sm else ""
        clips.append({
            "fileid": f"{track_fid}:{idx}",
            "start": _to_float(_field_str(item, "m_Start")),
            "duration": _to_float(_field_str(item, "m_Duration")),
            "display_name": _field_str(item, "m_DisplayName"),
            "asset_guid": asset_guid,
            "asset_kind": asset_kind,
        })
    return clips


def parse_timeline(text: str) -> Dict:
    """解析 Timeline(.playable)内部结构。

    返回:
      {
        "tracks": [{"fileid", "display_name", "script_guid", "parent_fileid", "clips":
                    [{"fileid", "start", "duration", "display_name",
                      "asset_guid", "asset_kind"}]}],
      }
    clip 的 asset_guid 是「clip 的 playable asset 引用的外部资产 guid」(AnimationClip /
    音频 / ControlPlayableAsset 的 source),为空表示内联(录制动画)或无外部引用;
    这种情况看 asset_kind(playable asset 的脚本 guid)判断 clip 类型。
    parent_fileid 来自 GroupTrack 的 m_Children,空串表示顶层轨道。
    """
    docs = _split_docs(text)
    by_id = {fid: (cid, body) for cid, fid, body in docs}

    # 轨道父子关系:任何 114 的 m_Children 列表都算(GroupTrack / 嵌套 track)
    parent_of: Dict[str, str] = {}
    for fid, (cid, body) in by_id.items():
        if cid != 114:
            continue
        for child in _list_fileids(body, "m_Children"):
            parent_of[child] = fid

    tracks: List[Dict] = []
    # track 是带 m_Clips 或 m_Animations 的 MonoBehaviour(114)
    for fid, (cid, body) in by_id.items():
        if cid != 114 or ("m_Clips:" not in body and "m_Animations:" not in body):
            continue
        sm = _SCRIPT_GUID_RE.search(body)
        nm = re.search(r"^  m_Name:\s*(.*?)\s*$", body, re.MULTILINE)
        tracks.append({
            "fileid": fid,
            "display_name": _unquote(nm.group(1)) if nm else "",
            "script_guid": sm.group(1) if sm else "",
            "parent_fileid": parent_of.get(fid, ""),
            "clips": _parse_clips(_block_after(body, "m_Clips"), by_id, fid),
        })
    return {"tracks": tracks}


def parse_shadergraph(text: str) -> Dict:
    """解析 ShaderGraph(.shadergraph,JSON)。只抽 subgraph 引用和属性名。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"subgraphs": [], "properties": []}
    subs = []
    for sg in data.get("m_SubGraphs", []) or []:
        if isinstance(sg, dict):
            subs.append({"name": sg.get("m_Name") or sg.get("displayName") or "",
                         "guid": sg.get("guid", "")})
    props = [p.get("m_Name") or p.get("referenceName") or ""
             for p in (data.get("m_Properties", []) or []) if isinstance(p, dict)]
    return {"subgraphs": [s for s in subs if s["guid"] or s["name"]],
            "properties": [p for p in props if p]}


def parse_vfx(text: str) -> Dict:
    """解析 VFX Graph(.vfx,JSON)。VFX 结构复杂,先只抽引用的外部资产 guid。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"refs": []}
    guids = set()

    def walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif isinstance(obj, str):
            for g in re.findall(r"\b[0-9a-fA-F]{32}\b", obj):
                guids.add(g)

    walk(data)
    return {"refs": [{"guid": g} for g in guids]}
