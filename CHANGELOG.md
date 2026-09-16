# Changelog

> 每版的主题与实测数据。行为约定变更见 [REVIEWS.md](REVIEWS.md)，
> 能力边界见 [README 的能力与局限](README.md#能力与局限诚实声明)。

### 0.11.0

主题:**补两类「结构性看不见」** —— 事件/委托这一类成员与类型,以及空条件调用这一种最基本的调用形式。
都是先在被测项目上量出规模、再动手。

| 盲点 | 规模(4.4 万资产商业项目) | 症状 |
| --- | --- | --- |
| `event` 成员 | **353 条**(入库前 0) | find 搜不到事件名、refs 解析成 unknown |
| `delegate` 类型 | **141 条**(入库前 0) | 整个委托类型不在 type 表里 |
| positional `record` | 本项目 0(框架能力) | `record X(int A);` 整条类型丢失 |
| 空条件调用 `x?.Foo()` | **2765 条调用边** | 只被 `?.` 调用的方法**被误报成死代码** |

- **`event` 入库**(`kind='event'`):两种写法都收 —— 字段式 `event Action X;` 与
  自定义访问器式 `event Action X { add; remove }`。订阅点 `X += h` 本来就以
  `method_ref` 记着 **handler**,所以 dead-code 不受影响;缺的是事件本身的可发现性。
- **`delegate` 入库**:delegate 是类型声明,`CLASS_RE` 从来认不出它。放在类型区解析,
  顺带保证 `field_scopes` 覆盖到它(否则后面的成员解析会 KeyError)。
- **positional record**:`CLASS_RE` 要求结尾是 `{`,而 `record X(int A);` 以 `;` 收尾 →
  **整条类型都丢了**。参数会编译成 init-only 属性,所以额外收成属性。
  该改动把 `bases` 的捕获组从 5 移到 6,调用处同步改了。
- **空条件调用**:`DOTCALL_RE` / `CHAIN_CALL_RE` 的 `.` 前多一个 `?` 就整条漏掉。
  实测 1772 条 / 555 个文件,其中 53% 指向非 `Invoke` 的业务方法
  (`Release` / `ResolveRef` / `Refresh` / `Cancel`)。补上后 dead-code 候选
  **326 → 309**,调用边 **255095 → 257860**。
- **`FIELD_RE` 收 `required`**:C# 11 修饰符,Unity 2022(只到 C# 9)用不到,
  但框架会跑在更新引擎的项目上 —— 漏掉它该字段会整个丢失。
- **`stats` 新增 `types_by_kind`**(class / struct / interface / enum / record / delegate),
  对称于 `members_by_kind`。
- **`note_property` 改名 `note_no_call_edge`**:属性与事件同样"没有调用方",
  提示语合并,`refs` / `impact` 命中两者时都会带上。
- 修补 0.10.0 的一处遗漏:`_present_node` 漏登记 `property`,导致属性节点绕过
  字段裁剪、多暴露内部 `guid`。
- 测试 213 → **227 项断言 / 34 组**。

### 0.10.0

主题:**C# 属性(property)入库**。此前属性完全不在图里 —— 而数据模型类的 API 面几乎全是属性。

起因是在一个 4.4 万资产 / 1.1 万脚本的真实项目上量出来的:源码里 **7700+ 条属性声明**
(独立正则口径 6400+),而 `members.kind` 只有 `method` / `field`,`property` 一条都没有。
后果具体到工具:`unity_find`(该项目**最高频**的 MCP 工具,占 65% 调用)搜属性名返回空、
`unity_refs PlayerModel.FirstSelectIPID` 解析成 `resolved_kind: unknown`、
`unity_context` 列不出属性签名。

- **解析**:`csharp.py` 新增 `PROPERTY_RE` + `CsProperty`,三种写法都收 ——
  自动属性 `{ get; private set; }`、访问器体、表达式体 `=> expr`;无修饰符的
  接口成员(实测该项目 194 条)也覆盖。
- **两条防误报约束**(实测踩到才加的):`^[ \t]*` **行首锚定** —— 否则 LINQ lambda
  会被整片吃掉,`ToDictionary(x => x, x => ...)` 里的 `> x, x =>` 看着就像
  「类型 + 名字 + `=>`」(该项目单文件就有 3 条这类假属性);类型首字符限 `[\w<]`
  作兜底。加约束后该项目命中数 8927 → 7779,**类型首字符异常 0 条**。
- **入库**:`members.kind='property'`,复用已有列,**无 schema 变更**。
  `serialized` / `code_used` 恒 0 —— Unity 不序列化属性(序列化的是编译器生成的
  `<Hp>k__BackingField`),而 dead-code 只扫 `method` / `field`,属性天然不进
  「未使用字段」榜。
- **查询打通**:`resolve_target` 支持 `Owner.Prop`,与字段一样**要求带 owner**
  (`Name` / `Id` / `Count` 这类属性名重名率太高,不做全局猜测);`impact` /
  `refs` / `components` 的 kind 白名单统一加 `property`;`context` 本来就不按
  kind 过滤 members,属性自动出现在接口列表里。
- **`stats` 新增 `members_by_kind`**:method / field / property 分项计数。
- **老库提示**:属性只对**被重解析过**的文件生效,而增量 `update` 只处理传入的
  文件 —— 0.9.x 建的库跑 `update` 后属性仍为空。`update` 结尾会检测「库中
  property 为 0 但有 .cs 资产」并提示**需要一次全量 build**,避免「升级了但
  find 还是搜不到属性」这种无声失败。
- 测试 193 → **213 项断言 / 33 组**。

**实测**(4.4 万资产 / 1.1 万脚本的商业项目,全量 rebuild 6m12s):

| 查询 | 升级前 | 升级后 |
| --- | --- | --- |
| `members_by_kind` 里的 property | **0** | **11459** |
| `find FirstSelectIPID` | 空结果 | 命中 `PlayerModel`(kind=property) |
| `refs PlayerModel.FirstSelectIPID` | `resolved_kind: unknown` | `kind=property`,签名 `int FirstSelectIPID` |
| `context PlayerModel` | 只有方法,没有属性 | `## Key signatures` 列出属性 |
| deadcode | — | 属性不进任何榜,计数无激增 |
| 解析开销 | — | **+1.5%**(1200 文件 7.92s vs 7.80s) |

属性天然没有调用方,极易被读成「没人用、可以删」。`refs` / `impact` 命中属性时
会带 `note_property` 说明这是语言事实:「调用方为 0」是正常的,不构成删除依据。

**诚实局限**:属性访问(`x.Prop`)在 C# 里**不是调用边**,所以属性不会有
`impact` / `refs` 的调用方 —— 这是语言事实,不是图谱缺陷。属性入库解决的是
**可发现性**(找得到名字、拿得到签名),不是「谁读写了这个属性」;getter/setter
体里的调用也暂不抽取(需要另开一套入口)。

### 0.9.1

主题:hook / git 漏刷时,图谱与磁盘对不上就按文件增量补齐,不必 rebuild。

- **查询期自愈**:`impact / refs / components / animator / timeline` 发现
  目标文件 `file_state` 与磁盘不一致时,只 `update` 这些文件再查。成功带
  `graph_healed`,失败才留 `graph_stale`。
- **`update --stale`**:扫全表漂移(含磁盘已删)。不传 `--files` 等同 `--stale`。
  可与 `--files` 并集,用来收从未进过图谱的新资产。
- **MCP `unity_update`**:`paths` 改为可选;省略则 `heal_stale`。
- **git hook 模板**:post-commit / post-merge / post-rewrite 在提交文件之外
  加 `--stale`,空 diff 不再直接退出。
- 测试 191 → **193 项断言 / 32 组**。

### 0.9.0

主题:把运行时错误接进静态图 + 明确工具链边界。

- **`error-report` / `unity_error_report`(错误现场打包)**:贴一段 Unity 堆栈
  (Console 右键复制 / logcat 原文,支持 Console / .NET / IL2CPP 三种格式),
  解析出涉事的用户代码类型(引擎帧/生成代码自动过滤),逐个 resolve 进图谱,
  每个类型返回迷你影响面(top 调用方 + 资产引用计数 + stale 检查)。
  把「贴日志 → AI 猜 → 来回问」压成一次调用。CLI 支持 `--file` / stdin 管道。
- **三层边界声明**:README 新增「AI × Unity 工具链」定位表 —— 磁盘静态图
  (本框架)/ 编辑器会话(Editor MCP)/ 运行时会话(独立项目),三层互补
  不重叠,`error-report` 是运行时层到静态图的桥。
- **文档脱敏**:`docs/visual_assets_extension.md` 与 README 中残留的真实项目
  资产名替换为合成名,实测数据统一为「某项目」表述。
- 测试 186 → **191 项断言 / 32 组**。

### 0.8.0

主题:「知道自己什么时候答错了」+「不等人问就说」。前七轮把「答得对」打磨到位后,
剩下的最大风险是图谱静默过期,最大的浪费是图谱知识没人来问。

- **stale 检测(图谱新鲜度自验证)**:build/update 记录每个收录文件的
  mtime/size(`file_state` 表);`impact / refs / components / animator / timeline`
  查询时校验目标文件,磁盘版本变了就在返回体里带 `graph_stale` 告警。防线从
  建图期延伸到查询期 —— hook 后台静默失败时,用户至少能从查询结果里看出
  「这结论基于旧结构」。老库(无 file_state 表)静默降级,不误报。
- **`digest` 命令(多文件影响面摘要)**:一次吃进整批变更文件(git diff 的
  输出,`--files -` 支持 stdin 管道),聚合出「调用方类型 top / 引用方资产 top /
  prefab 挂载点数」三张榜。pull / code review 后先看波及谁,再决定细查哪。
  与 git hooks(0.7.3)衔接:图谱知道答案,现在让答案主动可见。
- **`usage` 命令(MCP 工具采用率)**:把 Round 5/7 人工翻 calls.log 的方法论
  产品化 —— 工具直方图、`unity_update` 刷新占比、core 查询工具零调用告警。
  在真实项目(172 次调用)上验证:挖出 3 条 8 月中旬旧版 server 的
  「未知工具」历史错误。
- **未知工具报错带自纠信息**:MCP 调 profile 外工具时,错误信息附上当前
  可用工具清单,不再只回「未知工具」三个字(实测模型会原样重试浪费轮次)。
- 测试 172 → **186 项断言 / 31 组**;README badge 与实测数字同步。

### 0.7.3

补上 pull 拉取后图谱不更新的缺口(此前只有 post-commit,本地提交才刷图):

- **新增 `tools/post-merge.sample`**:`git pull`(fast-forward 或产生 merge commit)
  和 `git merge` 之后增量刷新。差异文件用 `git diff --name-only ORIG_HEAD HEAD`
  —— pull/merge 前 git 一定把 ORIG_HEAD 设为更新前的 HEAD。
- **新增 `tools/post-rewrite.sample`**:覆盖 `git pull --rebase` / `git rebase`
  (rebase 路径不触发 post-merge)。也覆盖 `commit --amend`。同一套 ORIG_HEAD
  差异逻辑;重放的本地提交虽已被 post-commit 刷过,update 幂等,多刷无害。
- README「档 2」改为三个 hook 的组合说明:本地提交 / 手改 + pull / pull --rebase
  全覆盖,已实装在 3.75 万资产的真实项目上验证。
- 三个 hook 均后台执行、静默失败、不阻塞 git 操作;pull 无变化时 diff 为空直接退出。

### 0.7.2

Timeline / Animator 资产录入补全(视觉资产扩展 Phase 1 收口,详见
`docs/visual_assets_extension.md`):

- **Animator 分层 + 默认态**:`animator_states` 加 `layer` / `is_default`,子状态机写成
  `层名/子机名` 路径;`animator_transitions` 加 `layer`,AnyState 转移 from 为空。
- **Timeline 轨道嵌套**:GroupTrack 的 `m_Children` 还原成 `parent_fileid`
  (实测某大型 Timeline 52 轨中 41 轨有父轨),`unity_timeline` 输出 `parent`。
- **clip 类型**:`timeline_clips.asset_kind` 记 PlayableAsset 的脚本 guid。实测某项目 318 个 clip
  里 277 个播的是内联录制动画(没有外部 guid),只有这一列能说明这条 clip 是什么。
- **非 ASCII 名字解码**:Unity 把中文名写成 `"骨架|Idle"`,入库前解码(实测某项目 11 行受影响)。
- **修增量更新丢数据**:`update_files` 以前既不重解析 `.controller`/`.playable` 的文件内结构,
  也不删旧行 —— 改过的动画资产会留脏行或整段丢失。已修 + 回归测试覆盖。
- 老库自动 `ALTER TABLE` 补上述 5 列,不需要全量重建。

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
