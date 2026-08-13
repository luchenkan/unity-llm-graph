"""扫描 .meta 文件,建立 guid -> 资源路径 的映射。

Unity 里每个资产(脚本/prefab/scene/贴图/音频...)都有一个同名 .meta 文件,
里面存着全局唯一的 guid。序列化引用(prefab YAML 里的 `guid: xxx`)全都指向
这个 guid,所以这张表是拼合两张图的基石。

三个实战踩过的坑:

  1. **包资产**:prefab 里大量引用 TextMeshPro / UGUI 的脚本,它们的 .meta 在
     `Packages/` 和 `Library/PackageCache/` 下。Library 默认不建图,但它的
     .meta 必须进 guid 表,否则这些引用全部「悬空」,看起来像项目坏了。
  2. **被重写过的 meta**:有些项目(资产保护 / 打包流水线)会把 .meta 里的
     guid 换成 Base64 长串,而 prefab YAML 里存的仍是 Unity 真正在用的 32 位
     十六进制 guid。此时 .meta 完全不可信 —— 必须用 Unity 侧导出的映射表
     `.unity-llm/guidmap.tsv`(见 tools/UnityLlmGuidDump.cs),否则这些资产
     一条入边都拿不到。
  3. **内置资源**:`0000000000000000e000000000000000` 之类是引擎内置资源,
     永远解析不到路径,不是错误。
"""
from __future__ import annotations

import os
import re
from typing import Callable, Dict, Iterator, Optional, Tuple

# Unity 原生是 32 位十六进制;部分项目(加密/重写工具)是 40~64 位 Base64 风格长串
_GUID_BODY = r"(?:[0-9a-fA-F]{32}|[A-Za-z0-9+/]{40,64}={0,2})"
GUID_RE = re.compile(r"^guid:\s*(" + _GUID_BODY + r")\s*$", re.MULTILINE)
HEX_GUID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

# 这些目录下的内容不参与建图(库缓存、IDE 产物、版本控制)
DEFAULT_EXCLUDE_DIRS = {
    "Library", "Temp", "Obj", "obj", "Logs", "UserSettings", "MemoryCaptures",
    ".git", ".svn", ".idea", ".vs", "node_modules", "Build", "Builds",
    ".unity-llm",
}

# 只收 guid、不解析内容的目录(引擎包:引用得上,但不是「你的代码」)
PACKAGE_META_ROOTS = ("Packages", os.path.join("Library", "PackageCache"))

# 路径里出现这些片段 = 第三方/引擎代码,默认从「影响面」「死代码」里排除
EXTERNAL_SEGMENTS = {
    "3rd", "3rdParty", "ThirdParty", "Plugins", "Packages", "PackageCache",
    "Standard Assets", "Samples", "TextMesh Pro", "ExternalDependencyManager",
}

GUID_MAP_FILE = os.path.join(".unity-llm", "guidmap.tsv")

# 引擎识别:团结引擎(Tuanjie,Unity 中国版)和国际版 Unity 的 .meta guid 不一样。
# 国际版:32 位 hex,写死在 .meta 里,磁盘文本就是权威。
# 团结引擎:资产保护会把 .meta 的 guid 重写成 Base64 长串,而 prefab/scene 里
#          m_Script 存的仍是引擎内部那份 hex guid —— 两边对不上,只能靠
#          AssetDatabase 导出 guidmap.tsv 才能把挂载点接上。
PROJECT_VERSION_FILE = os.path.join("ProjectSettings", "ProjectVersion.txt")


def detect_engine(root: str) -> Dict[str, str]:
    """判定项目用的是团结引擎还是国际版 Unity。

    两个信号:`m_TuanjieEditorVersion` 字段,以及版本号里的 `t` 后缀
    (如 2022.3.62t4)。识别不出来就当国际版。
    """
    out = {"engine": "unity", "version": ""}
    path = os.path.join(root, PROJECT_VERSION_FILE)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return out
    m = re.search(r"^m_EditorVersion:\s*(\S+)", text, re.MULTILINE)
    if m:
        out["version"] = m.group(1)
    tj = re.search(r"^m_TuanjieEditorVersion:\s*(\S+)", text, re.MULTILINE)
    if tj or re.match(r"^\d+\.\d+\.\d+t\d+", out["version"]):
        out["engine"] = "tuanjie"
        if tj:
            out["tuanjie_version"] = tj.group(1)
    return out

# 项目级配置(可选,JSON 而非 TOML:tomllib 要 Python 3.11,这里保持 3.9 兼容)
CONFIG_FILE = ".unity-llm.json"
_CONFIG_CACHE: Dict[str, Dict] = {}

# 常见的「项目内但不算产品代码」目录名:美术试验田、临时 Demo。
# 只是 init-config 生成模板时的建议值,不做默认排除(不同项目命名不同)。
SUGGESTED_EXCLUDES = ("ART_TEST", "Test", "Tests", "Demo", "Demos", "Sandbox",
                      "Example", "Examples", "Scratch")


