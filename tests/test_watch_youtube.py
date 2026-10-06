"""Screen watching and YouTube, without a screen or the internet."""
import pytest


def test_watch_reply_parsing():
    from jeeves.daemon.watcher import parse
    assert parse("SEEN: a boss fight\nSAY: Watch the left!") == ("a boss fight", "Watch the left!")
    assert parse("SEEN: inventory screen\nSAY: PASS") == ("inventory screen", "")
    assert parse("SEEN: menu\nSAY: (pass)")[1] == ""


def test_change_detection_sees_small_text_changes():
    from jeeves.daemon.watcher import CHANGED, difference
    a = bytes([200] * 20736)
    b = bytearray(a)
    for i in range(20):                          # ~two digits changing on a big screen
        b[5000 + i] = 40
    assert difference(a, a) == 0 and difference(a, bytes(b)) > CHANGED
    assert difference(None, a) == 1.0


@pytest.mark.parametrize("text,args", [
    ("Jeeves, watch my screen", {"action": "start", "focus": ""}),
    ("Jeeves, watch my screen and tell me when the render finishes", {"action": "start",
                                                                      "focus": "when the render finishes"}),
    ("Jeeves, commentate my game", {"action": "start"}),
    ("Jeeves, stop watching", {"action": "stop"}),
])
def test_watch_routing(engine, text, args):
    d = engine.intent.decide(dict(engine.agents()["jeeves"], id="jeeves"), text)
    assert d.function == "watch_screen" and all(d.args.get(k) == v for k, v in args.items())


@pytest.mark.parametrize("text,args", [
    ("Jeeves, pull up the newest video from moist critikal", {"channel": "moist critikal", "newest": True}),
    ("Jeeves, play lofi hip hop on youtube", {"query": "lofi hip hop"}),
    ("Jeeves, pause the video", {"action": "pause"}),
    ("Jeeves, close the video", {"action": "stop"}),
])
def test_youtube_routing(engine, text, args):
    d = engine.intent.decide(dict(engine.agents()["jeeves"], id="jeeves"), text)
    assert d.function == "youtube" and all(d.args.get(k) == v for k, v in args.items())


def test_skip_only_routes_to_the_video_while_one_plays(engine):
    agent = dict(engine.agents()["jeeves"], id="jeeves")
    assert engine.intent.decide(agent, "Jeeves, skip ahead 30 seconds").function != "youtube"
    engine.video_state["playing"] = True
    d = engine.intent.decide(agent, "Jeeves, skip ahead 30 seconds")
    assert d.function == "youtube" and d.args == {"action": "forward", "seconds": 30.0}


def fake_ytdlp(monkeypatch):
    from jeeves.functions.partials import youtube as yt
    calls = []

    def run(*args, timeout=60):
        calls.append(args)
        target = args[-1]
        if target.startswith("ytsearch"):
            return {"entries": [
                {"id": "a1", "title": "Moist Meter: Some Movie", "channel": "penguinz0", "channel_id": "UCq6V"},
                {"id": "b2", "title": "Reacting to Moist Critikal", "channel": "Random Fan", "channel_id": "UCfan"},
                {"id": "c3", "title": "Moist Critikal Podcast", "channel": "Moist Critikal", "channel_id": "UCmc"}]}
        if "/channel/UCmc/videos" in target:
            return {"channel": "Moist Critikal", "entries": [
                {"id": "new1", "title": "This Game Is Broken"}, {"id": "old2", "title": "Elden Ring DLC Is Hard"}]}
        if target.startswith("https://www.youtube.com/watch"):
            return {"title": "This Game Is Broken", "channel": "Moist Critikal", "webpage_url": target,
                    "requested_formats": [{"url": "https://v.example/1080.mp4", "vcodec": "avc1", "acodec": "none"},
                                          {"url": "https://a.example/a.m4a", "vcodec": "none", "acodec": "mp4a"}]}
        raise AssertionError(target)
    monkeypatch.setattr(yt, "_ytdlp", run)
    return calls


def test_newest_video_from_a_channel(monkeypatch):
    from jeeves.functions.partials import youtube as yt
    fake_ytdlp(monkeypatch)
    assert yt.find_channel("moist critikal") == ("UCmc", "Moist Critikal")
    assert yt.pick(channel="moist critikal", newest=True)["id"] == "new1"
    assert yt.pick(query="elden ring", channel="moist critikal")["id"] == "old2"    # topic within the channel
    s = yt.streams("https://www.youtube.com/watch?v=new1")
    assert s["video"].endswith("1080.mp4") and s["audio"].endswith("a.m4a")


def test_youtube_function_opens_the_player(engine, monkeypatch):
    fake_ytdlp(monkeypatch)
    events, said = [], []
    engine.publish = lambda t, d: events.append((t, d))
    engine.speak = lambda ctx, t: said.append(t)
    engine.handle_text("Jeeves, pull up the newest video from moist critikal", "jeeves", wait=True)
    video = next(d for t, d in events if t == "video")
    assert video["action"] == "play" and video["title"] == "This Game Is Broken" and video["audio"]
    assert said == ["Here's This Game Is Broken from Moist Critikal."] and engine.video_state["playing"]
