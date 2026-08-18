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
import os
import sys
import time
from typing import Any, Callable, Dict

from . import __version__
from . import graph, queries, context as context_mod
from .tokens import estimate_tokens

PROTOCOL_VERSION = "2024-11-05"

_TARGET_DESC = "类名(Enemy)/方法名(TakeDamage 或 Enemy.TakeDamage)/资产路径/guid"
_LOW = {"type": "boolean", "default": False,
        "description": "是否包含 low 置信度(接收者类型未知且方法名重名)的调用边,"
                       "默认 false —— 打开通常会多出成百上千条名字碰撞噪声"}
_EXTERNAL = {"type": "boolean", "default": False,
             "description": "是否包含 3rd/Plugins/Packages 等第三方目录的结果"}

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
                        "短名即可,同名 partial 文件会自动合并。"
                        "三通道 —— C# 调用与继承(含 method_ref、以及 "
                        "Type.Field.Method() 链式静态字段/单例调用)、"
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
                "include_external": _EXTERNAL,
                "limit": {"type": "integer", "default": 8,
                          "description": "每个分区最多返回条数"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "unity_refs",
        "description": ("引用查找(改 prefab/scene/SO 前先调,grep 看不到):"
                        "哪些 prefab/场景/ScriptableObject 的哪个字段、哪个 GameObject "
                        "引用了它,以及引用它的 C# 位置(file:line)。"
                        "目标写成 Owner.Method 或 Owner.Field 时只返回该项;"
                        "回调注册点以 kind=method_ref 出现,"
                        "Type.Field.Method() 链式调用以 kind=field_call 出现。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": _TARGET_DESC},
                "include_low": _LOW,
                "include_external": _EXTERNAL,
                "limit": {"type": "integer", "default": 12},
            },
            "required": ["target"],
        },
    },
    {
        "name": "unity_components",
        "description": ("组件清单:给 prefab/scene 列出它挂了哪些脚本、挂在哪个 GameObject,"
                        "并输出 GameObject 层级树(hierarchy,含父子关系与每个节点挂的组件)。"
                        "给脚本/类反过来列出被哪些 prefab/scene 的哪个 GameObject 挂载。"
                        "这是 grep 做不到的,动 prefab/scene 前优先用它,"
                        "不要用 Editor MCP 的 execute_code/FindGameObjects 去列层级。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": _TARGET_DESC},
                "include_external": _EXTERNAL,
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["target"],
        },
    },
    {
        "name": "unity_find",
        "description": ("按名字搜类型/成员/资产,返回精简索引(名+路径),不是全文。"
                        "用来确认短名后再跟 unity_impact / unity_refs;"
                        "不要拿它当 grep,大关键词会浪费 token。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "搜索关键词"},
                "kind": {"type": "string",
                         "description": "可选 type / member / asset,默认三类都搜"},
                "limit": {"type": "integer", "default": 5,
                          "description": "每类最多条数,默认 5"},
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
                "include_external": _EXTERNAL,
                "exclude": {"type": "array", "items": {"type": "string"},
                            "description": "额外排除的路径片段或前缀,如 "
                                           "[\"ART_TEST\", \"Assets/Demo\"]"},
                "limit": {"type": "integer", "default": 20},
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
            "properties": {"limit": {"type": "integer", "default": 20}},
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
        "description": ("增量更新图谱:改了个别文件后调用,只重建这些文件(秒级)。"
                        "传项目相对路径,如 Assets/Scripts/Enemy.cs。"
                        "会话里也可不调,交给 git post-commit hook 批量刷新。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "项目相对路径列表"},
            },
            "required": ["paths"],
        },
    },
    {
        "name": "unity_animator",
        "description": ("AnimatorController 状态机结构:分层的状态列表(层名 + 默认态 + 目标 clip)、"
                        "状态转移(源/目标 + 条件参数 + 所在层)。这是文件内 fileID 结构,"
                        "grep/代码图谱都看不见,改动画状态机前先用它看全貌。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": _TARGET_DESC},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["target"],
        },
    },
    {
        "name": "unity_timeline",
        "description": ("Timeline(.playable)轨道结构:track 列表(类型/名/GroupTrack 父子嵌套)+ "
                        "每条 track 的 clip(时序 start/duration + clip 类型 + 引用的外部资产)。"
                        "改演出前先用它看结构,别靠 Editor MCP execute_code 去列轨道。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": _TARGET_DESC},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["target"],
        },
    },
]

