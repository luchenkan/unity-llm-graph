namespace Game.Util
{
    // 精度回归夹具:Decoy 和 Enemy 都声明了 Refresh(),
    // 只按方法名匹配的实现会把 Decoy.Poke 算成 Enemy 的依赖(假阳性)。
    public class Decoy
    {
        public void Refresh()
        {
        }

        public void Poke()
        {
            Decoy d = new Decoy();
            d.Refresh();
        }
    }
}
