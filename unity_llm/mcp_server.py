"""MCP(Model Context Protocol)stdio server,零依赖实现。

MCP stdio 传输 = 换行分隔的 JSON-RPC 2.0。这里只实现 LLM 客户端需要的最小集:
  initialize / notifications/initialized / ping / tools/list / tools/call

兼容性:Claude Code、Claude Desktop、Cursor、Trae、Cherry Studio、CodeBuddy、
Cline 等所有支持 MCP stdio 的客户端。

输出默认是紧凑 JSON(无缩进):同样的信息量能省 20~30% token,
模型读 JSON 不需要缩进对齐。
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict

from . import __version__
from . import graph, queries, context as context_mod

PROTOCOL_VERSION = "2024-11-05"

_TARGET_DESC = "类名(Enemy)/方法名(TakeDamage 或 Enemy.TakeDamage)/资产路径/guid"
_LOW = {"type": "boolean", "default": False,
        "description": "是否包含 low 置信度(接收者类型未知且方法名重名)的调用边,"
                       "默认 false —— 打开通常会多出成百上千条名字碰撞噪声"}

TOOLS = [
    {
        "name": "unity_stats",
        "description": ("图谱统计:资产/类型/成员/调用边/序列化引用/UnityEvent 数量,"
                        "以及调用边置信度分布和 guid 来源。"),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "unity_impact",
        "description": ("影响面分析(改代码前先调它):修改某个脚本/方法/prefab 会炸到谁。"
                        "三通道 —— C# 调用与继承(含 method_ref:Register(OnFoo) / "
                        "evt += OnFoo 这种没有括号的回调注册;带接收者类型解析和置信度,"
                        "并区分 scope: external 外部调用方 / internal 内部自调用)、"
                        "prefab/scene/ScriptableObject 序列化引用(传递闭包)、"
                        "UnityEvent/按钮 onClick 绑定。后两者是普通代码图谱"
                        "(tree-sitter/AST)结构上看不见的 Unity 特有耦合。"
                        "目标名字没精确命中时会返回 hint 字段,务必读它 —— "
                        "模糊命中的 0 依赖不等于没人使用。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": _TARGET_DESC},
                "depth": {"type": "integer", "default": 3,
                          "description": "资产级传递深度,默认 3"},
                "include_low": _LOW,
                "limit": {"type": "integer", "default": 60,
                          "description": "每个分区最多返回条数"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "unity_refs",
        "description": ("引用查找:哪些 prefab/场景/ScriptableObject 的哪个字段引用了它"
                        "(含所在 GameObject),以及引用它的 C# 位置(file:line)。"
                        "目标写成 Owner.Method 时只返回这个方法的引用;"
                        "回调注册点以 kind=method_ref 出现。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": _TARGET_DESC},
                "include_low": _LOW,
                "limit": {"type": "integer", "default": 60},
            },
            "required": ["target"],
        },
    },
    {
        "name": "unity_components",
        "description": ("组件清单:给 prefab/scene 就列出它挂了哪些脚本;"
                        "给脚本/类就反过来列出它被哪些 prefab/scene 的哪个 GameObject 挂载。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": _TARGET_DESC},
                "limit": {"type": "integer", "default": 60},
            },
            "required": ["target"],
        },
    },
    {
        "name": "unity_find",
        "description": "按名字模糊搜索 C# 类型、成员和项目资产(第三方目录排在后面)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "搜索关键词"},
                "limit": {"type": "integer", "default": 30},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "unity_dead_code",
        "description": ("Unity 感知死代码:排除生命周期回调、消息方法、字符串调用"
                        "(SendMessage/Invoke/StartCoroutine)、委托与方法组引用、"
                        "**UnityEvent/Inspector 绑定的方法**、序列化字段、第三方目录后"
                        "的 private/internal 无调用者方法。明细按目录聚合在 "
                        "by_directory,噪声目录用 exclude 或项目根 .unity-llm.json "
                        "的 dead_code_exclude 过滤。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_external": {"type": "boolean", "default": False,
                                     "description": "是否包含 3rd/Plugins/Packages"},
                "exclude": {"type": "array", "items": {"type": "string"},
                            "description": "额外排除的路径片段或前缀,如 "
                                           "[\"ART_TEST\", \"Assets/Demo\"]"},
                "limit": {"type": "integer", "default": 60},
            },
            "required": [],
        },
    },
    {
        "name": "unity_validate",
        "description": ("体检:prefab/scene 里指向不存在资产的悬空 guid(按引用次数排序,"
                        "已排除引擎内置资源),以及 .meta guid 被改写工具处理过的告警。"),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 60}},
            "required": [],
        },
    },
    {
        "name": "unity_context",
        "description": ("token 预算内的上下文包(markdown):类型签名 + 生命周期标注 +"
                        "调用方 + 序列化引用方 + UnityEvent 绑定。"
                        "改代码前调它,别让模型通读整个文件。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": _TARGET_DESC},
                "budget": {"type": "integer", "default": 2000,
                           "description": "token 预算,默认 2000"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "unity_rebuild",
        "description": "重建依赖图谱(改了大量文件后使用;单文件小改用 unity_update 更快)。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "unity_update",
        "description": ("增量更新图谱:你(或用户)修改/新增/删除了个别文件后调用,"
                        "只重建这些文件的数据(秒级),不用全量 rebuild。"
                        "传项目相对路径,如 Assets/Scripts/Enemy.cs。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "项目相对路径列表"},
            },
            "required": ["paths"],
        },
    },
]


def _ok_text(payload: Any, compact: bool = True) -> Dict:
    if isinstance(payload, str):
        text = payload
    else:
        text = queries.to_json(payload, compact=compact)
    return {"content": [{"type": "text", "text": text}]}


def make_dispatcher(project_root: str) -> Callable[[str, Dict], Any]:
    def need_graph():
        if not graph.has_graph(project_root):
            return graph.build(project_root)
        return None

    def dispatch(name: str, args: Dict) -> Any:
        built = need_graph()  # 首次使用自动建图,省掉「先跑 build」这一步
        low = bool(args.get("include_low", False))
        limit = int(args.get("limit", queries.DEFAULT_LIMIT))
        if name == "unity_stats":
            out = queries.stats(project_root)
        elif name == "unity_impact":
            out = queries.impact(project_root, args["target"],
                                 depth=int(args.get("depth", 3)),
                                 include_low=low, limit=limit)
        elif name == "unity_refs":
            out = queries.find_refs(project_root, args["target"],
                                    include_low=low, limit=limit)
        elif name == "unity_components":
            out = queries.components(project_root, args["target"], limit=limit)
        elif name == "unity_find":
            out = queries.find_symbols(project_root, args["pattern"],
                                       limit=int(args.get("limit", 30)))
        elif name == "unity_dead_code":
            out = queries.dead_code(
                project_root,
                include_external=bool(args.get("include_external", False)),
                limit=limit, exclude=args.get("exclude") or None)
        elif name == "unity_validate":
            out = queries.validate(project_root, limit=limit)
        elif name == "unity_context":
            out = context_mod.build_context(
                project_root, args["target"],
                budget_tokens=int(args.get("budget", 2000)))
        elif name == "unity_rebuild":
            out = {"ok": True, "stats": graph.build(project_root)}
        elif name == "unity_update":
            out = graph.update_files(project_root, list(args["paths"]))
        else:
            raise ValueError(f"未知工具: {name}")
        if built and isinstance(out, dict):
            out["auto_built"] = built
        return out
    return dispatch


def _send(msg: Dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve(project_root: str) -> None:
    # Windows 控制台默认 GBK;MCP 要求 UTF-8
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    dispatch = make_dispatcher(project_root)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method", "")
        rid = req.get("id")

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "unity-llm-graph", "version": __version__},
            }})
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = req.get("params", {})
            try:
                result = dispatch(params.get("name", ""),
                                  params.get("arguments", {}) or {})
                _send({"jsonrpc": "2.0", "id": rid, "result": _ok_text(result)})
            except Exception as e:  # 工具错误按 MCP 规范走 isError
                _send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"错误: {e}"}],
                    "isError": True,
                }})
        elif method.startswith("notifications/"):
            continue  # initialized 等通知无需回复
        elif rid is not None:
            _send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": f"Method not found: {method}"}})
