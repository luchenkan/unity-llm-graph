using UnityEngine;

namespace Game.Combat
{
    public class Player : MonoBehaviour
    {
        [SerializeField] private Enemy targetEnemy;

        public void Attack()
        {
            targetEnemy.TakeDamage(25);
        }

        private void Awake()
        {
            RegisterHandler(OnHpChanged);
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
