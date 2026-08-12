"""unity-llm CLI:任何 LLM / 脚本 / 人都能用的入口。

    python -m unity_llm build      --project /path/to/UnityProject
    python -m unity_llm stats      --project ...
    python -m unity_llm impact     --project ... --target Enemy.TakeDamage
    python -m unity_llm refs       --project ... --target Assets/Prefabs/Enemy.prefab
    python -m unity_llm components --project ... --target ButtonPro
    python -m unity_llm find       --project ... --pattern Player
    python -m unity_llm deadcode   --project ...
    python -m unity_llm validate   --project ...
    python -m unity_llm context    --project ... --target Enemy --budget 1500
    python -m unity_llm serve      --project ...      # MCP stdio server
    python -m unity_llm init-config                   # 生成各客户端 MCP 配置示例
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from . import graph, queries, context as context_mod


def _project(args) -> str:
    return args.project


def _print(args, obj) -> None:
    print(queries.to_json(obj, compact=getattr(args, "compact", False)))


def cmd_build(args) -> int:
    stats = graph.build(_project(args), verbose=True,
                        include_external_code=not args.no_external_code)
    _print(args, {"ok": True, "stats": stats})
    return 0


def cmd_stats(args) -> int:
    _print(args, queries.stats(_project(args)))
    return 0


def cmd_impact(args) -> int:
    _print(args, queries.impact(_project(args), args.target, depth=args.depth,
                                include_low=args.include_low,
                                include_external=args.include_external,
                                limit=args.limit))
    return 0


def cmd_refs(args) -> int:
    _print(args, queries.find_refs(_project(args), args.target,
                                   include_low=args.include_low,
                                   limit=args.limit))
    return 0


def cmd_components(args) -> int:
    _print(args, queries.components(_project(args), args.target, limit=args.limit))
    return 0


def cmd_find(args) -> int:
    _print(args, queries.find_symbols(_project(args), args.pattern,
                                      limit=args.limit))
    return 0


def cmd_deadcode(args) -> int:
    _print(args, queries.dead_code(_project(args),
                                   include_external=args.include_external,
                                   limit=args.limit,
                                   exclude=args.exclude))
    return 0


def cmd_validate(args) -> int:
    _print(args, queries.validate(_project(args), limit=args.limit))
    return 0


def cmd_update(args) -> int:
    _print(args, graph.update_files(_project(args), args.files))
    return 0


def cmd_context(args) -> int:
    text = context_mod.build_context(_project(args), args.target,
                                     budget_tokens=args.budget)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"已写入 {args.out} (约 {len(text)//4} tokens)")
    else:
        print(text)
    return 0


def cmd_serve(args) -> int:
    from .mcp_server import serve
    serve(_project(args))
    return 0


def cmd_init_config(args) -> int:
    import os
    project = os.path.abspath(args.project)
    py = sys.executable.replace("\\", "/")
    config = {
        "mcpServers": {
            "unity-llm": {
                "command": py,
                "args": ["-m", "unity_llm", "serve", "--project", project],
            }
        }
    }
    print("# 把下面 JSON 合并进你的 MCP 客户端配置:\n")
    print("# Claude Code:        <项目>/.mcp.json")
    print("# Cursor:             ~/.cursor/mcp.json 或 <项目>/.cursor/mcp.json")
    print("# Claude Desktop:     claude_desktop_config.json 的 mcpServers 字段")
    print("# Trae / Cherry Studio / CodeBuddy 等: 设置里的「MCP 服务器」添加 stdio 类型\n")
    print(json.dumps(config, ensure_ascii=False, indent=2))
    from .meta import CONFIG_FILE, SUGGESTED_EXCLUDES
    print(f"\n# 可选:项目根 {CONFIG_FILE}(过滤死代码噪声目录 / 追加第三方目录判定)")
    print(json.dumps({"dead_code_exclude": list(SUGGESTED_EXCLUDES[:3]),
                      "external_segments": ["MyVendorSDK"]},
                     ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="unity-llm",
        description="Unity 依赖图谱:让 LLM 同时看懂 C# 代码与 prefab/scene 序列化引用")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_project(sp):
        sp.add_argument("--project", default=".",
                        help="Unity 项目根目录(含 Assets/ 的目录),默认当前目录")
        sp.add_argument("--compact", action="store_true",
                        help="输出紧凑 JSON(省 20~30%% token)")

    def add_limit(sp, default=queries.DEFAULT_LIMIT):
        sp.add_argument("--limit", type=int, default=default,
                        help=f"每个分区最多返回条数,默认 {default}")

    def add_low(sp):
        sp.add_argument("--include-low", action="store_true",
                        help="包含 low 置信度(接收者类型未知且方法名重名)的调用边,"
                             "默认过滤 —— 打开通常会多出成百上千条名字碰撞噪声")

    def add_external(sp):
        sp.add_argument("--include-external", action="store_true",
                        help="包含 3rd/Plugins/Packages 等第三方目录的结果")

    sp = sub.add_parser("build", help="构建/重建依赖图谱")
    add_project(sp)
    sp.add_argument("--no-external-code", action="store_true",
                    help="不解析第三方目录里的 C#(建图更快,但第三方对你代码的"
                         "调用会看不见)")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("stats", help="图谱统计信息")
    add_project(sp)
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("impact", help="影响面分析:改了它会炸到谁")
    add_project(sp)
    sp.add_argument("--target", required=True,
                    help="guid / 资产路径 / 类名 / 方法名(支持 Owner.Method)")
    sp.add_argument("--depth", type=int, default=3, help="资产级传递深度")
    add_low(sp)
    add_external(sp)
    add_limit(sp)
    sp.set_defaults(func=cmd_impact)

    sp = sub.add_parser("refs", help="引用查找:谁引用了这个资产/脚本")
    add_project(sp)
    sp.add_argument("--target", required=True)
    add_low(sp)
    add_limit(sp)
    sp.set_defaults(func=cmd_refs)

    sp = sub.add_parser("components",
                        help="组件清单:prefab 挂了哪些脚本 / 脚本被谁挂载")
    add_project(sp)
    sp.add_argument("--target", required=True)
    add_limit(sp)
    sp.set_defaults(func=cmd_components)

    sp = sub.add_parser("find", help="按名字模糊搜索类型/成员/资产")
    add_project(sp)
    sp.add_argument("--pattern", required=True)
    add_limit(sp, default=30)
    sp.set_defaults(func=cmd_find)

    sp = sub.add_parser("deadcode", help="Unity 感知的死代码检测")
    add_project(sp)
    add_external(sp)
    sp.add_argument("--exclude", nargs="*", default=None,
                    help="额外排除的路径片段/前缀(如 ART_TEST Assets/Demo);"
                         "长期生效建议写进项目根 .unity-llm.json 的 dead_code_exclude")
    add_limit(sp)
    sp.set_defaults(func=cmd_deadcode)

    sp = sub.add_parser("validate", help="体检:悬空 guid + .meta 被改写告警")
    add_project(sp)
    add_limit(sp)
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("update", help="增量更新:只重建指定文件(日常改代码后用)")
    add_project(sp)
    sp.add_argument("--files", nargs="+", required=True,
                    help="项目相对路径,如 Assets/Scripts/Enemy.cs;"
                         "文件已删除则清除其图数据")
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("context", help="为目标生成 token 预算内的 LLM 上下文包")
    add_project(sp)
    sp.add_argument("--target", required=True)
    sp.add_argument("--budget", type=int, default=2000, help="token 预算")
    sp.add_argument("--out", help="输出到文件(默认打印到 stdout)")
    sp.set_defaults(func=cmd_context)

    sp = sub.add_parser("serve", help="以 MCP stdio server 方式运行")
    add_project(sp)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("init-config", help="打印各 LLM 客户端的 MCP 配置示例")
    add_project(sp)
    sp.set_defaults(func=cmd_init_config)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # Windows 控制台默认 GBK,JSON 里的中文会变成乱码
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
