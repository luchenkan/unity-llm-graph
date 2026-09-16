# unity-llm-graph

**让任何大模型真正读懂你的 Unity 项目 —— C# 代码图 × 序列化引用图,合二为一。**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/protocol-MCP-green.svg)](https://modelcontextprotocol.io/)
[![Dependencies](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)](#)
[![Tests](https://img.shields.io/badge/tests-240%20assertions%20passing-brightgreen.svg)](#测试)

> English: A zero-dependency dependency-graph engine for Unity projects that merges the
> **C# code graph** (classes / delegates / methods / fields / properties / events /
> calls / inheritance / engine lifecycle callbacks)
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

# 或者直接用内置报告:工具直方图 + 零调用告警
python -m unity_llm usage --project /path/to/Proj
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
| `unity_error_report` | **错误现场打包**:贴一段 Unity 堆栈,返回每个涉事类型的迷你影响面(top 调用方/引用方) |
| `unity_stats`      | 图谱统计:规模、UnityEvent 数、调用边置信度分布、guid 来源                           |
| `unity_rebuild`    | 全量重建图谱(`admin/full` profile)                                     |
| `unity_update`     | **增量更新**:给 paths 只重建这些文件;不传 paths 则只补与磁盘不一致的文件(hook 漏刷自愈) |

MCP 查询不会隐式触发全量建图。首次使用先显式运行 `build`;中型项目大约 8-10 分钟,
期间 stderr 有进度输出,避免看起来像卡住。`rebuild` 只在 `admin/full` profile 暴露。

**图谱过期自愈(stale → 小更新)**:增量更新靠 git hook 后台跑且静默失败,挂掉时
图谱会悄悄冻结。0.8.0 起建图/更新会记录每个收录文件的 mtime/size(`file_state`),
查询时校验目标文件。**0.9.1 起对不上不再只告警**:`impact / refs / components /
animator / timeline` 会按这些文件做一次增量 `update`,结果带 `graph_healed`;
补不齐才留 `graph_stale`。也可主动 `unity-llm update --stale`(MCP 的
`unity_update` 不传 paths 等同)。新文件从未进过图谱的不在 `file_state` 里,
仍需 hook 的 `--files` 或全量 `build`。老库没有这张表时静默降级,不误报。

**采用率度量**:`python -m unity_llm usage` 把 `.unity-llm/calls.log` 聚合成
工具直方图:每个工具的调用次数/token/错误、`unity_update` 刷新占比、core
查询工具零调用告警。回答「模型到底在用哪些工具、图谱有没有被消费」——
这个方法曾发现过「刷新占七成、refs/components 零调用」的真实问题,现在是
一条命令的事。

## 定位:AI × Unity 工具链的三层边界

Unity 开发中的 AI 工具按「AI 看的是什么」分三层,本框架只做第一层:

| 层 | 看的是什么 | 归属 | 状态 |
|---|---|---|---|
| **磁盘静态图** | 代码 + 序列化资产(离线、可缓存) | **unity-llm-graph**(本仓库) | 成熟 |
| **编辑器会话** | 场景/prefab 实时读写、编译、控制台 | Editor MCP(MCP for Unity 等) | 用现成开源 |
| **运行时会话** | Play Mode / 真机日志流、Play 冒烟 | 独立项目(unity-llm-runtime) | 规划中 |

三层互补不重叠:查询找本框架,改东西找 Editor MCP,运行时观测找 runtime 层。
`unity_error_report` 是三层的桥:输入一段运行时堆栈(第三层产出),用静态图
(第一层)解释它从哪来、炸到谁。

## 让模型真的用上它

装好 MCP 只是让工具**可用**，不等于会被调用：新开一个对话时进模型上下文的
只有工具名和描述，图谱内容一条都不读。要把「可用」变成「默认动作」，
三档接入方式（项目规则文件 / git hooks / 编辑器 hook）与典型工作流见
**[docs/hooks.md](docs/hooks.md)**。

两个最容易踩的点：
- 规则文件是提示不是强制，命中率高但不上 100%；
- 不要在会话里例行调 `unity_update` —— 交给 hook 与查询期自愈。

## 调用边语义

调用边带置信度（`low` 默认过滤，过滤掉的是「接收者类型未知且方法名重名」的
纯噪声）和 `kind`（`call` / `dotcall` / `method_ref` / `field_call` …）。
两者的完整说明见 **[docs/graph-semantics.md](docs/graph-semantics.md)**。

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
                                 # --files - 可从 stdin 读(接 git diff 管道)
                                 # --stale 只补 file_state 与磁盘不一致的文件
                                 # 不传 --files 等同 --stale(hook 漏刷自愈,不必 rebuild)
python -m unity_llm digest     --project P --files Assets/... [...]
                                 # 一批变更文件的影响面摘要:调用方/引用方/挂载点 top 榜
                                 # pull / code review 后先看波及谁再决定细查哪
python -m unity_llm usage      --project P
                                 # MCP 工具采用率:读 calls.log 出直方图 + 零调用告警
python -m unity_llm context     --project P --target T [--budget 2000] [--out ctx.md]
python -m unity_llm serve       --project P [--profile core|full|admin]
python -m unity_llm init-config --project P [--profile core|full|admin]
```

所有命令输出 JSON(context 输出 Markdown),可直接被脚本和其它 LLM 管道消费。
加 `--compact` 输出紧凑 JSON(省 20~30% token,MCP 输出默认就是紧凑的)。

## 团结引擎（guid 可能被改写）

看 `ProjectSettings/ProjectVersion.txt`：带 `m_TuanjieEditorVersion`、或版本号有
`t` 后缀（如 `2022.3.62t4`），就是团结引擎 —— `.meta` 的 guid 会被资产保护重写成
Base64，**必须先导 `guidmap.tsv` 再 build**，否则挂载点全查成 0。
识别方法、导出步骤与「build 直接拒绝」的规则见 **[docs/tuanjie.md](docs/tuanjie.md)**。

## 测试

```bash
python tests/run_tests.py
```

内置一个迷你 Unity 工程(`tests/fixtures/SampleProject`),覆盖建图、影响面、
**同名方法跨类型误报的精度回归**、引用查找(含「方法级 refs 只返回该方法」)、组件清单、
死代码误报控制、**方法组引用(`method_ref`)**、**解析器注释/字符串遮蔽回归**、
悬空引用体检、上下文打包、MCP 握手与 `tools/call`、**增量更新(改/增/删)**
并覆盖 partial、namespace 重名、原子 build、tombstone、MCP profile 和严格
token 预算,以及 **stale 检测(磁盘变更自愈/老库降级)、digest 聚合、usage
采用率报告**、**C# 属性入库(四种写法 / `Owner.Prop` 解析 / LINQ lambda 不误报)**、
**事件与委托入库、positional record、空条件调用 `x?.Foo()`**、
**声明解析边界(switch 表达式类型模式臂不误当属性、`ref` 返回属性、
`Generic<K,V>.Member` 类型、多声明符事件、嵌套类型 `full_name`)**。
共 **240 项断言 / 35 个测试组**。

## 能力与局限(诚实声明)

**擅长:** 跨模块多跳追踪(谁调用了调用者)、改动爆炸半径、prefab/scene 引用定位、
UnityEvent 绑定溯源、新人上手项目地图、**类型的完整 API 面(方法 / 字段 / 属性)**、
上下文瘦身(只给模型看签名和依赖,而不是整个文件)。

**局限:**

- C# 解析基于正则而非 Roslyn/tree-sitter:接收者类型推断覆盖字段/参数/局部变量/
  `this`·`base`,但泛型实参、`dynamic`、扩展方法、复杂链式表达式(`a.b.c.Foo()`)
  推不出来 —— 这些边会落到 `low` 并被默认过滤,**宁可漏报不误报**;
- 反射、热更框架(HybridCLR / xLua / ILRuntime)、Timeline / Animation Event 入口
  **静态分析天然不可见**,dead-code 结果永远需要人工复核(工具自带免责声明);
- **属性和事件不产生调用边**:`x.Prop` 在 C# 里不是方法调用,`OnDead` 本身也只是
  一个字段式成员,所以 `impact` / `refs` 对它们只给类型层面的资产依赖,不会有
  「谁读写了它」。入库是为了让 `find` 搜得到名字、`context` 列得出签名 ——
  而且两者恒不计入 dead-code(Unity 不序列化属性;事件的订阅点早被 `method_ref`
  记成处理函数被引用);getter/setter 与 add/remove 体里的调用也暂不抽取;
- **C# 语法盲点**(正则解析的已知边界,均为「漏掉该成员」而非错误结果):
  索引器 `this[int i]` 不入库(`Owner.Member` 无法寻址,入库也查不到);
  显式接口实现只保留最后一段名字(`IFoo.Count` → `Count`);
  泛型实参嵌套超过两层的类型(`Dictionary<string, Dictionary<int, List<T>>>`)
  和元组类型(`(int hp, string name) Status => …`)在属性/事件声明上匹配不到;
  跨多行书写的声明只认「类型和名字在同一行」;
  `#if` 条件编译不求值,两个分支的成员都会入库;
- 字符串耦合(`SendMessage`、`transform.Find` 路径)能检出,但无法验证目标存在性;
- 全量重建中型项目(3.75 万资产 / 6 千脚本,实测)约 8-10 分钟,`graph.db` 约 250 MB;
  日常改动用 `update` 增量更新(秒级),调用边解析是全局的,update 后会整体重跑一次该步骤。

## 更新日志

见 **[CHANGELOG.md](CHANGELOG.md)**（每版主题 + 真实项目实测数据）。

## Roadmap

- [x] 增量更新(`update` / `unity_update`:只重建变更文件)
- [x] git hook 自动触发增量更新(`tools/post-commit.sample`)+ Claude Code
      PostToolUse hook(`tools/hook_post_edit.py`)
- [x] pull / merge / rebase 拉取后自动增量更新(`tools/post-merge.sample` /
      `tools/post-rewrite.sample`)
- [x] 图谱新鲜度自验证(查询期 stale 告警,`file_state` 表)+ 不一致时按文件
      增量自愈(`update --stale` / 查询 `graph_healed`)+ 多文件影响面
      摘要(`digest`)+ 采用率度量(`usage`)
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

Issue 和 PR 都欢迎。改动前请先跑 `python tests/run_tests.py` 确保 240 项断言全绿。

历次模型评审记录见 [REVIEWS.md](REVIEWS.md)(Kimi k3 / Claude Opus 5 /
Cursor Grok 4.6 / GPT-5.6 Sol / GLM-5.3 / DeepSeek-V4.1-Flash)。下一轮请对着
真实项目和 `calls.log` 审,不要只打 fixture。

## License

[MIT](LICENSE)