def load_config(root: str) -> Dict:
    """读项目根的 .unity-llm.json(不存在就返回空配置)。

    支持字段:
      external_segments : 追加到「第三方目录」判定,影响 external 标记(需重新 build)
      dead_code_exclude : 死代码报告里过滤掉的路径片段(查询期生效,不用重建)
    """
    root = os.path.abspath(root)
    if root in _CONFIG_CACHE:
        return _CONFIG_CACHE[root]
    path = os.path.join(root, CONFIG_FILE)
    cfg: Dict = {}
    if os.path.exists(path):
        try:
            import json
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg = loaded
        except (OSError, ValueError):
            cfg = {}  # 配置坏了不该让整个工具挂掉
    _CONFIG_CACHE[root] = cfg
    return cfg


def external_segments(root: str = None) -> set:
    segs = set(EXTERNAL_SEGMENTS)
    if root:
        extra = load_config(root).get("external_segments") or []
        segs.update(s for s in extra if isinstance(s, str))
    return segs


def is_external(rel_path: str, root: str = None) -> bool:
    return any(seg in external_segments(root) for seg in rel_path.split("/"))


def path_matches(rel_path: str, patterns) -> bool:
    """路径是否命中排除模式:整段相等,或作为路径前缀。"""
    if not patterns:
        return False
    segs = rel_path.split("/")
    for p in patterns:
        p = p.replace("\\", "/").strip("/")
        if not p:
            continue
        if "/" in p:
            if rel_path == p or rel_path.startswith(p + "/"):
                return True
        elif p in segs:
            return True
    return False


def iter_files(root: str, exclude_dirs=None) -> Iterator[str]:
    """遍历项目,产出所有文件的绝对路径(跳过排除目录)。"""
    excludes = set(DEFAULT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in excludes]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def relpath(root: str, path: str) -> str:
    """统一成 POSIX 风格的相对路径,保证跨平台输出一致。"""
    return os.path.relpath(path, root).replace(os.sep, "/")


def _guid_of_meta(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)  # guid 一定在文件头部,读前 4KB 足够
    except OSError:
        return None
    m = GUID_RE.search(head)
    return m.group(1) if m else None


def load_guid_override(root: str) -> Dict[str, str]:
    """读 Unity 侧导出的权威映射表(guid<TAB>路径,每行一条)。"""
    path = os.path.join(root, GUID_MAP_FILE)
    out: Dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0]:
                out[parts[0].strip()] = parts[1].strip().replace("\\", "/")
    return out


def build_guid_map(root: str, exclude_dirs=None,
                   on_tick: Optional[Callable[[int], None]] = None
                   ) -> Tuple[Dict[str, str], str]:
    """返回 ({guid: 相对资产路径}, 来源说明)。

    来源优先级:`.unity-llm/guidmap.tsv`(Unity 导出,权威) > .meta 扫描。
    两者会合并 —— 导出表覆盖 meta,meta 补齐导出表没有的(如包内资产)。
    on_tick(n) 每处理一个 .meta 回调一次,给全量 build 打进度。
    """
    guid_map: Dict[str, str] = {}
    n = 0
    for path in iter_files(root, exclude_dirs):
        if path.endswith(".meta"):
            g = _guid_of_meta(path)
            if g:
                guid_map[g] = relpath(root, path[:-5])  # 去掉 .meta
            n += 1
            if on_tick:
                on_tick(n)
    # 引擎包:只收 guid,保证 prefab 里对 TMP/UGUI 的引用能落地
    for sub in PACKAGE_META_ROOTS:
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            for fn in filenames:
                if not fn.endswith(".meta"):
                    continue
                p = os.path.join(dirpath, fn)
                g = _guid_of_meta(p)
                if g:
                    guid_map.setdefault(g, relpath(root, p[:-5]))
                n += 1
                if on_tick:
                    on_tick(n)

    override = load_guid_override(root)
    if override:
        # 导出表是权威的:先把被 override 的路径从旧 guid 上摘掉
        overridden = set(override.values())
        guid_map = {g: p for g, p in guid_map.items() if p not in overridden}
        guid_map.update(override)
        source = f"guidmap.tsv({len(override)}) + meta"
    else:
        source = "meta"
    return guid_map, source


def guid_format_report(guid_map: Dict[str, str]) -> Dict[str, int]:
    """统计 guid 格式:非 32 位十六进制的 meta 说明被工具重写过。"""
    hexed = sum(1 for g in guid_map if HEX_GUID_RE.match(g))
    return {"total": len(guid_map), "hex": hexed,
            "nonstandard": len(guid_map) - hexed}


def iter_assets(root: str, exclude_dirs=None) -> Iterator[Tuple[str, str]]:
    """产出 (绝对路径, 相对路径),跳过 .meta 文件本身。"""
    for path in iter_files(root, exclude_dirs):
        if path.endswith(".meta"):
            continue
        yield path, relpath(root, path)
