using UnityEngine;

namespace Game.Combat
{
    // delegate 是**类型声明**,不是成员。旧 CLASS_RE 只认 class/struct/interface/
    // enum/record,整个委托类型都不进图 —— 实测某商业项目 58 条。
    public delegate void PlayerEventHandler(int hp);

    // positional record:参数会被编译成 init-only 属性。
    // 旧 CLASS_RE 要求结尾是 `{`,这种以 `;` 收尾的写法整条类型都丢了。
    public record DamageInfo(int Amount, string Source);

    public class Player : MonoBehaviour
    {
        [SerializeField] private Enemy targetEnemy;

        // 属性夹具(0.10.0):C# 属性不是字段,图谱要分开收。
        // 四种写法各一,任何一条漏了都会让 find 搜不到名字 / Type.Prop 解析成 unknown。
        public int Hp { get; private set; }             // 自动属性
        public bool IsAlive { get { return Hp > 0; } }  // 访问器体
        public string DisplayName => "Player" + Hp;     // 表达式体
        public static int MaxHp { get; set; }           // 静态属性

        // 事件夹具(0.11.0):event 既不是字段也不是属性,旧版一条都收不到。
        public event System.Action<Player> OnDied;      // 字段式
        public static event System.Action OnLevelUp;    // 静态
        public event System.Action<int> OnDamaged       // 自定义访问器式
        {
            add { }
            remove { }
        }

        public void Attack()
        {
            targetEnemy.TakeDamage(25);
        }

        private void Awake()
        {
            RegisterHandler(OnHpChanged);
            BuildIndex(new System.Collections.Generic.List<string>());
            Notify(targetEnemy);
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
