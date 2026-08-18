# unity-llm 视觉资产扩展设计（Animation / Animator / Timeline / ShaderGraph / VFX）

> 目标：把 unity-llm 从「代码图 + prefab/scene 序列化引用」扩展到「演出资产的可读结构」，
> 让 LLM 不只是知道「A 引用了 B」，而是能看懂「这个 Timeline 有几条 track、每条 track 绑了谁、
> clip 在什么时间点、Animator 状态机怎么连」。
>
> 本文先给现状盘点 + 差距 + 分阶段方案，代码在方案确认后落地。

---

## 一、现状盘点（已实测 graph.db，不是猜的）

`graph.py` 的 `YAML_ASSET_EXTS` 已经覆盖：

```python
{".prefab", ".unity", ".asset", ".controller", ".anim",
 ".mat", ".physicMaterial", ".playable", ".mask", ".preset"}
```

实测各类型**已经捕获的跨资产 guid 边**：

| 资产 | 已捕获 | 说明 |
|---|---|---|
| `.controller`(Animator) | `m_Motion -> .anim/.fbx` | ✅ 状态→clip 有了 |
| `.mat`(材质) | `m_Shader -> .shader`、`m_Texture -> 贴图` | ✅ 材质→shader/贴图 有了 |
| `.anim`(clip) | `script -> .cs`、`value -> 贴图` | ✅ clip→目标组件 有了 |
| `.playable`(Timeline) | 只有 `m_Script -> track 脚本` | ⚠️ 只有 track 类型，没有 clip 内容/时序/绑定 |
| `.shadergraph` | **无**（294 个资产 0 条边） | ❌ 未解析（是 JSON，不是 YAML） |
| `.vfx` | **无**（本项目 0 个） | ❌ 未解析 |

**根因**：现在的 `parse_unity_yaml` 只抽**跨资产 guid 边**（`{fileID, guid}`），
内部结构是靠 `fileID` 互相引用的（`{fileID: X}` 无 guid），而 parser 明确把这部分 `continue` 掉了
（`unity_yaml.py` 第 215 行 `if guid is None: continue`）。
所以「Animator 状态机怎么连」「Timeline 的 track→clip→绑定」这些**文件内结构**目前是空白。

---

## 二、真实 YAML 结构（已读文件验证，这是接下来要解析的东西）

### AnimatorController（`battle_cut_in_tshh.controller` 实测）

```
!u!91  AnimatorController
  m_AnimatorLayers[]  → m_StateMachine: {fileID: 1107id}
!u!1107 AnimatorStateMachine
  m_ChildStates[]     → m_State: {fileID: 1102id}
  m_DefaultState: {fileID: 1102id}
  m_AnyStateTransitions[]
!u!1102 AnimatorState
  m_Name
  m_Motion: {fileID: 7400000, guid: <clip>, type: 2}   ← 已有 guid 边
  m_Transitions[]
!u!1101 AnimatorStateTransition
  m_DestinationState: {fileID: 1102id}
  m_Conditions[]
```

→ 要补的是：`Controller → Layer → StateMachine → (ChildStates / DefaultState) → State → (Motion / Transitions → DestinationState)` 这条**内部 fileID 链**，以及 `m_Conditions`（参数名 + 比较符 + 阈值）。

### Timeline（`timeline_weapon_wujinjiuhu.playable` 实测）

```
!u!114 MonoBehaviour  (m_Script = TimelineAsset.cs)      ← 根
  m_Tracks[]           → 各 track 的 fileID
!u!114 MonoBehaviour  (m_Script = AnimationTrack.cs / ControlTrack.cs ...)
  m_Clips[]            → clip 的 fileID
  m_Binding / m_DisplayName
!u!114 MonoBehaviour  (m_Script = AnimationPlayableAsset.cs / ControlPlayableAsset.cs)
  m_AnimationClip / m_SourceGameObject  ← 引用 clip / 物体
!u!74  AnimationClip   ← 内联的录制动画（Recorded），不是外部 .anim
```

→ 要补的是：`TimelineAsset → Tracks → Clips → (PlayableAsset → 目标 clip/物体)` 这条链，
外加 clip 的时序（`m_Start`/`m_Duration`）和 track 绑定目标（`m_Binding` / clip 里的 `path: xxx/yyy`）。

---

## 三、差距总结

1. **结构缺口（最大价值）**：内部 fileID 链没解析 → 状态机图、Timeline 轨道图是空白。
2. **JSON 资产缺口**：`.shadergraph`、`.vfx` 是 JSON 序列化，走不了现有 YAML parser。
3. **写侧缺口（操作阶段）**：Editor MCP（MCPForUnity）**有 `manage_animation`**（能建 clip、
   建 AnimatorController、加 state、`legacyAnim.AddClip`），但**没有 Timeline 工具**——
   往 Timeline 里塞 clip 目前只能 `execute_code` 走 Timeline API。

---

## 四、分阶段方案

### Phase 1：内部 fileID 链解析（Animator + Timeline 结构）——最优先

- 新增 `visual_assets.py`（或扩展 `unity_yaml.py`），对 `.controller` / `.playable` 做**第二遍解析**：
  1. 复用现有 `parse_unity_yaml` 的 doc 切分结果；
  2. 建立 `fileID → YamlDoc` 索引；
  3. 按上面第二节的字段，把内部 fileID 引用解析成结构边。
