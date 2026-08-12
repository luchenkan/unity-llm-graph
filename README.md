# unity-llm-graph

**让任何大模型真正读懂你的 Unity 项目 —— C# 代码图 × 序列化引用图,合二为一。**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/protocol-MCP-green.svg)](https://modelcontextprotocol.io/)
[![Dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)](#)
[![Tests](https://img.shields.io/badge/tests-106%20assertions%20passing-brightgreen.svg)](#测试)

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

# 1. 对你的 Unity 项目建图(几秒到几十秒,取决于项目规模)
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
体积参考:3.75 万资产 / 1 万脚本的项目约 **200 MB**,全量重建约 60 秒。

### Quick Start (English)

Zero third-party deps, Python ≥ 3.9 (standard library only).

```bash
git clone https://github.com/luchenkan/unity-llm-graph.git
cd unity-llm-graph

# 1. Build the graph for your Unity project (seconds to ~1 min)
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
`<project>/.unity-llm/graph.db` (~200 MB for a 37.5k-asset project) — gitignore it.
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
`approx_tokens`(字符数 / 4)、耗时。用来量化「查图谱 vs 让模型通读文件」到底省多少 token。
超过 2 MB 自动轮转成 `calls.log.1`。设 `UNITY_LLM_NO_LOG=1` 关掉。

```bash
# 本次会话所有工具调用一共花了多少 token
python -c "import json,sys;print(sum(json.loads(l)['approx_tokens'] for l in open(sys.argv[1],encoding='utf-8') if l.strip()))" /path/to/Proj/.unity-llm/calls.log
```

### 省了多少:`tools/token_report.py`

结算「净节省」并追加进一个 Markdown 台账,顺便打印一行
`Unity-LLM已经帮你节省了Token：N`:

```bash
python tools/token_report.py --project /path/to/Proj --ledger /path/to/token-savings.md
```

口径刻意保守:`baseline` 是这些调用涉及的源文件**全文** token(不用图谱时模型得整篇
读进上下文,按 graph.db 里的类名/方法名/资产路径解析,同一文件只算一次),
`spent` 是实际返回体的 `approx_tokens`,`net = baseline - spent`。
`unity_update` / `stats` 这类不替代读文件的调用只计 spent,所以净收益可能为负 —— 不虚增。

游标存 `.unity-llm/token_report.state`,每次只结算新增的调用;台账里放一行
`<!-- token-ledger -->`,新记录插在它下面(文件不存在就不写,不乱建文件)。
配成 Claude Code 的 `Stop` hook 就是每个任务结束自动报一次:

```json
{ "hooks": { "Stop": [ { "hooks": [ { "type": "command",
  "command": "python /path/to/unity-llm-graph/tools/token_report.py --project /path/to/Proj --ledger /path/to/token-savings.md" } ] } ] } }
```

## 接入各种 LLM

### Claude Code / Claude Desktop / Cursor(MCP)

```bash
python -m unity_llm init-config --project /path/to/YourUnityProject
```

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
| `unity_components` | prefab/scene 挂了哪些脚本;反过来,某个脚本被哪些 prefab 的哪个 GameObject 挂载        |
| `unity_dead_code`  | Unity 感知死代码:排除生命周期回调、消息方法、字符串调用、**UnityEvent 绑定**、序列化字段、第三方目录   |
| `unity_validate`   | 体检:指向不存在资产的悬空 guid(已排除引擎内置),以及 `.meta` guid 被改写工具处理过的告警         |
| `unity_context`    | token 预算内的精简上下文包(签名 + 依赖),改代码前先调它                               |
| `unity_find`       | 按名字模糊搜索类型 / 成员 / 资产(第三方目录排在后面)                                  |
| `unity_stats`      | 图谱统计:规模、UnityEvent 数、调用边置信度分布、guid 来源                           |
| `unity_rebuild`    | 全量重建图谱                                                          |
| `unity_update`     | **增量更新**:改了几个文件后只重建它们(秒级),支持修改/新增/删除                            |

首次调用任意工具时若图谱不存在会**自动建图**,不需要先手动 `build`。

## 让模型真的用上它(重要)

装好 MCP 只是让工具**可用**,不等于会被调用。新开一个对话时,进模型上下文的
只有工具名 + 描述(约 900 token),图谱内容一条都不读。模型什么时候调,取决于
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
- 改方法/脚本:unity_impact target=类名 或 Owner.Method
- 改 prefab/scene/SO:unity_refs target=资产路径
- 只要签名 + 依赖:unity_context(比读整个文件省 token)
grep 看不到的耦合(prefab 序列化引用、UnityEvent onClick、SendMessage 字符串调用)
只有这套工具能看到。

## 改完代码后
编辑过任何 .cs / .prefab / .unity / .asset,收尾调一次
unity_update paths=[改过的文件相对路径...] 刷新图谱(秒级)。
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
python -m unity_llm find        --project P --pattern 关键词 [--limit 30]
python -m unity_llm deadcode    --project P [--include-external] [--limit 60]
                                 [--exclude 片段 ...]   # 临时排除噪声目录
python -m unity_llm validate    --project P [--limit 60]
python -m unity_llm update      --project P --files Assets/Scripts/Enemy.cs [...]
                                 # 增量更新指定文件(秒级);文件已删除则清除其数据
python -m unity_llm context     --project P --target T [--budget 2000] [--out ctx.md]
python -m unity_llm serve       --project P                        MCP stdio server
python -m unity_llm init-config --project P                        生成 MCP 客户端配置
```

所有命令输出 JSON(context 输出 Markdown),可直接被脚本和其它 LLM 管道消费。
加 `--compact` 输出紧凑 JSON(省 20~30% token,MCP 输出默认就是紧凑的)。

## 特殊情况:`.meta` guid 被改写过的项目

有些项目(资产保护、打包流水线)会把 `.meta` 里的 guid 换成 Base64 长串,
而 prefab/scene YAML 里存的仍是 Unity 真正在用的 32 位十六进制 guid。
这时 `.meta` 文本**完全不可信**,序列化引用会一条都连不上
(`unity_stats` / `unity_validate` 会告警)。

解决:把 `tools/UnityLlmGuidDump.cs` 放进 Unity 工程的任意 `Editor/` 目录,
执行菜单 **Tools/unity-llm/Dump GUID Map**,它用 `AssetDatabase`(唯一权威来源)
导出 `<项目>/.unity-llm/guidmap.tsv`,然后重新 `build` 即可 —— 建图会自动优先用这张表。

## 测试

```bash
python tests/run_tests.py
```

内置一个迷你 Unity 工程(`tests/fixtures/SampleProject`),覆盖建图、影响面、
**同名方法跨类型误报的精度回归**、引用查找(含「方法级 refs 只返回该方法」)、组件清单、
死代码误报控制、**方法组引用(`method_ref`)**、**解析器注释/字符串遮蔽回归**、
悬空引用体检、上下文打包、MCP 握手与 `tools/call`、**增量更新(改/增/删)**
共 **106 项断言 / 21 个测试组**。

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
- 全量重建大项目(3.75 万资产 / 1 万脚本量级)约 60 秒,`graph.db` 约 200 MB;
  日常改动用 `update` 增量更新(秒级),调用边解析是全局的,update 后会整体重跑一次该步骤。

## Changelog

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
- [ ] 文件监听 daemon(免 hook,编辑器里手改也实时跟)
- [x] UnityEvent / Inspector 事件绑定的 YAML 提取
- [ ] `transform.Find` 路径 ↔ 场景层级校验
- [ ] Addressables / AssetBundle 分组分析
- [ ] 导出 DOT / Mermaid 架构图
- [ ] Roslyn 后端(可选,提升 C# 解析精度)

## 贡献

Issue 和 PR 都欢迎。改动前请先跑 `python tests/run_tests.py` 确保 106 项断言全绿。

## License

[MIT](LICENSE)
