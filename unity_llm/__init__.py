"""unity-llm-graph: Unity-aware dependency graph & context engine for LLMs.

把 Unity 项目的两张图拼成一张:
  1. C# 代码图(类 / 方法 / 调用 / 继承 / 引擎生命周期回调)
  2. 序列化引用图(.meta guid / prefab / scene / asset YAML 里的引用)

对外提供:
  - CLI          : 任何 LLM / 脚本都能调用,输出 token 友好的文本
  - MCP server   : Claude / Cursor / Trae / Cherry Studio 等直接接入
  - context pack : 按 token 预算生成精简上下文,喂给不支持工具的模型
"""

__version__ = "0.7.3"