PROFILES = {
    # 日常会话只暴露查询工具,避免把维护工具 schema 和错误选项塞进模型上下文。
    "core": ("unity_impact", "unity_refs", "unity_components",
             "unity_find", "unity_context", "unity_animator", "unity_timeline"),
    "full": tuple(t["name"] for t in TOOLS),
    "admin": ("unity_stats", "unity_dead_code", "unity_validate",
              "unity_rebuild", "unity_update"),
}
_DEFAULT_LIMITS = {
    "unity_impact": 8,
    "unity_refs": 12,
    "unity_components": 20,
    "unity_find": 5,
    "unity_dead_code": 20,
    "unity_validate": 20,
    "unity_animator": 20,
    "unity_timeline": 20,
}


def tools_for_profile(profile: str) -> list:
    names = set(PROFILES.get(profile, ()))
    if not names:
        raise ValueError(
            f"未知 MCP profile: {profile};可选 {', '.join(PROFILES)}")
    return [t for t in TOOLS if t["name"] in names]


def _ok_text(payload: Any, compact: bool = True) -> Dict:
    if isinstance(payload, str):
        text = payload
    else:
        text = queries.to_json(payload, compact=compact)
    return {"content": [{"type": "text", "text": text}]}


CALL_LOG = "calls.log"
_LOG_MAX_BYTES = 2 * 1024 * 1024


def _log_call(project_root: str, name: str, args: Dict,
              output_text: str, seconds: float, error: str = "",
              resolved_kind: str = "") -> None:
    """把每次工具调用的返回体大小记进 .unity-llm/calls.log。

    用途:量化「用图谱查 vs 让模型通读文件」到底省多少 token
    (中日韩字符按 1 token,其余按约 4 字符/token)。设环境变量
    UNITY_LLM_NO_LOG=1 关掉。
    """
    if os.environ.get("UNITY_LLM_NO_LOG"):
        return
    try:
        path = os.path.join(project_root, graph.DB_DIR, CALL_LOG)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > _LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        rec = {"t": int(time.time()), "tool": name,
               "args": {k: v for k, v in args.items() if k != "paths"},
               "chars": len(output_text),
               "approx_tokens": estimate_tokens(output_text),
               "seconds": round(seconds, 3)}
        paths = args.get("paths") or []
        if paths:
            rec["n_paths"] = len(paths)
            rec["paths_head"] = list(paths)[:3]
        if resolved_kind:
            rec["resolved_kind"] = resolved_kind
        if error:
            rec["error"] = error
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志是附赠品,永远不能影响工具调用


