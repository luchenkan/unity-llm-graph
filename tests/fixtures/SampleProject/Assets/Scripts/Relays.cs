public class EventChannel
{
    internal void AddListener(System.Action cb) { }
    public void RemoveListener(System.Action cb) { }
    public void Dispatch() { }
}

public class EventHub
{
    public static EventChannel ScoreChanged = new EventChannel();
    public static EventChannel UnusedChannel = new EventChannel();
}
