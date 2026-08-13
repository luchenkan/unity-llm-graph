public class BusListener
{
    void Start()
    {
        EventHub.ScoreChanged.AddListener(OnItem);
        EventHub.ScoreChanged.Dispatch();
    }

    private void OnItem()
    {
    }
}
