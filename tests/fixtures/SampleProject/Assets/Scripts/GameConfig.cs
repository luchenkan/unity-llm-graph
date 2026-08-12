using UnityEngine;

namespace Game.Config
{
    [CreateAssetMenu(menuName = "Game/Config")]
    public class GameConfig : ScriptableObject
    {
        public int maxPlayers = 4;
        public string serverUrl = "https://example.com";
    }
}
