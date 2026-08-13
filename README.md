# unity-llm-graph

**让任何大模型真正读懂你的 Unity 项目 —— C# 代码图 × 序列化引用图,合二为一。**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/protocol-MCP-green.svg)](https://modelcontextprotocol.io/)
[![Dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)](#)
[![Tests](https://img.shields.io/badge/tests-147%20assertions%20passing-brightgreen.svg)](#测试)

> English: A zero-dependency dependency-graph engine for Unity projects that merges the
> **C# code graph** (classes / methods / calls / inheritance / engine lifecycle callbacks)
> with the **serialized-reference graph** (`.meta` GUIDs, prefab / scene / ScriptableObject
> YAML references) — the exact coupling that tree-sitter-based LLM tools structurally
> cannot see. Exposes everything through a **CLI** (any LLM, any script) and an
> **MCP server** (Claude Code / Cursor / Trae / Cherry Studio / CodeBuddy / Cline …),
> plus **token-budgeted context packs** for web-only models (Kimi, DeepSeek, 豆包, 文心, 通义).

---

## 为什么需要它

现有 LLM 代码理解工具(code-review-graph、Greptile、Sourcegraph、Cursor 索引……)
底层都是 tree-sitter + 源码 AST,前提是**耦合关系写在代码里**。

Unity 恰恰是个反例——最要命的耦合写在 **prefab / scene 的 YAML 序列化数据**里:

| Unity 特有耦合                                                | 纯代码图谱能看到吗                                  |
| --------------------------------------------------------- | ------------------------------------------ |
| `Awake / Start / Update / OnEnable` 等引擎生命周期回调             | ❌ 静态图上是孤立节点,dead-code 大量误报                 |
| Inspector 里拖的 `[SerializeField]` 引用                       | ❌ 赋值发生在 prefab YAML 里                      |
| `UnityEvent` / 按钮 `onClick` 绑定                            | ❌ 存在 scene 文件里(本工具会解析 `m_PersistentCalls`) |
| `SendMessage("OnHit")` / `Invoke` / `StartCoroutine("X")` | ❌ 字符串调用                                    |
| `transform.Find("UI/Panel/Btn")` 路径耦合                     | ❌ 字符串路径                                    |
| prefab 嵌套、scene 引用 prefab、SO 引用脚本                         | ❌ 全靠 guid                                  |
| AOT / 热更(HybridCLR、xLua、ILRuntime)入口                      | ❌ 反射                                       |

**unity-llm-graph 把两张图拼起来:**

```
┌─────────────────────── Unity 项目 ───────────────────────┐
│                                                          │
│   .meta 扫描          C# 解析            Unity YAML 解析  │
│   guid → 路径    类型/方法/调用/继承    prefab/scene/SO   │
│       │                │                     │           │
│       └────────┬───────┴──────────┬──────────┘           │
│                ▼                  ▼                      │
│        C# 代码图          序列化引用图(guid 边)          │
│                └────────┬─────────┘                      │
│                         ▼                                │
│              SQLite 统一依赖图谱                          │
│          (<项目>/.unity-llm/graph.db)                    │
└─────────────────────────┬────────────────────────────────┘
                          ▼
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                  ▼
     MCP server         CLI            context pack
  Claude / Cursor   任何 LLM/脚本    网页版模型直接粘贴
  Trae / Cherry…    CI / 自动化      Kimi/DeepSeek/豆包…
```

## 快速开始

零第三方依赖,Python ≥ 3.9 即可(只用标准库)。

```bash
# 克隆后直接使用;或 pip install -e . 获得 unity-llm 命令
git clone https://github.com/luchenkan/unity-llm-graph.git
cd unity-llm-graph

# 1. 对你的 Unity 项目建图(首次全量扫描;中型项目约 8-10 分钟)
#    期间 stderr 会打进度,不是卡住
python -m unity_llm build --project /path/to/YourUnityProject

# 2. 立刻可用
python -m unity_llm stats    --project /path/to/YourUnityProject
python -m unity_llm impact   --project /path/to/YourUnityProject --target Enemy.TakeDamage
python -m unity_llm refs     --project /path/to/YourUnityProject --target Assets/Prefabs/Enemy.prefab
python -m unity_llm components --project /path/to/YourUnityProject --target Enemy
python -m unity_llm deadcode --project /path/to/YourUnityProject
python -m unity_llm validate --project /path/to/YourUnityProject
```

目标 `--target` 支持四种写法,自动解析:**类名**(`Enemy`)、**方法名**(`TakeDamage`)、
**资产路径**(`Assets/Scripts/Enemy.cs`)、**guid**(`e10000...`)。

图谱落在 `<项目>/.unity-llm/graph.db`,建议把 `.unity-llm/` 加进 `.gitignore`。
体积参考(实测中型商业项目):**3.75 万资产 / 6 千脚本**,`graph.db` 约 **250 MB**,
初次全量建图约 **8-10 分钟**(本机 519 秒)。之后日常改动用 `update` 增量更新(秒级)。

### Quick Start (English)

Zero third-party deps, Python ≥ 3.9 (standard library only).

```bash
git clone https://github.com/luchenkan/unity-llm-graph.git
cd unity-llm-graph

# 1. First build (mid-size project ≈ 8–10 min; stderr prints progress)
python -m unity_llm build --project /path/to/YourUnityProject

# 2. Query it
python -m unity_llm impact  --project /path/to/YourUnityProject --target Enemy.TakeDamage
python -m unity_llm refs    --project /path/to/YourUnityProject --target Assets/Prefabs/Enemy.prefab
python -m unity_llm context --project /path/to/YourUnityProject --target Enemy --budget 1500

# 3. Or wire it into an MCP client (Claude Code / Cursor / Cline / Trae …)
python -m unity_llm init-config --project /path/to/YourUnityProject
```

`--target` accepts a **type name**, a **method name** (`Type.Method` for an exact
member), an **asset path**, or a raw **guid**. The graph lives in
`<project>/.unity-llm/graph.db` (~250 MB for a 37.5k-asset / 6k-script mid-size
project; first build ≈ 8–10 min) — gitignore it.
If your `.meta` GUIDs were rewritten by an asset-protection tool, see
[the GUID map section](#特殊情况meta-guid-被改写过的项目).

## 项目配置 `.unity-llm.json`

可选。放在 Unity 项目根目录(与 `Assets/` 同级),JSON 格式:

```json
{
  "external_segments": ["ThirdParty", "Plugins", "TextMesh Pro", "Packages"],
  "dead_code_exclude": ["ART_TEST", "Assets/Sandbox", "_Legacy"]
}
```

| 键                  | 作用                                                             |
| ------------------ | -------------------------------------------------------------- |
| `external_segments` | 路径里出现这些片段即视为「第三方」:默认排除在 dead-code / 影响面之外,查询排序也靠后。**追加**在内置默认值之上,改了要重新 `build` |
| `dead_code_exclude` | 只影响 `deadcode`:路径含这些片段的文件不参与死代码统计(测试场景、美术试验田、临时目录),查询期生效,不用重建 |

命令行也能临时排除:`python -m unity_llm deadcode --project P --exclude ART_TEST Sandbox`。
`init-config` 会顺手打印一份可用的 `.unity-llm.json` 模板。

### 调用日志 `.unity-llm/calls.log`

MCP server 会把每次工具调用记一行 JSON:工具名、参数、返回体字符数、
`approx_tokens`、耗时。估算对 CJK 按约 1 字符/token、其它文本按约
4 字符/token,避免中文输出被统一 `chars/4` 低估。
超过 2 MB 自动轮转成 `calls.log.1`。设 `UNITY_LLM_NO_LOG=1` 关掉。

```bash
# 本次会话所有工具调用一共花了多少 token
python -c "import json,sys;print(sum(json.loads(l)['approx_tokens'] for l in open(sys.argv[1],encoding='utf-8') if l.strip()))" /path/to/Proj/.unity-llm/calls.log
```

### 避免全文读取的估算上限:`tools/token_report.py`

估算查询可能避免的全文读取量并追加进 Markdown 台账:

```bash
python tools/token_report.py --project /path/to/Proj --ledger /path/to/token-savings.md
```

`baseline` 是目标涉及源文件的全文估算 token,`spent` 是工具返回体估算 token,
`net = baseline - spent`。这是“如果原本会整篇读取”的**上限估计**,不是 API
账单实测:它不含 MCP schema/输入,也不知道模型原本是否只会读片段。脚本和台账
必须保留“估算上限”措辞,不要把这个数字当精确节省。

游标存 `.unity-llm/token_report.state`,每次只结算新增的调用;台账里放一行
`<!-- token-ledger -->`,新记录插在它下面(文件不存在就不写,不乱建文件)。
配成 Claude Code 的 `Stop` hook 就是每个任务结束自动报一次:

```json
{ "hooks": { "Stop": [ { "hooks": [ { "type": "command",
  "command": "python /path/to/unity-llm-graph/tools/token_report.py --project /path/to/Proj --ledger /path/to/token-savings.md" } ] } ] } }
```

脚本只能算工具调用替代掉的文件读取。**模型自己「少读了什么」它算不出来** ——
比如靠 `unity_impact` 结论决定不去通读四个文件、prefab 用编辑器侧脚本列层级
而不读 YAML,这部分往往是省得最多的一块。补法:在项目规则/记忆里加一条硬约束,
让模型每个任务收尾自己补一行台账并在回复末尾报数。实测有效的写法:

```markdown
任何涉及读代码 / 改代码 / 查依赖的任务,收尾必须两件事一起做:
1. 往 token 台账追加一行(时间 + 净省 + 量级来自哪),更新末尾「累计 N」
2. 回复最后一句报出「本次省 token ~N,累计 N」
数字估算即可,不必推导口径;只调了 unity_update 没替代读文件的任务照实写 0 或负数。
```

Claude Code 放进 memory 或 `CLAUDE.md`,Cursor 放 `.cursor/rules/`。
和 `Stop` hook 是互补的:hook 给硬数据(工具返回体),规则补模型侧省下的通读量。
两个都装就能长期看到累计曲线,而不是每次凭感觉。

## 接入各种 LLM

### Claude Code / Claude Desktop / Cursor(MCP)

```bash
python -m unity_llm init-config --project /path/to/YourUnityProject
```

默认生成 `--profile core`,只向日常会话暴露
`impact / refs / components / find / context` 五个查询工具。需要体检和维护时用
`--profile admin`;兼容旧行为则用 `--profile full`。CLI 子命令始终全部可用。

会打印一段配置,合并到对应文件即可:

| 客户端            | 配置文件                                           |
| -------------- | ---------------------------------------------- |
| Claude Code    | `<项目>/.mcp.json`                               |
| Cursor         | `~/.cursor/mcp.json` 或 `<项目>/.cursor/mcp.json` |
| Claude Desktop | `claude_desktop_config.json` 的 `mcpServers` 字段 |

### 国内 LLM 客户端(MCP)

Trae、Cherry Studio、腾讯 CodeBuddy、Cline 等支持 MCP 的客户端:
在设置里添加 **stdio 类型** MCP 服务器,command 填 `python`,args 填
`-m unity_llm serve --project <你的Unity项目路径>`(`init-config` 会帮你生成好)。

### 网页版模型(Kimi / DeepSeek / 豆包 / 文心 / 通义,无工具调用)

```bash
# 生成一个 1500 token 预算的上下文包,直接粘贴进对话框
python -m unity_llm context --project /path/to/Proj --target Enemy --budget 1500 --out ctx.md
```

### MCP 工具一览

| 工具                 | 作用                                                              |
| ------------------ | --------------------------------------------------------------- |
| `unity_impact`     | 影响面分析:改了这个脚本/方法/prefab 会炸到谁(代码调用+继承、序列化引用传递闭包、UnityEvent 绑定三通道) |
| `unity_refs`       | 谁引用了这个资产:哪些 prefab/scene/SO 的哪个字段、哪段代码(`file:line`)             |
| `unity_components` | prefab/scene 挂了哪些脚本 + **GameObject 层级树(hierarchy)**;反过来,某个脚本被哪些 prefab 的哪个 GameObject 挂载 |
| `unity_dead_code`  | Unity 感知死代码:排除生命周期回调、消息方法、字符串调用、**UnityEvent 绑定**、序列化字段、第三方目录   |
| `unity_validate`   | 体检:指向不存在资产的悬空 guid(已排除引擎内置),以及 `.meta` guid 被改写工具处理过的告警         |
| `unity_context`    | token 预算内的精简上下文包(签名 + 依赖),改代码前先调它                               |
| `unity_find`       | 按名字模糊搜索类型 / 成员 / 资产(第三方目录排在后面)                                  |
| `unity_stats`      | 图谱统计:规模、UnityEvent 数、调用边置信度分布、guid 来源                           |
| `unity_rebuild`    | 全量重建图谱(`admin/full` profile)                                     |
| `unity_update`     | **增量更新**:改了几个文件后只重建它们(秒级),支持修改/新增/删除                            |

MCP 查询不会隐式触发全量建图。首次使用先显式运行 `build`;中型项目大约 8-10 分钟,
期间 stderr 有进度输出,避免看起来像卡住。`rebuild` 只在 `admin/full` profile 暴露。

## 让模型真的用上它(重要)

装好 MCP 只是让工具**可用**,不等于会被调用。新开一个对话时,进模型上下文的
只有当前 profile 的工具名 + 描述,图谱内容一条都不读。模型什么时候调,取决于
你的问法能不能撞上工具描述 —— 问「改 X 会影响谁」大概率会调,问
「这个按钮点了没反应」它可能先去 grep。

同理,**改完文件不会自动刷新图谱**:`unity_update` 也得有人调。

所以要把「可用」变成「默认动作」,以下三档任选:

### 档 1:项目规则文件(最低成本,先做这个)

在 Unity 项目根写 `CLAUDE.md`(Claude Code 每次会话必读;Cursor 用
`.cursor/rules/`,Cline 用 `.clinerules`,内容一样):

```markdown
## 改代码前
动 C# / prefab / scene 前先查影响面,别靠 grep 猜:
- 改方法/脚本:unity_impact target=类名 或 Owner.Method(partial 短名即可)
- 改 prefab/scene/SO:unity_refs / unity_components(grep 看不到)
- 改静态字段/事件总线:unity_impact target=Type.Field
- 只要签名 + 依赖:unity_context(比读整个文件省 token)

## 改完代码后
不要在会话里例行调 unity_update。交给 git post-commit hook。
只有接下来还要查刚改过的文件时才调一次。
```

命中率高但不是 100%:规则是提示,不是强制。

### 档 2:git post-commit hook(推荐,便宜且覆盖手改)

提交后批量增量更新一次。也覆盖你在 Unity 编辑器里手改的 prefab/scene ——
那些改动模型根本不知道。现成模板 `tools/post-commit.sample`:

```bash
cp tools/post-commit.sample <repo>/.git/hooks/post-commit
chmod +x <repo>/.git/hooks/post-commit
# 编辑首行的 UNITY_LLM_DIR,指向本框架仓库
```

内容:

```bash
#!/bin/sh
# unity-llm-graph: 提交后增量刷新依赖图谱
UNITY_LLM_DIR="/path/to/unity-llm-graph"   # 框架仓库路径
ROOT=$(git rev-parse --show-toplevel)
[ -f "$ROOT/.unity-llm/graph.db" ] || exit 0   # 没建过图就不管
FILES=$(git diff-tree --no-commit-id --name-only -r HEAD \
        | grep -E '\.(cs|prefab|unity|asset|controller|anim|playable|mask|preset)$')
[ -z "$FILES" ] && exit 0
cd "$UNITY_LLM_DIR" || exit 0
python -m unity_llm update --project "$ROOT" --files $FILES \
    >> "$ROOT/.unity-llm/hook.log" 2>&1 &
exit 0
```

后台跑、失败静默、永不阻塞提交。git worktree 下 hook 是**多个 worktree 共享**的,
上面用 `git rev-parse --show-toplevel` 动态取项目根,所以每个 worktree 各更新自己的图。

### 档 3:Claude Code PostToolUse hook(实时,但吵)

每次 Edit/Write 之后立刻更新。现成脚本 `tools/hook_post_edit.py`(自己从被编辑
文件向上找项目根,非 Unity 后缀直接跳过,异常一律静默)。写 `<项目>/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Edit|Write",
        "hooks": [{ "type": "command",
                    "command": "python /path/to/unity-llm-graph/tools/hook_post_edit.py" }] }
    ]
  }
}
```

代价:调用边解析是全局的,`update` 每次都会整体重跑一次该步骤 —— 一次重构改十几个
文件就是十几次重复付费。**除非你确实需要会话中途图谱永远最新,否则用档 2。**

三档不冲突。实测组合是 **档 1 + 档 2**:规则管会话内,hook 管提交后兜底。

### 调用边置信度

`obj.Foo()` 到底调的是谁,不用 Roslyn 也能判断大半:建图期推断接收者类型
(字段声明类型 / 方法参数 / `var x = new T()` / `GetComponent<T>()` / `this`·`base`),
再沿基类链找到真正声明该方法的类型。结果落在 `calls.confidence` 上:

| 置信度      | 含义                                       |
| -------- | ---------------------------------------- |
| `high`   | 接收者类型已确定,且在自身或基类链上找到了该方法声明               |
| `medium` | 类型确定但方法在链外(基类在引擎/第三方),或方法名全项目唯一,或字符串调用   |
| `low`    | 接收者类型未知且方法名在多个类型里重名 —— 纯名字碰撞噪声,**默认不返回** |

`low` 默认过滤:实测一个 `Refresh` 这种热名字能拉出 500+ 条假依赖。
真要看全部就传 `include_low=true` / `--include-low`。

### 调用边种类(`calls.kind`)

| kind          | 来源                                                       |
| ------------- | -------------------------------------------------------- |
| `call`        | 裸调用 `Foo()`                                              |
| `dotcall`     | `obj.Foo()` —— 带接收者类型推断                                  |
| `new`         | `new Enemy()`                                            |
| `api_generic` | `GetComponent<T>()` / `AddComponent<T>()` 等泛型 API        |
| `api_string`  | `SendMessage("OnHit")` / `Invoke("X")` / `StartCoroutine("X")` |
| `method_ref`  | **方法组引用**:`Register(OnFoo)`、`btn.onClick += OnFoo`(没括号的回调注册) |
| `field_call`  | **链式静态字段**:`Type.Field.Method()`(事件总线、单例 `Xxx.Instance.Foo()` 等) |

`method_ref` 是 0.4.0 加的:回调注册在 Unity 项目里满地都是,不认它会把
成百上千个真正被调的回调方法误判成死代码。

### 典型 LLM 工作流

```
你:帮我改 Enemy.TakeDamage,加个护盾逻辑
LLM:(先调 unity_impact target=TakeDamage)
    → 发现 Player.Attack 调用它;Enemy.prefab 挂了脚本;Main.unity 引用了 prefab
    → 改之前就知道爆炸半径,而不是改完等 QA 报 bug
LLM:(改完文件后调 unity_update paths=[...])
    → 图谱秒级刷新,下一次查询基于最新代码
```

## CLI 参考

```
python -m unity_llm build       --project P [--no-external-code]   构建/重建图谱
python -m unity_llm stats       --project P                        统计信息
python -m unity_llm impact      --project P --target T [--depth 3] [--include-low]
                                                  [--include-external] [--limit 60]
python -m unity_llm refs        --project P --target T [--include-low] [--limit 60]
python -m unity_llm components  --project P --target T [--limit 60]
python -m unity_llm find        --project P --pattern 关键词 [--kind type|member|asset] [--limit 12]
python -m unity_llm deadcode    --project P [--include-external] [--limit 60]
                                 [--exclude 片段 ...]   # 临时排除噪声目录
python -m unity_llm validate    --project P [--limit 60]
python -m unity_llm update      --project P --files Assets/Scripts/Enemy.cs [...]
                                 # 增量更新指定文件(秒级);文件已删除则清除其数据
python -m unity_llm context     --project P --target T [--budget 2000] [--out ctx.md]
python -m unity_llm serve       --project P [--profile core|full|admin]
python -m unity_llm init-config --project P [--profile core|full|admin]
```

所有命令输出 JSON(context 输出 Markdown),可直接被脚本和其它 LLM 管道消费。
加 `--compact` 输出紧凑 JSON(省 20~30% token,MCP 输出默认就是紧凑的)。

## 建图前先确认:团结引擎 还是 国际版 Unity

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

## 测试

```bash
python tests/run_tests.py
```

内置一个迷你 Unity 工程(`tests/fixtures/SampleProject`),覆盖建图、影响面、
**同名方法跨类型误报的精度回归**、引用查找(含「方法级 refs 只返回该方法」)、组件清单、
死代码误报控制、**方法组引用(`method_ref`)**、**解析器注释/字符串遮蔽回归**、
悬空引用体检、上下文打包、MCP 握手与 `tools/call`、**增量更新(改/增/删)**
并覆盖 partial、namespace 重名、原子 build、tombstone、MCP profile 和严格
token 预算。共 **147 项断言 / 26 个测试组**。

## 能力与局限(诚实声明)

**擅长:** 跨模块多跳追踪(谁调用了调用者)、改动爆炸半径、prefab/scene 引用定位、
UnityEvent 绑定溯源、新人上手项目地图、上下文瘦身(只给模型看签名和依赖,而不是整个文件)。

**局限:**

- C# 解析基于正则而非 Roslyn/tree-sitter:接收者类型推断覆盖字段/参数/局部变量/
  `this`·`base`,但泛型实参、`dynamic`、扩展方法、复杂链式表达式(`a.b.c.Foo()`)
  推不出来 —— 这些边会落到 `low` 并被默认过滤,**宁可漏报不误报**;
- 反射、热更框架(HybridCLR / xLua / ILRuntime)、Timeline / Animation Event 入口
  **静态分析天然不可见**,dead-code 结果永远需要人工复核(工具自带免责声明);
- 字符串耦合(`SendMessage`、`transform.Find` 路径)能检出,但无法验证目标存在性;
- 全量重建中型项目(3.75 万资产 / 6 千脚本,实测)约 8-10 分钟,`graph.db` 约 250 MB;
  日常改动用 `update` 增量更新(秒级),调用边解析是全局的,update 后会整体重跑一次该步骤。

## Changelog

### 0.7.1

全量 `build` 的用户体验:中型项目从 0 建图要数分钟,以前中途零输出,看起来像卡住。

- **stderr 进度心跳**:`python -m unity_llm build` 默认 `verbose=True`,按阶段打印
  `[1/6] 扫描 .meta` → C# → YAML → 调用边解析,每 5 秒刷新当前计数并 `flush`。
  进度走 stderr,不污染 stdout 的最终 JSON。库调用默认仍然安静。
- **耗时口径改成实测**:3.75 万资产 / 6 千脚本的中型项目初次建图约 **8-10 分钟**
  (本机 519 秒),`graph.db` 约 **250 MB**。原先「约 60 秒」偏低。
- MCP 缺图提示同步写上预估时间和「不是卡住」。

### 0.7.0

让图谱覆盖 Editor MCP 的「列层级 / 列组件」查询,补齐一直标「未实现」的
fileID 对象图:

- **新增 `objects` 表**:prefab/scene/asset 里每个 YAML 文档都是一条本地对象记录
  (fileID / classID / GameObject 名 / 所属 GameObject / Transform 父节点 / 脚本 guid)。
- **`unity_components` 返回 `hierarchy`**:给 prefab/scene 时,额外输出缩进的
  GameObject 层级树,每个节点下列出挂载的组件(MonoBehaviour 显示脚本文件名)。
  层级关系来自 Transform 的 `m_Father`,组件通过 `m_GameObject` 反挂 —— 这层耦合
  grep 和 AST 都看不见,以前只能靠 Editor MCP 实时列出来。
- **无 Transform 的 GameObject 也进树**(fixture 里 BattleCoreGO 那种),不会丢节点。
- 组件反挂不依赖 GameObject 的 `m_Component` 列表(它可能不完整),用每个组件的
  `m_GameObject` 字段,更稳健。
- 新增多层级 fixture `UiPanel.prefab`(根→子→兄弟),测试扩到 **142 项断言 / 25 组**。
- 定位:查结构用图谱,改结构用 Editor MCP(写操作仍不可替代)。

### 0.6.0

本轮先修结论可信度,再减会话 token 和构建风险:

- **调用消歧不再“重名取第一个”**:同 namespace 的唯一候选才可升 `high`;
  缺 namespace/using 证据时最多 `medium`。
- **dead-code 修正**:`Type.Field.Method()` 的末段 `Method` 会计为已调用;
  YAML 序列化字段恰好与方法同名不再全局隐藏死方法。
- **删除资产 tombstone**:增量删除后仍可按旧 path/guid 解释入边,
  `validate` 同时继续报告悬空引用。
- **MCP profile**:默认 `core` 只暴露 5 个日常查询工具;`admin/full` 按需启用。
  MCP 不再隐式执行昂贵全量建图。
- **输出瘦身**:`impact` 去掉重复传递路径和恒定说明,`resolved` 只返回公开身份;
  MCP 默认 limit 降为 impact 8 / refs 12 / components 20 / find 5。
- **context 硬预算**:CJK-aware 估算,任何预算都严格封顶;UnityEvent/序列化依赖
  和外部调用方优先于低价值私有成员列表。
- **构建可靠性/性能**:全量 build 写临时 SQLite 后原子替换旧库,
  批量插入完成后才建索引;新增 `calls(kind,arg)` 复合索引。
- **安全边界**:`update` 拒绝越出 Unity 项目根目录的相对路径。
- 测试扩到 **130 项断言 / 24 组**。

### 0.5.0

查询层不强制重建;要看到 **relay 边 / GameObject 名 / is_mono 基类链**,
对现有项目跑一次 `build`。schema 无变更,`update` 仍可用。

对着约 3.75 万资产的真实项目修的实战洞:

- **partial 类短名可查**:同名拆到多个 `.cs` 文件时不再判 ambiguous。
  guid 取所有 partial 文件的并集,方法级 impact 能看到挂在主文件上的 prefab。
- **`kind=field_call`**:`Type.Field.Method()` 链式调用以前被拆成 `Field.Method`
  并标 low 丢掉。现在任意静态字段/单例链都能回溯到类型和字段
  (不写死某个项目的事件 API)。
- **GameObject 名**:沿 `m_GameObject` fileID 还原物体名,
  `unity_components` 不再满屏 `on: MonoBehaviour`。
- **`is_mono` 沿基类链传播**:自定义 UI 基类的子页面也会标成 MonoBehaviour。
- **`unity_find` 默认每类 12 条精简索引**(名+路径),可按 `kind` 过滤。
- **impact 默认丢掉 `dead_code_exclude` 目录**的资产传递引用。
- **Cursor 接入**:`init-config` 写明必须用 `<项目>/.cursor/mcp.json`,
  并带上 `PYTHONPATH`(源码树直接跑)。`unity_update` 缺 `paths` 时给人话错误。
- **calls.log** 记录 `n_paths` / `paths_head` / `resolved_kind`。
- 测试 106 → **117 项断言 / 22 组**。

### 0.4.0

**schema 有变更(`members.code_used`、新表 `dotted_members`):老的 `graph.db`
必须重跑一次 `build` 全量重建;`update` 检测到旧 schema 会直接报错提示。**

在真实项目(3.75 万资产)上的效果:死方法 1643 → 331(-80%),
疑似未用字段 7023 → 319(-95%),抽样 grep 复核过。

- **新增 `method_ref` 调用边**:`Register(OnFoo)` / `evt += OnFoo` 这类方法组引用
  以前完全看不见,导致回调方法被大批误报成死代码(实测本体项目 1300+ 条误报)。
- **字段使用检测**:`members.code_used` 标记「声明之外在类体里被读写过」;
  再加一张 `dotted_members` 名字索引覆盖跨类访问 `other.field`。
- **修解析器遮蔽 bug**:旧实现先去注释再去字符串,`"https://..."` 里的 `//`
  被当成行注释,吃掉后面整行并破坏引号配对 —— 实测因此丢了 82 个类型 / 520 个成员。
  现在单趟遮蔽、字符串优先,非 verbatim 字符串不跨行。
- **修 `api_string` 一直是 0**:`SendMessage`/`Invoke`/`StartCoroutine` 的字符串参数
  提取跑在已被抹空的文本上,功能静默死掉;改为在原文上跑(1298 条边)。
- **内插字符串洞保留**:`$"{AppSetting.VerifyServer}"` 里的表达式不再被抹掉。
- **`refs` 支持方法级过滤**:`--target Owner.Method` 只返回该方法的引用,
  不再把整个类型的边都倒出来;注册点以 `kind=method_ref` 出现。
- **调用方 scope 区分 external / internal**;补充解析提示(resolution hints)。
- **`deadcode --exclude`** 与 `.unity-llm.json` 的 `dead_code_exclude`。
- **guid 告警更准**:不再笼统报警,直接指出常见原因是 `guidmap.tsv` 过期
  (导出之后新增的资产),`stats` 另外给出 `guid_warning_examples` 样例。
- **新增 `.unity-llm/calls.log`**:每次 MCP 工具调用的返回体大小与估算 token,
  方便量化省了多少上下文(`UNITY_LLM_NO_LOG=1` 关闭)。
- 测试从 65 项断言扩到 106 项 / 21 组。

### 0.3.0

- 增量更新 `update` / `unity_update`;调用边置信度分级;UnityEvent YAML 提取。

## Roadmap

- [x] 增量更新(`update` / `unity_update`:只重建变更文件)
- [x] git hook 自动触发增量更新(`tools/post-commit.sample`)+ Claude Code
      PostToolUse hook(`tools/hook_post_edit.py`)
- [x] 链式静态字段调用(`Type.Field.Method()`,含事件总线 / 单例)
- [ ] 文件监听 daemon(免 hook,编辑器里手改也实时跟)
- [x] UnityEvent / Inspector 事件绑定的 YAML 提取
- [x] Unity 本地 fileID 对象图(prefab/scene 的 GameObject 层级树 + 组件挂载,
  `unity_components` 输出 `hierarchy`,替代 Editor MCP 列层级)
- [ ] prefab override `propertyPath` 语义(嵌套 prefab 的字段覆盖)
- [ ] AnimationEvent / Timeline Signal 静态入口
- [ ] `transform.Find` 路径 ↔ 场景层级校验
- [ ] Addressables / AssetBundle 分组分析
- [ ] 导出 DOT / Mermaid 架构图
- [ ] Roslyn 后端(可选,提升 C# 解析精度)

## 贡献

Issue 和 PR 都欢迎。改动前请先跑 `python tests/run_tests.py` 确保 147 项断言全绿。

历次模型评审记录见 [REVIEWS.md](REVIEWS.md)(Kimi k3 / Claude Opus 5 /
Cursor Grok 4.6 / GPT-5.6 Sol)。下一轮请对着真实项目和 `calls.log` 审,
不要只打 fixture。

## License

[MIT](LICENSE)
