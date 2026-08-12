using UnityEngine;
using UnityEngine.UI;

namespace Game.Combat
{
    public class Enemy : MonoBehaviour
    {
        [SerializeField] private Image healthBar;
        public int hp = 100;
        // prefab 里没赋值、代码里也没读写:真正的死字段
        [SerializeField] private int neverTouched;

        void Start()
        {
            hp = 100;
        }

        private void Update()
        {
        }

        public void TakeDamage(int amount)
        {
            hp -= amount;
            if (hp <= 0)
            {
                Die();
            }
        }

        private void Die()
        {
            Destroy(gameObject);
        }

        // 与 Game.Util.Decoy.Refresh() 同名:用来验证调用边不是按名字瞎匹配的
        public void Refresh()
        {
        }

        // 只在 prefab 的 m_OnClick 里被指名调用,代码里没有任何调用点
        private void OnButtonClicked()
        {
        }

        private void UnusedPrivateMethod()
        {
        }
    }
}
