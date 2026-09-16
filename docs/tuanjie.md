# 团结引擎 / `.meta` guid 被改写的项目

> 从 README 拆出。**这件事必须在 build 之前确认** —— 搞错的结果不是报错，
> 而是一张挂载点全查成 0 的图，比没有图更容易得出错误结论。

**这件事必须在 build 之前知道**,它直接决定要不要先导 guid 映射表 ——
搞错的结果不是报错,而是一张挂载点全查成 0 的图,比没有图更容易得出错误结论。

| | 国际版 Unity | 团结引擎(Tuanjie,Unity 中国版) |
| --- | --- | --- |
| `.meta` 里的 guid | 32 位十六进制,磁盘文本就是权威 | 资产保护会重写成 Base64 长串 |
| prefab/scene 里 `m_Script` 的 guid | 同上,两边一致 | 仍是引擎内部那份 hex guid,**和 `.meta` 对不上** |
| 建图前置动作 | 无,直接 `build` | 必须先导出 `guidmap.tsv`,否则脚本挂载点全为 0 |

怎么判断:看 `ProjectSettings/ProjectVersion.txt`。有 `m_TuanjieEditorVersion`
字段、或版本号带 `t` 后缀(如 `2022.3.62t4`)就是团结引擎。
`build` 现在会在第 0 步把识别结果打到 stderr,不用自己看。

同一套机制也适用于任何**打包流水线 / 资产保护工具改写过 `.meta`** 的国际版项目。

### 导出 guidmap.tsv

把 `tools/UnityLlmGuidDump.cs` 放进 Unity 工程的任意 `Editor/` 目录,执行菜单
**Tools/unity-llm/Dump GUID Map**,它用 `AssetDatabase`(唯一权威来源)导出
`<项目>/.unity-llm/guidmap.tsv`,然后 `build` 会自动优先用这张表。
不想往工程里塞文件的话,用 Unity MCP 的 `execute_code` 跑同样的逻辑也行:
遍历 `AssetDatabase.GetAllAssetPaths()`,把 `guid + "\t" + path` 写进那个文件。

### 检测到问题时 build 会直接拒绝

非 hex guid 占比 > 2% 且没有 `guidmap.tsv` 时,`build` 抛错退出,不出降级图谱。
确实只想要一张挂载点不可用的图:`build --allow-degraded-guid`。

注意 `.unity-llm/` 下两个文件性质不同:`graph.db` 是**产物**,随时可删重建;
`guidmap.tsv` 是**输入**,重建它需要一个跑着的 Unity —— 清理时别一起删掉。
