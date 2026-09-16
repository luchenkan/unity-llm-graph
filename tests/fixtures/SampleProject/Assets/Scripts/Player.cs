using UnityEngine;

namespace Game.Combat
{
    public class Player : MonoBehaviour
    {
        [SerializeField] private Enemy targetEnemy;

        // 属性夹具(0.10.0):C# 属性不是字段,图谱要分开收。
        // 四种写法各一,任何一条漏了都会让 find 搜不到名字 / Type.Prop 解析成 unknown。
        public int Hp { get; private set; }             // 自动属性
        public bool IsAlive { get { return Hp > 0; } }  // 访问器体
        public string DisplayName => "Player" + Hp;     // 表达式体
        public static int MaxHp { get; set; }           // 静态属性

        public void Attack()
        {
            targetEnemy.TakeDamage(25);
        }

        private void Awake()
        {
            RegisterHandler(OnHpChanged);
            BuildIndex(new System.Collections.Generic.List<string>());
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