def make_dispatcher(project_root: str,
                    allowed_tools=None) -> Callable[[str, Dict], Any]:
    checked_schema = False
    asked_build = False  # 一个 MCP 进程 = 一个会话,建图只问一次

    def need_graph():
        nonlocal checked_schema, asked_build
        if not graph.has_graph(project_root):
            if asked_build:
                # 已经问过了。用户当时要么选了 grep,要么没建成 ——
                # 都不要再让模型第二次打断他,直接退回 grep/Glob。
                raise RuntimeError(
                    "图谱不存在(本会话已提示过建图,不再重复询问)。"
                    "直接用 grep/Glob 顶,别再调 unity_* 工具;"
                    "除非用户主动说要建图,那时才跑:\n"
                    f"  {graph.build_command(project_root)}")
            asked_build = True
            can_rebuild = allowed_tools is None or "unity_rebuild" in allowed_tools
            how = ("调 `unity_rebuild` 工具"
                   if can_rebuild else "在终端跑下面这条命令")
            raise RuntimeError(
                "图谱不存在。MCP 不会隐式建图。"
                f"建图{graph.BUILD_TIME_HINT}。\n"
                "请先问用户一次(不要自己替他决定;他选 B 之后本会话别再问):\n"
                f"  A) 现在建图({how});\n"
                "  B) 本次不建图,直接用 grep/Glob 顶(prefab 挂载点、UnityEvent 绑定、"
                "序列化引用查不到,结果可能漏)。\n"
                "选 A 时的命令:\n"
                f"  {graph.build_command(project_root)}\n"
                + graph.guidmap_hint(project_root))
        if not checked_schema:
            graph.ensure_schema(project_root)
            checked_schema = True

    def dispatch(name: str, args: Dict) -> Any:
        if allowed_tools is not None and name not in allowed_tools:
            raise ValueError(f"工具 `{name}` 不在当前 MCP profile 中")
        if name == "unity_rebuild":
            return {"ok": True, "stats": graph.build(project_root, verbose=True)}
        need_graph()
        low = bool(args.get("include_low", False))
        ext = bool(args.get("include_external", False))
        limit = int(args.get("limit", _DEFAULT_LIMITS.get(
            name, queries.DEFAULT_LIMIT)))
        if name == "unity_stats":
            out = queries.stats(project_root)
        elif name == "unity_impact":
            out = queries.impact(project_root, args["target"],
                                 depth=int(args.get("depth", 3)),
                                 include_low=low,
                                 include_external=ext, limit=limit)
        elif name == "unity_refs":
            out = queries.find_refs(project_root, args["target"],
                                    include_low=low,
                                    include_external=ext, limit=limit)
        elif name == "unity_components":
            out = queries.components(project_root, args["target"],
                                     include_external=ext, limit=limit)
        elif name == "unity_find":
            out = queries.find_symbols(project_root, args["pattern"],
                                       limit=int(args.get(
                                           "limit", _DEFAULT_LIMITS["unity_find"])),
                                       kind=args.get("kind") or None)
        elif name == "unity_dead_code":
            out = queries.dead_code(
                project_root,
                include_external=ext,
                limit=limit, exclude=args.get("exclude") or None)
        elif name == "unity_validate":
            out = queries.validate(project_root, limit=limit)
        elif name == "unity_context":
            out = context_mod.build_context(
                project_root, args["target"],
                budget_tokens=int(args.get("budget", 2000)))
        elif name == "unity_update":
            paths = args.get("paths")
            if not paths:
                raise ValueError(
                    "unity_update 需要 paths(项目相对路径列表),"
                    "如 [\"Assets/Scripts/Enemy.cs\"]。"
                    "会话内也可不调,交给 git post-commit hook。")
            out = graph.update_files(project_root, list(paths))
        else:
            raise ValueError(f"未知工具: {name}")
        return out
    return dispatch


def _send(msg: Dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve(project_root: str, profile: str = "core") -> None:
    # Windows 控制台默认 GBK;MCP 要求 UTF-8
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    selected_tools = tools_for_profile(profile)
    allowed_tools = {t["name"] for t in selected_tools}
    dispatch = make_dispatcher(project_root, allowed_tools=allowed_tools)
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
            _send({"jsonrpc": "2.0", "id": rid,
                   "result": {"tools": selected_tools}})
        elif method == "tools/call":
            params = req.get("params", {})
            tool = params.get("name", "")
            call_args = params.get("arguments", {}) or {}
            t0 = time.time()
            try:
                result = dispatch(tool, call_args)
                payload = _ok_text(result)
                rk = ""
                if isinstance(result, dict) and isinstance(result.get("resolved"), dict):
                    rk = result["resolved"].get("kind") or ""
                _log_call(project_root, tool, call_args,
                          payload["content"][0]["text"], time.time() - t0,
                          resolved_kind=rk)
                _send({"jsonrpc": "2.0", "id": rid, "result": payload})
            except Exception as e:  # 工具错误按 MCP 规范走 isError
                _log_call(project_root, tool, call_args, "",
                          time.time() - t0, error=str(e))
                _send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"错误: {e}"}],
                    "isError": True,
                }})
        elif method.startswith("notifications/"):
            continue  # initialized 等通知无需回复
        elif rid is not None:
            _send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": f"Method not found: {method}"}})
