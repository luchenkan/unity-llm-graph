# 调用边语义（置信度与 kind）

> 从 README 拆出。`low` 默认被过滤，要看全部传 `include_low=true` / `--include-low`。

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