- 新表（或 `objects` 加 `extra` 列存 JSON）：
  ```sql
  -- 状态机边
  animator_states(src_guid, state_fileid, name, motion_guid, layer)
  animator_transitions(src_guid, from_state, to_state, conditions_json)
  -- Timeline 边
  timeline_tracks(src_guid, track_fileid, track_type, display_name, binding_path)
  timeline_clips(src_guid, track_fileid, clip_fileid, start, duration, asset_guid)
  ```
- 新查询命令：`components --target xxx.controller` 输出层级化状态机；`timeline --target xxx.playable` 输出轨道树。
- 这一步做完，LLM 就能回答：「这个 Animator 有哪些状态、怎么切换、每个状态播哪个 clip」「这个 Timeline 有哪几条 track、每条绑了谁、clip 在什么时间段」。

**Phase 1 状态：已完成（0.7.2）**，落在 `unity_llm/visual_assets.py` + `graph.py` 建表 + `queries.animator/timeline`
（CLI `animator` / `timeline`，MCP `unity_animator` / `unity_timeline`）。实际入库的东西：

| 维度 | 入库内容 | 表/列 |
|---|---|---|
| Animator 层 | 每个 layer 名，state 属于哪层，子状态机写成 `层名/子机名` 路径 | `animator_states.layer` |
| Animator 默认态 | layer / 子状态机的 `m_DefaultState` | `animator_states.is_default` |
| Animator 转移 | from/to + 条件（参数名、`m_ConditionMode`、阈值）+ 所在层；AnyState 转移 from 为空 | `animator_transitions.layer` |
| Timeline 轨道嵌套 | GroupTrack 的 `m_Children` 还原成父子（实测 `Gacha_tenTimeline` 52 轨里 41 轨有父） | `timeline_tracks.parent_fileid` |
| Timeline clip 类型 | clip 的 PlayableAsset 的 `m_Script` guid，用来区分 Animation/Control/Activation clip。**关键**：实测 318 个 clip 里 277 个播的是内联录制动画（没有外部 guid），只有这一列能说明它是什么 | `timeline_clips.asset_kind` |
| 名字可读性 | Unity 把非 ASCII 名写成 `"骨架|Idle"`，入库前解码（实测真实项目 11 行受影响） | 全部 name 列 |
| 增量刷新 | `update_files` 之前既不重解析视觉结构、也不删旧行 → `.controller`/`.playable` 增量更新会丢数据或留脏行。已修 + 回归测试覆盖 | `graph.update_files` |

老库自动 `ALTER TABLE` 补这 5 列，不用重建。

### Phase 2：ShaderGraph / VFX（JSON 解析）——次优先

- 新增 JSON 分支：`.shadergraph` / `.vfx` 用 `json.loads`（Unity 这两个都是 JSON）。
- ShaderGraph 要抽：`m_SubGraphs`（subgraph 引用）、属性（`m_Properties`）、节点引用。
- 价值略低于 Phase 1（着色器图不是"演出结构"，更多是"材质生产"），可后置。

### Phase 3：写侧 Timeline 工具——验证「操作阶段」

- 目标：让 LLM 能把一个 AnimationClip 放进某条 track 的某个时间段。
- 两条路（都需在 Unity 里跑）：
  1. **扩展 MCPForUnity**：加一个 `manage_timeline` 工具（`TimelineAsset.CreateTrack` +
     `track.CreateClip` + `director.SetGenericBinding`），走 HTTP MCP，最顺。
  2. **Editor 脚本 + `execute_menu_item`**：项目里放一个静态 Editor 方法，LLM 用 MCPForUnity
     现成的 `execute_menu_item` 触发。
- 这一步是「操作阶段行不行」的关键验证，做完才知道 LLM 闭环「读结构 → 改 Timeline → 验证」是否成立。

---

## 五、风险 / 待验证

- [x] 团结引擎（`%TAG !u! tag:yousandi.cn,2023`）的 fileID 是负数（如 `&-2764362647030779139`），
      解析时按**字符串**处理 fileID。已按字符串实现 + fixture 覆盖负 fileID。
- [x] Animator `m_Conditions` 的 `m_ConditionMode` 原样入库（数值 + 参数名 + 阈值），不做语义映射：
      Unity 各版本枚举值不保证一致，映射错比不映射更坏。
- [ ] Timeline 的 `m_Binding` / PlayableDirector 的场景绑定**仍未捕获** —— 现在能说出「有哪几条 track、
      clip 在什么时间段、播的是什么类型」，但说不出「这条 track 绑的是场景里哪个物体」。是 Phase 1 的已知缺口。
- [ ] `parse_shadergraph` 在真实项目 294 个 `.shadergraph` 上**产出 0 行**：真实 ShaderGraph JSON 用
      `m_ObjectId` 引用块，不是设计时假设的 `m_SubGraphs`。Phase 2 要按真实格式重写。
- [ ] `.vfx` 解析**未验证**（本项目 0 个资产）。
- [ ] 写侧 Timeline 需要 Unity 编辑器里真机验证（`TimelineAsset` API 的 `CreateClip` 签名在 Unity 6 是否有变）。

---

## 六、和「公司资源会过期」的关系

这套扩展的**方法论**（把 LLM 看不见的序列化结构本地化成可查询图）才是可带走的资产；
具体解析出的 Timeline/Animator 内容仍是项目数据，留在项目里。
建议把「Phase 1 的解析器」做成**通用库**（任何 Unity 项目的 .controller/.playable 都能吃），
这样它和 unity-llm 一样，是可迁移的独立能力，而不是一次性脚本。
