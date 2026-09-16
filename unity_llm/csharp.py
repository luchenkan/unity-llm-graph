"""C# 静态解析器(零依赖,基于正则 + 花括号配对)。

不追求编译器级精度,目标是覆盖 LLM 场景最关键的信息:
  - 类型声明:class / struct / interface / enum / record / delegate,
    含命名空间、基类、修饰符
  - 成员:方法签名、字段、属性、事件,含 [SerializeField] / public 等可见性
  - Unity 语义:生命周期函数(Awake/Start/Update...)、消息方法(OnXxx)、
    SendMessage("...") / GetComponent<T>() / transform.Find("...") 等字符串耦合
  - 调用边:obj.Method() / Method() / new Xxx(),**带接收者表达式、
    行号、以及本文件内能推断出的接收者类型**
  - 方法组引用:Register(OnFoo) / evt += OnFoo(没有括号的回调注册)

接收者类型推断(不需要 Roslyn,覆盖实测 ~80% 的调用):
  1. 裸调用 `X()` / `this.X()` / `base.X()` → 当前类型(解析期再走基类链)
  2. `field.X()`         → 字段声明类型(同类字段表)
  3. `param.X()`         → 方法参数声明类型
  4. `local.X()`         → 局部变量类型(var + new T / GetComponent<T> / T x = ...)
  5. `SomeType.X()`      → 大写开头且不是已知变量 → 视为静态调用,类型即接收者
  6. 其它(链式 a.b.C())→ 留空,由解析期降级为 low confidence

这些正是 tree-sitter 图谱在 Unity 项目里看不见、或看得见但认不准的部分。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Unity 生命周期 / 消息回调:引擎通过反射调用,静态调用图上看不到调用者,
# 必须显式标记,否则 dead-code 检测会大量误报。
UNITY_LIFECYCLE = {
    "Awake", "Start", "OnEnable", "OnDisable", "OnDestroy", "OnApplicationQuit",
    "OnApplicationPause", "OnApplicationFocus", "Update", "FixedUpdate",
    "LateUpdate", "OnGUI", "OnDrawGizmos", "OnDrawGizmosSelected",
    "OnValidate", "Reset", "OnBecameVisible", "OnBecameInvisible",
    "OnPreCull", "OnPreRender", "OnPostRender", "OnRenderObject",
    "OnWillRenderObject", "OnRenderImage", "OnAudioFilterRead",
    "OnParticleCollision", "OnParticleTrigger", "OnTransformChildrenChanged",
    "OnTransformParentChanged", "OnRectTransformDimensionsChange",
    "OnBeforeTransformParentChanged", "OnDidApplyAnimationProperties",
    "OnCanvasGroupChanged", "OnRectTransformRemoved",
}
UNITY_MESSAGE_PREFIXES = (
    "OnCollision", "OnTrigger", "OnMouse", "OnJointBreak", "OnControllerCollider",
)

CS_KEYWORDS = {
    "if", "else", "for", "foreach", "while", "do", "switch", "case", "return",
    "new", "typeof", "sizeof", "nameof", "default", "using", "lock", "catch",
    "finally", "try", "throw", "base", "this", "checked", "unchecked", "fixed",
    "get", "set", "add", "remove", "when", "where", "in", "out", "ref", "is",
    "as", "yield", "await", "static", "class", "struct", "interface", "enum",
    "namespace", "void", "var", "delegate", "event", "params", "goto",
}

MODIFIERS = {
    "public", "private", "protected", "internal", "static", "abstract",
    "virtual", "override", "sealed", "readonly", "const", "async", "extern",
    "unsafe", "partial", "volatile", "new",
}

# 这些接收者是引擎/语言内建的,推断出的"类型"没有意义(且会污染统计)
BUILTIN_RECEIVERS = {
    "transform", "gameObject", "Debug", "Mathf", "Vector2", "Vector3", "Vector4",
    "Quaternion", "Color", "Time", "Input", "Application", "Screen", "Physics",
    "Physics2D", "Resources", "Object", "GameObject", "Convert", "Math",
    "String", "string", "int", "float", "bool", "Console", "Array", "Enum",
    "Path", "File", "Directory", "JsonUtility", "PlayerPrefs", "SceneManager",
    "Instantiate", "Destroy", "base", "this",
}

# 行内有界的 attribute 前缀(捕获组,保持后续 group 编号不变)。
# 严禁跨行:跨行空白会在 (...)* 迭代间产生指数级划分方式 → 灾难性回溯
# (VolumetricLightBeam 那种连续 attribute 行的文件会直接卡死)。
# 代价:独占一行的 attribute 不会被捕获,声明本身仍会匹配。
_ATTR = r"((?:[^\S\n]*\[[^\]\n]*\][^\S\n]*)*)"

NS_RE = re.compile(r"^\s*namespace\s+([\w.]+)", re.MULTILINE)
CLASS_RE = re.compile(
    _ATTR +
    r"((?:(?:public|private|protected|internal|static|abstract|sealed|partial|readonly|ref|unsafe|new)[ \t]+)*)"
    r"\b(class|struct|interface|enum|record)[ \t]+(\w+)"
    r"(?:[ \t]*<[^>\n{}]*>)?"
    # positional record 的参数表:`record Foo(int X, string Y);`。
    # 旧版不认这种写法,**整条类型都进不了图**(比成员丢失更严重)。
    # 参数名会另外收成属性(编译器为它们生成 init-only 属性)。
    # 这一组插在 bases 之前,所以下面 bases 的组号由 5 变成 6 —— 调用处同步改了。
    r"(?:[ \t]*\(([^)\n]*)\))?"
    r"(?:[ \t]*:[ \t]*([^\{\n]+?))?"
    # 结尾可以是 `{`(有体)或 `;`(positional record 没有体)
    r"[ \t]*(?:\r?\n[ \t]*)?(?:\{|;)"
)
METHOD_RE = re.compile(
    _ATTR +
    r"((?:(?:public|private|protected|internal|static|abstract|virtual|override|sealed|async|extern|unsafe|new|readonly)[ \t]+)+)"
    r"([\w<>\[\],.\? ]*?)[ \t]+(\w+)[ \t]*\(([^)]*)\)"
    r"(?:[ \t]*(?:\{|=>|where)|[ \t]*\r?\n[ \t]*\{)"
)
# 无修饰符的方法(Unity 常见写法,如 `void Update()`),仅匹配行首,降低误报
METHOD_RE2 = re.compile(
    r"^[ \t]*" + _ATTR +
    r"([\w<>\[\],.\?]+)[ \t]+(\w+)[ \t]*\(([^)]*)\)[ \t]*(?:\{|=>|\r?\n[ \t]*\{)",
    re.MULTILINE,
)
FIELD_RE = re.compile(
    _ATTR +
    # required 是 C# 11 的修饰符:Unity 2022(含团结引擎 2022.3)只到 C# 9,
    # 业务代码里不会出现,但框架会跑在更新引擎的项目上 —— 漏掉它该字段会整个丢失,
    # 收着不吃亏。
    r"((?:(?:public|private|protected|internal|static|readonly|const|volatile|new|"
    r"required)[ \t]+)+)"
    # `=(?!>)`:表达式体属性 `public float X => _x;` 不是字段,不能当序列化字段
    r"([\w<>\[\],.\?]+)[ \t]+(\w+)[ \t]*(?:=(?!>)[^;\n]*)?;"
)
# 属性(property)。**Unity 不序列化属性** —— 序列化的是编译器生成的
# backing field(`<Hp>k__BackingField`),名字和属性名不同,图谱本来就抓不到。
# 所以属性恒 serialized=0,也永远不进 dead-code 的「未使用序列化字段」判定
# (见 queries.dead_code 里的 kind 白名单)。
# 但属性是 C# 类型的主要 API 面:数据模型类(PlayerModel 这类)几乎全是属性,
# 缺了它们 `unity_find` 搜不到名字、`refs Type.Prop` 解析成 unknown、
# `unity_context` 少掉半张接口表。三种写法都要收:
#   自动属性 `public int Hp { get; private set; }`
#   访问器体 `public int Hp { get { ... } }`
#   表达式体 `public int Hp => _hp;`
# 判据:名字后面必须是 `{`(且紧跟 get/set/init 访问器关键字)或 `=>`;
# 带 `(` 的是方法,留给 METHOD_RE/METHOD_RE2。
# 两条防误报约束,缺一不可(实测某 4.5k 脚本项目):
#   1. `^[ \t]*` 行首锚定 —— 否则 LINQ lambda 会被整片吃掉:
#      `ToDictionary(x => x, x => ...)` 里的 `> x, x =>` 看着就像
#      「类型 `> x,` + 名字 `x` + `=>`」。合法的属性声明总在行首。
#   2. 类型首字符限 `[\w<]` —— 合法类型名不会以 `>`/`,`/`.` 开头,
#      这条是上面那条被 `[Attr]` 同行写法绕过时的兜底。
# 用 `{` + 访问器关键字做约束(而不是只认 `{`),是为了让**无修饰符**的
# 接口成员/私有属性也能收,同时不会把 `else {` / `try {` / `class Foo\n{` 误当属性。
PROPERTY_RE = re.compile(
    r"^[ \t]*" + _ATTR +
    r"((?:(?:public|private|protected|internal|static|abstract|virtual|override|"
    r"sealed|new|readonly|unsafe|extern|required)[ \t]+)*)"
    # 类型名允许一次空格分隔,覆盖 `Dictionary<string, int>` 这类带空格泛型
    r"([\w<][\w<>\[\],.\?]*(?:[ \t]+[\w<][\w<>\[\],.\?]*)?)[ \t]+(\w+)"
    r"[ \t\r\n]*(?:\{[ \t\r\n]*(?:get|set|init)\b|=>)",
    re.MULTILINE,
)
# 事件(event)。C# 里 event 是**独立的成员类别**,既不是字段也不是属性:
# `public event Action OnDead;` 尾部虽是 `;`,但 `event` 不在 FIELD_RE 的修饰符表里,
# 所以旧版完全收不到 —— 实测某 4.4 万资产项目 245 条(223 字段式 + 22 自定义访问器),
# 图谱里 0 条。表现和属性盲点同型:find 搜不到事件名、refs Owner.Event 解析成 unknown、
# context 列不出「这个类会通知谁」。
# 注意 deadcode 不受影响:订阅点 `Foo.OnDead += Handler` 早就被 method_ref 记成
# Handler 被引用;这里补的是**事件本身**的可发现性。
# 两种写法都收:字段式 `event Action X;` 与自定义访问器式 `event Action X { add; remove }`。
EVENT_RE = re.compile(
    r"^[ \t]*" + _ATTR +
    r"((?:(?:public|private|protected|internal|static|virtual|override|abstract|"
    r"sealed|new|unsafe|extern)[ \t]+)*)"
    r"event[ \t]+"
    r"([\w<][\w<>\[\],.\?]*(?:[ \t]+[\w<][\w<>\[\],.\?]*)?)[ \t]+(\w+)"
    r"[ \t\r\n]*(?:;|\{[ \t\r\n]*(?:add|remove)\b|=>)",
    re.MULTILINE,
)
# delegate 是**类型声明**(编译器生成的委托类),不是成员。
# CLASS_RE 只认 class/struct/interface/enum/record,所以旧版整个委托类型都不在图里 ——
# 实测该项目 58 条。与 record 的 positional 形式同属「类型级丢失」,比成员丢失更严重。
DELEGATE_RE = re.compile(
    r"^[ \t]*" + _ATTR +
    r"((?:(?:public|private|protected|internal|unsafe|new)[ \t]+)*)"
    r"delegate[ \t]+"
    r"([\w<][\w<>\[\],.\?]*(?:[ \t]+[\w<][\w<>\[\],.\?]*)?)[ \t]+(\w+)"
    r"[ \t]*(?:<[^>\n]*>)?[ \t]*\(([^)]*)\)[ \t]*;",
    re.MULTILINE,
)
CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(")
# `\??`:空条件调用 `cache?.Release()`。C# 里事件、可选依赖、缓存命中的惯用写法,
# 实测某项目 1772 条 / 涉及 555 个文件,其中 53% 指向非 Invoke 的业务方法
# (`Release` / `ResolveRef` / `Refresh` / `Cancel`)。旧正则因为 `.` 前多了个 `?`
# 全部漏掉 —— 不只是少一条边,还会让只被 `x?.Foo()` 调用的方法被误报成死代码。
DOTCALL_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*\??\s*\.\s*([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(")
# Type.Field.Method() —— 静态字段/单例上的链式调用。
# 两段 DOTCALL 只会看成 Field.Method,recv_type 被大写启发式误判成字段名,
# 事件总线、Xxx.Instance.Foo() 都会因此掉进 low。
CHAIN_CALL_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*\??\s*\.\s*([A-Za-z_]\w*)\s*\??\s*\.\s*([A-Za-z_]\w*)"
    r"\s*(?:<[^>]*>)?\s*\(")
GENERIC_APIS = (
    "GetComponent", "GetComponentInChildren", "GetComponentInParent",
    "GetComponents", "GetComponentsInChildren", "GetComponentsInParent",
    "AddComponent", "FindObjectOfType", "FindFirstObjectByType",
    "FindAnyObjectByType", "Instantiate",
)
GENERIC_CALL_RE = re.compile(
    r"\b(" + "|".join(GENERIC_APIS) + r"|Resources\.Load|"
    r"Addressables\.LoadAssetAsync)\s*<([\w.<>,\s]+)>"
)
STRING_CALL_RE = re.compile(
    r"\b(SendMessage|BroadcastMessage|SendMessageUpwards|Invoke|InvokeRepeating|"
    r"StartCoroutine|StopCoroutine|CancelInvoke|transform\.Find|GameObject\.Find|"
    r"GameObject\.FindWithTag|Resources\.Load|PlayerPrefs\.Get\w+|SceneManager\.LoadScene)"
    r"\s*\(\s*\"([^\"]+)\""
)
NEW_RE = re.compile(r"\bnew\s+([A-Z]\w*)\s*(?:\(|\{)")
# 方法组/委托引用:`OnNet<T>(OnG2C_Xxx_Ack)`、`btn.onClick.AddListener(OnClick)`、
# `evt += OnFoo;` —— 名字后面没有括号,CALL_RE / DOTCALL_RE 全抓不到。
# Unity 项目里协议回调和 UI 回调大量走这条路,漏掉会让 dead-code 大面积误报
# (实测某 37.5k 资产项目:1323 条"死方法"里绝大多数是这种回调)。
DELEGATE_ARG_RE = re.compile(r"[(,]\s*([A-Z]\w*)\s*(?=[,)])")
DELEGATE_ASSIGN_RE = re.compile(r"(?:\+=|-=|=)\s*([A-Z]\w*)\s*;")
# 跨类字段/属性读写:`other.count = 1` / `if (cfg.enabled)` —— 没有括号,
# 不是调用边。只按「名字」收集(不存边),给 dead-field 检测当白名单用:
# 存全量 member_ref 边会让库膨胀几十万条,而这里只需要"这个名字被点过"。
DOTTED_MEMBER_RE = re.compile(
    r"\.\s*([A-Za-z_]\w*)\b(?!\s*[(<])")
USING_RE = re.compile(r"^\s*using\s+([\w.]+)\s*;", re.MULTILINE)

# 局部变量类型推断
LOCAL_NEW_RE = re.compile(
    r"(?<![\w.])(?:var|[\w<>\[\],.\?]+)\s+(\w+)\s*=\s*new\s+([\w.]+)")
LOCAL_GENERIC_RE = re.compile(
    r"(?<![\w.])(?:var|[\w<>\[\],.\?]+)\s+(\w+)\s*=\s*(?:[\w.]+\s*\.\s*)?(?:"
    + "|".join(GENERIC_APIS) + r")\s*<\s*([\w.]+)\s*>")
LOCAL_DECL_RE = re.compile(
    r"^[ \t]*(?!return\b|new\b|else\b|await\b|yield\b)([A-Z][\w.]*)"
    r"(?:<[^>=;\n]*>)?[ \t]+(\w+)[ \t]*=", re.MULTILINE)
FOREACH_RE = re.compile(r"\bforeach\s*\(\s*([\w.<>\[\]]+)\s+(\w+)\s+in\b")

# 注释和字符串必须**同一遍**扫描,且字符串排在前面:
# 先删注释会把 "https://cdn..." 里的 // 当成注释,吃掉行尾的收尾引号,
# 引号奇偶一乱,后面的字符串正则会跨行吞掉成百行真代码
# (实测:一个带 URL 常量的文件后半截全部消失,字段/调用全丢)。
# 非 verbatim 字符串禁止跨行(\n 不在字符类里),避免单个坏引号引发雪崩。
_MASK_RE = re.compile(
    r'\$@"(?:[^"]|"")*"'        # 内插 verbatim $@"..."
    r'|\$"(?:[^"\\\n]|\\.)*"'   # 内插字符串 $"..."
    r'|@"(?:[^"]|"")*"'         # verbatim @"..." (可跨行,"" 转义)
    r"|\"(?:[^\"\\\n]|\\.)*\""  # 普通字符串
    r"|'(?:[^'\\\n]|\\.)*'"     # 字符字面量
    r"|//[^\n]*"                # 行注释
    r"|/\*.*?\*/",              # 块注释
    re.DOTALL)


def _blank_interpolated(s: str) -> str:
    """内插字符串:`{...}` 里是真代码(字段读写、方法调用),只抹字面量部分。"""
    out = []
    depth = 0
    for ch in s:
        if ch == "{":
            depth += 1
            out.append(" ")
        elif ch == "}":
            depth = max(0, depth - 1)
            out.append(" ")
        elif depth > 0:
            out.append(ch)
        else:
            out.append("\n" if ch == "\n" else " ")
    return "".join(out)


def _strip_comments_and_strings(text: str) -> str:
    """去掉注释和字符串字面量(保留等长空白,保持行号不漂移)。"""
    def blank(m: re.Match) -> str:
        s = m.group(0)
        if s.startswith("$"):
            return _blank_interpolated(s)
        return "".join("\n" if c == "\n" else " " for c in s)
    return _MASK_RE.sub(blank, text)


def short_type(name: str) -> str:
    """`System.Collections.Generic.List<Foo>` → `List`;取短名便于跨文件匹配。"""
    n = re.sub(r"<.*", "", name or "").strip()
    n = n.rstrip("[]?")
    return n.split(".")[-1].strip()


def _enclosing_class(pos: int, classes: List["CsType"]) -> Optional["CsType"]:
    best = None
    for c in classes:
        if c.body_start is not None and c.body_start <= pos <= (c.body_end or 0):
            if best is None or c.body_start > best.body_start:
                best = c
    return best


def _brace_pairs(text: str) -> dict:
    """一次 O(n) 扫描,返回 {开括号位置: 匹配的闭括号位置}。
    大文件(如 Luban/FlatBuffers 生成代码)下比逐字符循环快几个数量级。"""
    pairs = {}
    stack = []
    for m in re.finditer(r"[{}]", text):
        if m.group(0) == "{":
            stack.append(m.start())
        elif stack:
            pairs[stack.pop()] = m.start()
    return pairs


def _parse_params(params: str) -> Dict[str, str]:
    """`int amount, Enemy target, ref Vector3 pos` → {amount: int, target: Enemy}。"""
    out: Dict[str, str] = {}
    depth = 0
    buf = []
    chunks = []
    for ch in params:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth <= 0:
            chunks.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        chunks.append("".join(buf))
    for c in chunks:
        c = c.split("=")[0].strip()
        toks = [t for t in c.split() if t not in
                ("ref", "out", "in", "params", "this", "readonly")]
        if len(toks) >= 2:
            out[toks[-1]] = short_type(toks[-2])
    return out


@dataclass
class CsCall:
    kind: str          # call / dotcall / new / api_generic / api_string / field_call
    name: str          # 被调方法名 / 类型名;field_call 时是中间字段名
    recv: str          # 接收者表达式原文("" = 裸调用);field_call 时是类型名
    recv_type: str     # 推断出的接收者类型短名("" = 未知)
    arg: str           # api_string 的字符串参数;field_call 时是末段方法名
    line: int          # 调用所在行(evidence,LLM 可直接跳转)

    @property
    def target(self) -> str:
        return f"{self.recv}.{self.name}" if self.recv else self.name


@dataclass
class CsMethod:
    name: str
    signature: str
    modifiers: str
    return_type: str
    attributes: str
    line: int
    params: Dict[str, str] = field(default_factory=dict)
    is_lifecycle: bool = False
    is_message: bool = False
    calls: List[CsCall] = field(default_factory=list)


@dataclass
class CsField:
    name: str
    type: str
    modifiers: str
    attributes: str
    line: int
    serialized: bool = False  # [SerializeField] 或 public 字段(会被 prefab 引用)
    code_used: bool = False   # 声明之外还在类体里出现过(读/写),死字段检测用


@dataclass
class CsProperty:
    """C# 属性。只用于「这个类型对外有哪些 API」的可发现性。

    属性访问(`x.Prop`)在 C# 里**不是调用边**,不产生 calls 记录 ——
    所以属性不会有 impact/refs 的调用方,这是语言事实而不是图谱缺陷。
    serialized 恒为 False:Unity 序列化的是编译器生成的 backing field,
    名字是 `<Hp>k__BackingField`,与属性名不同,图谱也不该把它算作属性被序列化。
    """
    name: str
    type: str
    modifiers: str
    attributes: str
    line: int
    is_expression: bool = False   # 表达式体 `X => expr`(无访问器块)


@dataclass
class CsEvent:
    """C# 事件。与属性一样,只服务于「这个类型对外有哪些 API」。

    订阅点(`X += Handler`)是一条 method_ref 边,记录的是 **Handler**;
    事件名本身永不出现在 calls 里 —— 所以 event 同样"没有调用方",
    这是语言事实,不是图谱漏了。
    is_custom 标记自定义 add/remove 访问器(非编译器生成的字段式事件)。
    """
    name: str
    type: str
    modifiers: str
    attributes: str
    line: int
    is_custom: bool = False


@dataclass
class CsType:
    name: str
    kind: str            # class / struct / interface / enum / record
    namespace: str
    modifiers: str
    attributes: str
    bases: List[str]
    line: int
    body_start: Optional[int] = None
    body_end: Optional[int] = None
    methods: List[CsMethod] = field(default_factory=list)
    fields: List[CsField] = field(default_factory=list)
    properties: List[CsProperty] = field(default_factory=list)
    events: List[CsEvent] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.namespace}.{self.name}" if self.namespace else self.name

    @property
    def is_mono_behaviour(self) -> bool:
        return any(b in ("MonoBehaviour", "NetworkBehaviour") or
                   b.endswith("Behaviour") for b in self.bases)

    @property
    def is_scriptable_object(self) -> bool:
        return "ScriptableObject" in self.bases


@dataclass
class CsFile:
    path: str
    types: List[CsType] = field(default_factory=list)
    usings: List[str] = field(default_factory=list)
    dotted_members: List[str] = field(default_factory=list)  # 被 `x.Name` 访问过的名字


def _parse_bases(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [re.sub(r"<.*>", "", p).split(".")[-1].strip() for p in parts if p.strip()]


def _method_flag(name: str) -> Tuple[bool, bool]:
    lifecycle = name in UNITY_LIFECYCLE
    message = lifecycle or any(name.startswith(p) for p in UNITY_MESSAGE_PREFIXES)
    return lifecycle, message


def _local_types(body: str) -> Dict[str, str]:
    """方法体内的局部变量 → 类型短名。"""
    out: Dict[str, str] = {}
    for rx in (LOCAL_NEW_RE, LOCAL_GENERIC_RE):
        for m in rx.finditer(body):
            out[m.group(1)] = short_type(m.group(2))
    for m in LOCAL_DECL_RE.finditer(body):
        out.setdefault(m.group(2), short_type(m.group(1)))
    for m in FOREACH_RE.finditer(body):
        out.setdefault(m.group(2), short_type(m.group(1)))
    return out


def _extract_calls(body: str, base_pos: int, line_of, scope: Dict[str, str],
                   own_type: str, raw_body: str = None) -> List[CsCall]:
    """从方法体抽取调用边。scope = 变量名 → 类型短名(字段 + 参数 + 局部变量)。

    body 是去掉注释/字符串后的文本;raw_body 是同一段的原文(长度一致,偏移可通用)。
    SendMessage("Foo") 这类字符串耦合必须在原文上抓 —— body 里引号已经被抹平了。
    """
    calls: List[CsCall] = []

    def add(kind, name, recv, recv_type, arg, pos):
        calls.append(CsCall(kind=kind, name=name, recv=recv, recv_type=recv_type,
                            arg=arg, line=line_of(base_pos + pos)))

    for m in GENERIC_CALL_RE.finditer(body):
        gen = short_type(m.group(2))
        add("api_generic", gen, m.group(1), gen, m.group(2).strip(), m.start())
    for m in STRING_CALL_RE.finditer(raw_body if raw_body is not None else body):
        add("api_string", m.group(1), "", "", m.group(2), m.start())
    chain_spans = []
    for m in CHAIN_CALL_RE.finditer(body):
        owner, field, op = m.group(1), m.group(2), m.group(3)
        chain_spans.append((m.start(), m.end()))
        # 大写开头视为类型(静态字段 / 单例),任意末段方法都记,
        # 不写死 AddListener/Dispatch 这类业务 API。
        if owner not in CS_KEYWORDS and owner[:1].isupper() and op not in CS_KEYWORDS:
            add("field_call", field, owner, owner, op, m.start())
    for m in DOTCALL_RE.finditer(body):
        if any(s <= m.start() < e for s, e in chain_spans):
            continue
        recv, meth = m.group(1), m.group(2)
        if meth in CS_KEYWORDS:
            continue
        if recv in ("this", "base"):
            add("call", meth, recv, own_type, meth, m.start())
            continue
        rt = scope.get(recv, "")
        if not rt and recv not in BUILTIN_RECEIVERS and recv[:1].isupper():
            rt = recv          # 大写开头且不是已知变量 → 静态调用,接收者即类型
        if recv in BUILTIN_RECEIVERS:
            rt = ""            # 引擎内建,不参与项目内类型解析
        add("dotcall", meth, recv, rt, meth, m.start())
    for m in CALL_RE.finditer(body):
        name = m.group(1)
        if name in CS_KEYWORDS or name in ("get", "set"):
            continue
        add("call", name, "", own_type, name, m.start())
    for m in NEW_RE.finditer(body):
        add("new", m.group(1), "", m.group(1), m.group(1), m.start())
    # 方法组引用(没有括号的回调注册)。大写开头 + 不是已知变量/内建,
    # 命中枚举值或常量时最坏只是多一条 low confidence 边(默认被过滤)。
    for rx in (DELEGATE_ARG_RE, DELEGATE_ASSIGN_RE):
        for m in rx.finditer(body):
            name = m.group(1)
            if name in CS_KEYWORDS or name in BUILTIN_RECEIVERS or name in scope:
                continue
            add("method_ref", name, "", own_type, name, m.start(1))
    return calls


def parse_csharp(text: str, path: str = "") -> CsFile:
    """解析一个 .cs 文件。"""
    result = CsFile(path=path)
    result.usings = USING_RE.findall(text)
    clean = _strip_comments_and_strings(text)
    result.dotted_members = sorted(set(DOTTED_MEMBER_RE.findall(clean)))
    line_starts = [0]
    for m in re.finditer("\n", text):
        line_starts.append(m.end())

    def line_of(pos: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    ns_matches = list(NS_RE.finditer(clean))
    braces = _brace_pairs(clean)  # 全文件一次扫描,后续 O(1) 查询
    # 命名空间按出现位置近似归属:取声明位置之前最后一个 namespace
    for m in CLASS_RE.finditer(clean):
        ns = ""
        for n in ns_matches:
            if n.start() < m.start():
                ns = n.group(1)
            else:
                break
        # 结尾可能是 `{`(有体)或 `;`(positional record,没有体)。
        # 绝不能把 `;` 的位置当成 body_start —— 那样 _enclosing_class 的区间判断
        # 会把它后面的所有成员都算到这个类型名下。
        if m.group(0).endswith("{"):
            body_start = m.end() - 1  # '{' 的位置
            body_end = braces.get(body_start, len(clean))
        else:
            body_start = body_end = None
        t = CsType(
            name=m.group(4), kind=m.group(3), namespace=ns,
            modifiers=" ".join(m.group(2).split()),
            attributes=" ".join(m.group(1).split()),
            bases=_parse_bases(m.group(6)),   # 组 6:组 5 让给了 positional 参数表
            line=line_of(m.start()),
            body_start=body_start,
            body_end=body_end,
        )
        # positional record 的参数就是编译期生成的 init-only 属性,收成属性。
        # 类型经 _parse_params 归一为短名(会丢泛型实参),与接收者推断共用同一口径。
        if m.group(3) == "record" and m.group(5):
            for pname, ptype in _parse_params(m.group(5)).items():
                t.properties.append(CsProperty(
                    name=pname, type=ptype, modifiers="public",
                    attributes="", line=line_of(m.start()),
                ))
        result.types.append(t)

    # 委托类型。CLASS_RE 认不出 `delegate`,所以旧版整个委托类型都不在图里 ——
    # 放在这里(类型区)而不是成员区,是为了让后面的 field_scopes 覆盖到它,
    # 避免方法解析时 field_scopes[id(owner)] 取空。delegate 没有体,body 留空。
    for m in DELEGATE_RE.finditer(clean):
        dname = m.group(4)
        if dname in CS_KEYWORDS:
            continue
        ns = ""
        for n in ns_matches:
            if n.start() < m.start():
                ns = n.group(1)
            else:
                break
        result.types.append(CsType(
            name=dname, kind="delegate", namespace=ns,
            modifiers=" ".join(m.group(2).split()),
            attributes=" ".join(m.group(1).split()),
            bases=[], line=line_of(m.start()),
            body_start=None, body_end=None,
        ))

    # 字段先行:方法体里的 `field.Method()` 需要字段声明类型才能定向
    for m in FIELD_RE.finditer(clean):
        owner = _enclosing_class(m.start(), result.types)
        if owner is None:
            continue
        ftype, fname = m.group(3), m.group(4)
        if ftype in CS_KEYWORDS or fname in CS_KEYWORDS:
            continue
        mods = " ".join(m.group(2).split())
        attrs = " ".join(m.group(1).split())
        serialized = ("SerializeField" in attrs) or ("public" in mods and
                      "const" not in mods and "static" not in mods)
        owner.fields.append(CsField(
            name=fname, type=ftype, modifiers=mods, attributes=attrs,
            line=line_of(m.start()), serialized=serialized,
        ))

    # 属性。只登记「类型对外有哪些 API」,不抽 getter/setter 里的调用 ——
    # 属性访问不是调用边,而 getter 体里的调用要另开一套入口(见 README 局限)。
    for m in PROPERTY_RE.finditer(clean):
        owner = _enclosing_class(m.start(), result.types)
        if owner is None:
            continue
        ptype, pname = m.group(3), m.group(4)
        if ptype in CS_KEYWORDS or pname in CS_KEYWORDS:
            continue          # get/set 已在 CS_KEYWORDS 里,单独补 init
        if pname in ("init", "add", "remove", "value"):
            continue
        owner.properties.append(CsProperty(
            name=pname, type=ptype,
            modifiers=" ".join(m.group(2).split()),
            attributes=" ".join(m.group(1).split()),
            line=line_of(m.start()),
            is_expression=m.group(0).rstrip().endswith("=>"),
        ))

    # 事件。同样只登记可发现性,不抽 add/remove 访问器里的调用。
    for m in EVENT_RE.finditer(clean):
        owner = _enclosing_class(m.start(), result.types)
        if owner is None:
            continue
        ename = m.group(4)
        if ename in CS_KEYWORDS:
            continue
        owner.events.append(CsEvent(
            name=ename, type=m.group(3),
            modifiers=" ".join(m.group(2).split()),
            attributes=" ".join(m.group(1).split()),
            line=line_of(m.start()),
            # 字段式事件以 `;` 收尾;自定义访问器式只匹配到 `{ add/remove` 就结束了
            # (group(0) 不含收尾的 `}`),所以判据是「不是分号结尾」。
            is_custom=not m.group(0).rstrip().endswith(";"),
        ))

    field_scopes = {id(t): {f.name: short_type(f.type) for f in t.fields}
                    for t in result.types}

    # 字段是否在类体里被读写:调用边只记方法调用,`count++` / `hp = 0` 这类
    # 纯字段访问图上看不见,不补这一步 dead-field 会几千条假阳性。
    for t in result.types:
        if not t.fields or t.body_start is None:
            continue
        body = clean[t.body_start:(t.body_end or len(clean)) + 1]
        seen = Counter(re.findall(r"[A-Za-z_]\w*", body))
        for f in t.fields:
            f.code_used = seen.get(f.name, 0) > 1

    def body_of(match_end: int):
        """返回 (方法体文本, 起始绝对偏移);表达式体方法返回 (None, 0)。"""
        brace = clean.find("{", match_end - 1, min(len(clean), match_end + 200))
        if brace == -1 or "=>" in clean[match_end - 1:brace]:
            return None, 0
        end = braces.get(brace, len(clean))
        return clean[brace:end + 1], brace

    # 方法归属到最近的包含类型
    for m in METHOD_RE.finditer(clean):
        owner = _enclosing_class(m.start(), result.types)
        if owner is None:
            continue
        name = m.group(4)
        if name in CS_KEYWORDS:
            continue
        lifecycle, message = _method_flag(name)
        ret = " ".join(m.group(3).split()) or "void"
        mods = " ".join(m.group(2).split())
        params = " ".join(m.group(5).split())
        meth = CsMethod(
            name=name,
            signature=f"{name}({params})",
            modifiers=mods, return_type=ret,
            attributes=" ".join(m.group(1).split()),
            line=line_of(m.start()),
            params=_parse_params(m.group(5)),
            is_lifecycle=lifecycle, is_message=message,
        )
        body, base_pos = body_of(m.end())
        if body is not None:
            scope = dict(field_scopes[id(owner)])
            scope.update(meth.params)
            scope.update(_local_types(body))
            meth.calls = _extract_calls(body, base_pos, line_of, scope,
                                        owner.name,
                                        text[base_pos:base_pos + len(body)])
        owner.methods.append(meth)

    # 补充:无修饰符方法(void Update() 这种 Unity 常见写法),按 (owner,name) 去重
    seen_methods = {(t.name, meth.name) for t in result.types for meth in t.methods}
    for m in METHOD_RE2.finditer(clean):
        owner = _enclosing_class(m.start(), result.types)
        if owner is None:
            continue
        ret_raw, name = m.group(2), m.group(3)
        if name in CS_KEYWORDS or (ret_raw in CS_KEYWORDS and ret_raw != "void"):
            continue
        if ret_raw == name:  # 构造函数
            continue
        if (owner.name, name) in seen_methods:
            continue
        seen_methods.add((owner.name, name))
        lifecycle, message = _method_flag(name)
        params = " ".join(m.group(4).split())
        meth = CsMethod(
            name=name,
            signature=f"{name}({params})",
            modifiers="private",  # C# 缺省可见性
            return_type=" ".join(ret_raw.split()),
            attributes=" ".join(m.group(1).split()),
            line=line_of(m.start()),
            params=_parse_params(m.group(4)),
            is_lifecycle=lifecycle, is_message=message,
        )
        body, base_pos = body_of(m.end())
        if body is not None:
            scope = dict(field_scopes[id(owner)])
            scope.update(meth.params)
            scope.update(_local_types(body))
            meth.calls = _extract_calls(body, base_pos, line_of, scope,
                                        owner.name,
                                        text[base_pos:base_pos + len(body)])
        owner.methods.append(meth)

    return result
