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


def _field_str(body: str, key: str) -> str:
    m = re.search(rf"^\s*{re.escape(key)}:\s*(.*?)\s*$", body, re.MULTILINE)
    return m.group(1).strip('"') if m else ""


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


def parse_animator(text: str) -> Dict:
    """解析 AnimatorController 内部结构。

    返回:
      {
        "name": "controller 名",
        "states": [{"fileid", "name", "motion_guid"}],
        "transitions": [{"from", "to", "conditions": [{"event","mode","threshold"}]}],
      }
    """
    docs = _split_docs(text)
    by_id = {fid: (cid, body) for cid, fid, body in docs}

    states: List[Dict] = []
    for fid, (cid, body) in by_id.items():
        if cid != CID_STATE:
            continue
        motion_guid = ""
        mm = re.search(r"m_Motion:\s*\{[^}]*\}", body)
        if mm:
            motion_guid = _first_guid(mm.group(0))
        states.append({"fileid": fid, "name": _field_str(body, "m_Name"),
                       "motion_guid": motion_guid})

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
                                    "conditions": trans_meta[tfid]["conditions"]})

    # AnyState 转移挂在 state machine 上,没有源 state —— from 记 "AnyState"
    for fid, (cid, body) in by_id.items():
        if cid != CID_STATE_MACHINE:
            continue
        for tfid in _list_fileids(body, "m_AnyStateTransitions"):
            if tfid in trans_meta:
                transitions.append({"from": "AnyState",
                                    "to": trans_meta[tfid]["to"],
                                    "conditions": trans_meta[tfid]["conditions"]})

    name = ""
    for fid, (cid, body) in by_id.items():
        if cid == CID_CONTROLLER:
            name = _field_str(body, "m_Name")
            break

    return {"name": name, "states": states, "transitions": transitions}


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
        asset_guid = ""
        am = re.search(r"m_Asset:\s*\{fileID:\s*(-?\d+)", item)
        if am:
            asset_fid = str(int(am.group(1)))
            if asset_fid in by_id:
                cm = _CONTENT_GUID_RE.search(by_id[asset_fid][1])
                asset_guid = cm.group(1) if cm else ""
        clips.append({
            "fileid": f"{track_fid}:{idx}",
            "start": _to_float(_field_str(item, "m_Start")),
            "duration": _to_float(_field_str(item, "m_Duration")),
            "display_name": _field_str(item, "m_DisplayName"),
            "asset_guid": asset_guid,
        })
    return clips


def parse_timeline(text: str) -> Dict:
    """解析 Timeline(.playable)内部结构。

    返回:
      {
        "tracks": [{"fileid", "display_name", "script_guid", "clips":
                    [{"fileid", "start", "duration", "display_name", "asset_guid"}]}],
      }
    clip 的 asset_guid 是「clip 的 playable asset 引用的外部资产 guid」(AnimationClip /
    音频 / ControlPlayableAsset 的 source),为空表示内联或无外部引用。
    """
    docs = _split_docs(text)
    by_id = {fid: (cid, body) for cid, fid, body in docs}

    tracks: List[Dict] = []
    # track 是带 m_Clips 或 m_Animations 的 MonoBehaviour(114)
    for fid, (cid, body) in by_id.items():
        if cid != 114 or ("m_Clips:" not in body and "m_Animations:" not in body):
            continue
        sm = _SCRIPT_GUID_RE.search(body)
        nm = re.search(r"^  m_Name:\s*(.*?)\s*$", body, re.MULTILINE)
        tracks.append({
            "fileid": fid,
            "display_name": nm.group(1).strip('"') if nm else "",
            "script_guid": sm.group(1) if sm else "",
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
