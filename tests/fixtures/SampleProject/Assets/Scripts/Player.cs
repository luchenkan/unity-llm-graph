using UnityEngine;

namespace Game.Combat
{
    // delegate 是**类型声明**,不是成员。旧 CLASS_RE 只认 class/struct/interface/
    // enum/record,整个委托类型都不进图 —— 实测某商业项目 58 条。
    public delegate void PlayerEventHandler(int hp);

    // positional record:参数会被编译成 init-only 属性。
    // 旧 CLASS_RE 要求结尾是 `{`,这种以 `;` 收尾的写法整条类型都丢了。
    public record DamageInfo(int Amount, string Source);

    // `record struct` / `record class`:旧 CLASS_RE 里裸 `record` 分支先命中,
    // 名字被解析成关键字 `struct`,整条类型丢失。
    public readonly record struct HitResult(int Damage, bool Critical);

    // 显式接口实现的目标。接口成员没有修饰符,PROPERTY_RE 必须允许修饰符为空。
    public interface IReadOnlyHp
    {
        int Count { get; }
    }

    // `ref` 返回属性(UniTask 的 ITaskPoolNode 就是这个写法)。
    // PROPERTY_RE 的修饰符表漏了 `ref`,`ref` 会被当成类型名、`T` 当成属性名,
    // 后面对不上访问器,整条丢弃(实测某商业项目 29 条)。
    public interface ITaskNode<T>
    {
        ref T NextNode { get; }
    }

    // 嵌套类型重名夹具:和 Player.Phase 同名,只有 full_name 带上外层类名才区分得开。
    // 否则两个枚举塌成同一个 full_name,成员互相混进对方名下。
    public class Sidekick
    {
        public enum Phase { Idle }
    }

    public class Player : MonoBehaviour, IReadOnlyHp
    {
        [SerializeField] private Enemy targetEnemy;

        // 属性夹具(0.10.0):C# 属性不是字段,图谱要分开收。
        // 四种写法各一,任何一条漏了都会让 find 搜不到名字 / Type.Prop 解析成 unknown。
        public int Hp { get; private set; }             // 自动属性
        public bool IsAlive { get { return Hp > 0; } }  // 访问器体
        public string DisplayName => "Player" + Hp;     // 表达式体
        public static int MaxHp { get; set; }           // 静态属性

        // 显式接口实现:名字取最后一段(`IReadOnlyHp.Count` → `Count`)
        int IReadOnlyHp.Count => Hp;

        // 三参泛型:旧写法「类型名最多一次空格分隔」匹配不到 `<int, string, bool>`,
        // 整条声明丢失(实测某商业项目 property 2 条 / event 11 条)。
        public System.Action<int, string, bool> OnBigAction { get; set; }

        // 泛型闭合后还有 `.成员`:`Dictionary<K,V>.ValueCollection`。
        // 类型片段只到 `>` 为止的话,`.ValueCollection` 会被当成名字的一部分,
        // 匹配不上后面的 `=>`,整条丢弃(实测某商业项目 2 条)。
        public System.Collections.Generic.Dictionary<string, int>.ValueCollection Slots => null;

        // 嵌套枚举。和 Sidekick.Phase 重名。
        public enum Phase { Idle, Fighting }

        // 事件夹具(0.11.0):event 既不是字段也不是属性,旧版一条都收不到。
        public event System.Action<Player> OnDied;      // 字段式
        public static event System.Action OnLevelUp;    // 静态
        public event System.Action<int, string, bool> OnTriple;   // 三参泛型
        public event System.Action OnReady = delegate { };        // 带初始化器
        public event System.Action OnOpen, OnClose;               // 多声明符
        public event System.Action<int> OnDamaged       // 自定义访问器式
        {
            add { }
            remove { }
        }

        // 属性参与接收者推断:旧版 field_scopes 只收字段,`Rival.TakeDamage()` 里的
        // Rival 查不到类型,只能靠「大写开头 = 静态调用」兜底,推成不存在的 Rival 类型。
        public Enemy Rival { get; private set; }

        public void Attack()
        {
            targetEnemy.TakeDamage(25);
        }

        private void Strike()
        {
            Rival.TakeDamage(7);
        }

        // switch 表达式的类型模式臂长得和属性声明一模一样:行首、缩进、
        // 「类型 名字 =>」。行首锚定和类型名约束都挡不住,只有花括号深度能。
        // 实测某商业项目 5 条幻影属性由此而来(其中 3 条把内部类名注册成了公开属性)。
        private string Describe(object arg)
        {
            return arg switch
            {
                Enemy phantomEnemy => "enemy",
                DamageInfo phantomInfo => "damage",
                _ => "unknown"
            };
        }

        // switch 表达式同样出现在**属性的表达式体**里 —— 所以不能用「落在方法体跨度内
        // 就丢弃」来过滤,那样这里的幻影会漏网。
        public string PhaseName => Rival switch
        {
            Enemy phantomRival => "rival",
            _ => "none"
        };

        private void Awake()
        {
            RegisterHandler(OnHpChanged);
            BuildIndex(new System.Collections.Generic.List<string>());
            Notify(targetEnemy);
            Strike();
            Describe(null);
        }

        // 空条件调用夹具:旧 DOTCALL_RE 因为 `.` 前多了个 `?` 把所有 `x?.Foo()`
        // 都漏掉了(实测某项目 1772 条 / 555 个文件),连带把只被这样调用的方法
        // 误报成死代码。
        private void Notify(Enemy target)
        {
            OnDied?.Invoke(this);
            target?.TakeDamage(1);
        }

        // 属性正则的误报回归:LINQ lambda 的 `x => x, x => ...` 长得就像
        // 「类型 + 名字 + =>」,只有行首锚定能挡住它。这里必须保持在行中间。
        private void BuildIndex(System.Collections.Generic.List<string> names)
        {
            var index = names.ToDictionary(x => x, x => x + "-hit");
        }

        private void RegisterHandler(System.Action<int> cb)
        {
        }

        private void OnHpChanged(int hp)
        {
        }

        public void Heal(int amount)
        {
        }

        private void OnTriggerEnter(Collider other)
        {
            SendMessage("OnPlayerTouched", other, SendMessageOptions.DontRequireReceiver);
        }

        public void OnPlayerTouched(object sender)
        {
        }
    }
}
