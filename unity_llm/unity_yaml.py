"""Unity YAML(prefab / scene / asset)解析器。

不依赖 PyYAML —— Unity 的 YAML 带自定义 tag(!u!114)且是多文档流,PyYAML
需要逐 tag 注册才能吃。我们这里只做两件确定性的事:

  1. 按 `--- !u!<classID> &<fileID>` 切分文档,记录 classID / fileID;
  2. 逐行提取 `{fileID: x, guid: y, type: z}` 形式的引用和关键字段(m_Name 等);
  3. 提取 UnityEvent 持久化绑定(`m_PersistentCalls.m_Calls` 里的
     m_MethodName + m_TargetAssemblyTypeName + m_Target)。

序列化引用的「字段名」通过引用所在行或其上方最近的 `key:` 行推断,
足够用于"这个 prefab 的哪个字段引用了这个脚本/资源"的报告。

UnityEvent 绑定是 Unity 项目里 dead-code 误报的最大单一来源:按钮 onClick
在 scene/prefab 的 YAML 里指名调用某个 public 方法,代码里没有任何调用点。
把它抽出来,dead code 才从"永远需人工复核"变成可用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

DOC_HEADER_RE = re.compile(r"^--- !u!(\d+) &(-?\d+)( stripped)?\s*$", re.MULTILINE)

# {fileID: 11500000, guid: xxx, type: 3} / {fileID: 1234}
# guid 兼容两种格式:32 位十六进制(Unity 原生) / Base64 风格长串(加密项目)
_GUID_BODY = r"(?:[0-9a-fA-F]{32}|[A-Za-z0-9+/]{40,64}={0,2})"
REF_RE = re.compile(
    r"\{fileID:\s*(-?\d+)(?:,\s*guid:\s*(" + _GUID_BODY + r"))?(?:,\s*type:\s*\d+)?\}"
)

NAME_RE = re.compile(r"^\s*m_Name:[ \t]*(.*)$", re.MULTILINE)
GAMEOBJECT_RE = re.compile(
    r"^\s*m_GameObject:\s*\{fileID:\s*(-?\d+)", re.MULTILINE)
SCRIPT_RE = re.compile(
    r"^\s*m_Script:\s*\{fileID:\s*-?\d+,\s*guid:\s*(" + _GUID_BODY + r")",
    re.MULTILINE
)
# Transform 的父节点(fileID=0 表示根)。这是还原 GameObject 层级树的关键:
# Transform 与 GameObject 一一对应,父子关系写在 Transform 的 m_Father 上,
# 而不是 GameObject 上 —— 纯文本 grep 看不到这层耦合。
FATHER_RE = re.compile(r"^\s*m_Father:\s*\{fileID:\s*(-?\d+)", re.MULTILINE)
KEY_RE = re.compile(r"^\s{2,}([A-Za-z_]\w*):", re.MULTILINE)

# UnityEvent 持久化绑定
METHOD_NAME_RE = re.compile(r"^\s*m_MethodName:[ \t]*(\S.*?)\s*$")
TARGET_TYPE_RE = re.compile(r"^\s*m_TargetAssemblyTypeName:[ \t]*(\S.*?)\s*$")
EVENT_TARGET_RE = re.compile(
    r"^\s*m_Target:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*(" + _GUID_BODY + r"))?")
# 这些 key 是 UnityEvent 的内部结构,不是"事件字段名"
_EVENT_INTERNAL_KEYS = {"m_PersistentCalls", "m_Calls", "m_Arguments",
                        "m_Target", "m_Mode", "m_CallState", "m_Delegates"}

# 常见 classID 对照(报告时友好显示)
CLASS_NAMES = {
    1: "GameObject", 4: "Transform", 20: "Camera", 23: "MeshRenderer",
    33: "MeshFilter", 54: "Rigidbody", 64: "MeshCollider", 65: "BoxCollider",
    81: "AudioListener", 82: "AudioSource", 95: "Animator", 96: "TrailRenderer",
    104: "RenderSettings", 108: "Light", 111: "Animation", 114: "MonoBehaviour",
    120: "LineRenderer", 135: "SphereCollider", 136: "CapsuleCollider",
    137: "SkinnedMeshRenderer", 143: "CharacterController", 157: "LightmapSettings",
    198: "ParticleSystem", 199: "ParticleSystemRenderer", 205: "LODGroup",
    212: "SpriteRenderer", 213: "Sprite", 215: "ReflectionProbe",
    220: "LightProbeGroup", 222: "CanvasRenderer", 223: "Canvas",
    224: "RectTransform", 225: "CanvasGroup", 320: "PlayableDirector",
    1001: "PrefabInstance", 1660057539: "SceneRoots",
}


@dataclass
class YamlRef:
    """一条从本文档指向外部资产的序列化引用。"""
    field: str           # 推断出的字段名,如 m_Script / enemyPrefab / m_Material
    file_id: int         # 目标文件内的 fileID
    guid: Optional[str]  # 目标资产 guid(内置资源可能为 None)


@dataclass
class YamlEvent:
    """一条 UnityEvent 持久化绑定(按钮 onClick 之类)。"""
    field: str                    # 事件字段名,如 m_OnClick / onValueChanged
    method: str                   # 被绑定的方法名
    target_type: str              # m_TargetAssemblyTypeName 的类型全名(去掉程序集)
    target_fileid: int            # 目标组件的 fileID
    target_guid: Optional[str]    # 跨资产绑定时的 guid


@dataclass
class YamlDoc:
    class_id: int
    file_id: int
    stripped: bool
    name: str = ""
    script_guid: Optional[str] = None      # 仅 MonoBehaviour: 挂载的脚本 guid
    go_fileid: Optional[int] = None        # m_GameObject fileID,用来还原物体名
    father_fileid: Optional[int] = None    # 仅 Transform: 父 Transform 的 fileID(0=根)
    refs: List[YamlRef] = field(default_factory=list)
    events: List[YamlEvent] = field(default_factory=list)

    @property
    def class_name(self) -> str:
        return CLASS_NAMES.get(self.class_id, f"Class{self.class_id}")


@dataclass
class UnityYamlFile:
    docs: List[YamlDoc] = field(default_factory=list)

    def behaviours(self) -> List[YamlDoc]:
        return [d for d in self.docs if d.class_id == 114 and not d.stripped]

    def game_object_name(self, file_id: int) -> str:
        for d in self.docs:
            if d.file_id == file_id and d.class_id == 1:
                return d.name
        return ""


def _infer_field(body: str, ref_start: int) -> str:
    """从引用出现的位置向上找最近的 `key:` 作为字段名。"""
    line_start = body.rfind("\n", 0, ref_start) + 1
    m = KEY_RE.match(body, line_start)
    if m:
        return m.group(1)
    # 引用在值的下一行(多行写法),向上扫
    pos = line_start - 1
    while pos > 0:
        prev_start = body.rfind("\n", 0, pos) + 1
        m = KEY_RE.match(body, prev_start)
        if m:
            return m.group(1)
        if prev_start == pos:
            break
        pos = prev_start - 1
    return ""


def _extract_events(body: str) -> List[YamlEvent]:
    """抽取 UnityEvent 绑定。

    典型形状(m_Target / 类型名 / 方法名的顺序在不同 Unity 版本里会变,
    所以在方法名附近的一个小窗口里双向查找):

        m_OnClick:
          m_PersistentCalls:
            m_Calls:
            - m_Target: {fileID: 1234}
              m_TargetAssemblyTypeName: Game.UI.Shop, Assembly-CSharp
              m_MethodName: OnBuyClicked
    """
    lines = body.split("\n")
    out: List[YamlEvent] = []
    for i, line in enumerate(lines):
        mm = METHOD_NAME_RE.match(line)
        if not mm:
            continue
        method = mm.group(1).strip().strip('"')
        if not method or method in ("", "''", '""'):
            continue
        target_type, fid, guid = "", 0, None
        for j in list(range(i - 1, max(-1, i - 9), -1)) + \
                 list(range(i + 1, min(len(lines), i + 5))):
            l = lines[j]
            if not target_type:
                tm = TARGET_TYPE_RE.match(l)
                if tm:
                    target_type = tm.group(1).split(",")[0].strip()
            if not fid:
                em = EVENT_TARGET_RE.match(l)
                if em:
                    fid = int(em.group(1))
                    guid = em.group(2)
        fname = ""
        for j in range(i - 1, max(-1, i - 40), -1):
            km = re.match(r"^\s*([A-Za-z_]\w*):\s*$", lines[j])
            if km and km.group(1) not in _EVENT_INTERNAL_KEYS:
                fname = km.group(1)
                break
        out.append(YamlEvent(field=fname, method=method, target_type=target_type,
                             target_fileid=fid, target_guid=guid))
    return out


def parse_unity_yaml(text: str) -> UnityYamlFile:
    """解析一份 prefab / scene / .asset 的文本。"""
    result = UnityYamlFile()
    headers = list(DOC_HEADER_RE.finditer(text))
    for i, h in enumerate(headers):
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end]
        doc = YamlDoc(
            class_id=int(h.group(1)),
            file_id=int(h.group(2)),
            stripped=bool(h.group(3)),
        )
        nm = NAME_RE.search(body)
        if nm:
            doc.name = nm.group(1).strip().strip('"')
        gm = GAMEOBJECT_RE.search(body)
        if gm:
            fid = int(gm.group(1))
            if fid:
                doc.go_fileid = fid
        fm = FATHER_RE.search(body)
        if fm:
            doc.father_fileid = int(fm.group(1))
        sm = SCRIPT_RE.search(body)
        if sm:
            doc.script_guid = sm.group(1)
        seen = set()
        for rm in REF_RE.finditer(body):
            fid, guid = int(rm.group(1)), rm.group(2)
            if guid is None:
                continue  # 文件内部引用,不是跨资产边
            key = (guid, fid)
            if key in seen:
                continue
            seen.add(key)
            doc.refs.append(YamlRef(field=_infer_field(body, rm.start()),
                                    file_id=fid, guid=guid))
        if "m_MethodName:" in body:
            doc.events = _extract_events(body)
        result.docs.append(doc)
    return result
