# 序列化接线审计与嵌套 prefab(0.12.0)

两个能力解决同一类问题:**prefab 的「隐性契约」断了,而静态工具以前看不见**。
一个 UI 按钮点了没反应,九成是 `[SerializeField]` 没接上线;而这以前只能靠
`execute_code` 反射遍历 + 人眼判读,一个页面半小时起。

## `unity_wiring`:字段到底接没接上

对 prefab/scene 里每个 MonoBehaviour 实例,把两侧对差:

- **C# 侧**:`[SerializeField]` 或 public 字段,排除 `static` / `const` /
  `[NonSerialized]`,以及委托 / `Dictionary` 等 Unity 根本不序列化的类型
- **YAML 侧**:该实例文档里实际出现过的顶层字段

分六类:`ok` / `scalar` / `none`(YAML 里显式 null)/ `broken`(指向不存在的 fileID)/
`dangling`(指向已删资产)/ `missing`(C# 有、YAML 无)。`missing` 带启发式标注,
**只按命名习惯提示,不替你下结论**。

### 三条降噪规则(都是真实数据逼出来的)

| 规则 | 不做的后果(实测 `CardAlbumPageNew.prefab`) |
| --- | --- |
| stripped 占位文档跳过,单独计 `nested_placeholders` | **101 条假 missing**(嵌套 prefab 的字段值在源 prefab 里) |
| 委托 / `Dictionary` 不计入 declared | 修完上一条仍剩 **10 条假 missing**(全是 `LoopListView2` 的 `System.Action`) |
| `[NonSerialized]` 排除 | public 但显式不序列化的字段被当成漏接线 |

### 真实项目读数

```
components 184   nested_placeholders 16   fields_checked 97
ok 84   none 13   broken 0   dangling 0   missing 0
```

`none: 13` 是「YAML 里显式为 null」的字段 —— 对 UI 引用字段而言通常就是没接线。
其中一部分是**设计如此**(例如 `RedDotUI._nodeName` 留空 = 红点常亮),
工具只负责把 13 个摊到你面前,不替你判。

## `unity_prefab_instances`:嵌套 prefab 覆盖了谁

Unity 的嵌套 prefab / variant 在 YAML 里只有一个 `PrefabInstance` 文档(classID 1001),
字段覆盖全在 `m_Modifications` 里,**引用写在 `objectReference` 上**。
源 prefab 自己的文件里没有这些边 —— 所以「这个嵌套实例的字段指向谁」
在源 prefab 视角查不到,必须回到父文件。这就是 09-02 实测返回空的根源。

双向查询:

- 给**父** prefab → 嵌套了哪些源 prefab、各覆盖多少字段、哪些是引用覆盖
  (`reference_overrides`;Transform / 标量覆盖不算耦合)
- 给**源** prefab → 被哪些 prefab 继承、对方改写了它的哪些字段

`unity_refs` 现在也会把嵌套覆盖引用以 `override:<字段>` 列进 `serialized_refs`,
和直接引用可区分。

## 什么时候用

| 场景 | 用什么 |
| --- | --- |
| 美术交了新 prefab / 改了接线 | `unity_wiring` |
| 按钮没反应、界面一片空白 | `unity_wiring`(先看 `none` / `broken`) |
| 改共享 prefab 前评估波及面 | `unity_prefab_instances`(给源 prefab,看被谁继承) |
| 查资产被谁引用(含嵌套覆盖) | `unity_refs`(已含 `override:` 边) |

## 已知边界

- 只审**直接声明**在该脚本类型上的字段;基类字段不进 `declared`
  (所以基类接的线会让 `in_yaml` 大于 `declared`,不报错、也不计 ok)。
- `missing` 是待办清单,不是 bug 清单。
- **老库要先补数据**:跑一次 `build`,或对目标 prefab 单独 `update`;
  否则查询返回明确 hint,而不是空结果让人误判成「没有字段」。
