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


def fake_youtube(monkeypatch, handles=None, channel_results=None, video_results=None, uploads=None, inside=None):
    from jeeves.functions.partials import youtube as yt
    calls = []

    def run(*args, timeout=60):
        calls.append(args[-1])
        t = args[-1]
        if "/@" in t:
            h = t.split("/@")[1].split("/")[0]
            if h in (handles or {}):
                return handles[h]
            raise yt.FunctionError("no such handle")
        if "sp=EgIQAg" in t:
            return {"entries": channel_results or []}
        if t.startswith("ytsearch"):
            return {"entries": video_results or []}
        if "/search?query=" in t:
            cid = t.split("/channel/")[1].split("/")[0]
            return {"entries": (inside or {}).get(cid, [])}
        if t.endswith("/videos"):
            cid = t.split("/channel/")[1].split("/")[0]
            return {"entries": (uploads or {}).get(cid, [])}
        raise AssertionError(t)
    monkeypatch.setattr(yt, "_ytdlp", run)
    return calls


def test_newest_video_uses_the_exact_channel_not_the_top_search_hit(monkeypatch):
    from jeeves.functions.partials import youtube as yt
    fake_youtube(monkeypatch,
                 handles={"alexbale": {"channel": "Alex Bale", "channel_id": "UCab", "entries": [{"id": "x"}]}},
                 video_results=[{"id": "r1", "title": "Alex Bale reacts", "channel": "Fan Channel", "channel_id": "UCfan"}],
                 uploads={"UCab": [{"id": "newest", "title": "Brand new video"}, {"id": "old", "title": "Old one"}]})
    assert yt.find_channel("alex bale") == ("UCab", "Alex Bale")
    assert yt.pick(channel="alex bale", newest=True)["id"] == "newest"


def test_described_video_is_found_inside_the_named_channel(monkeypatch):
    from jeeves.functions.partials import youtube as yt
    fake_youtube(monkeypatch,
                 handles={"alexbale": {"channel": "Alex Bale", "channel_id": "UCab", "entries": []}},
                 video_results=[{"id": "pixar", "title": "The Pixar Theory Explained", "channel": "Random Guy",
                                 "channel_id": "UCr"}],
                 uploads={"UCab": [{"id": "n1", "title": "Something else"}]},
                 inside={"UCab": [{"id": "sb", "title": "I Combined Every SpongeBob Conspiracy Theory Into One"},
                                  {"id": "x", "title": "Ranking Cartoon Villains"}]})
    args = yt.parse_request("pull up the alex bale video that combines all the spongebob conspiracy theories into one")
    assert args["channel"] == "alex bale" and "spongebob" in args["query"]
    assert yt.pick(args["query"], args["channel"], args["newest"])["id"] == "sb"
    # even when the intent model passes it all as one query
    assert yt.pick("alex bale video that combines all the spongebob conspiracy theories into one")["id"] == "sb"


def test_search_ranks_by_how_well_the_title_fits(monkeypatch):
    from jeeves.functions.partials import youtube as yt
    fake_youtube(monkeypatch, video_results=[
        {"id": "a", "title": "Pixar Conspiracy Theories", "channel": "Some Guy", "channel_id": "UC1"},
        {"id": "b", "title": "Every SpongeBob Conspiracy Theory Combined", "channel": "Other", "channel_id": "UC2"}])
    assert yt.pick("spongebob conspiracy theories combined")["id"] == "b"


def test_streams_carry_qualities_chapters_and_fps(monkeypatch):
    from jeeves.functions.partials import youtube as yt
    monkeypatch.setattr(yt, "_ytdlp", lambda *a, timeout=60: {
        "title": "T", "channel": "C", "webpage_url": "https://www.youtube.com/watch?v=x",
        "formats": [{"height": 1080, "vcodec": "avc1"}, {"height": 720, "vcodec": "avc1"},
                    {"height": 360, "vcodec": "vp9"}, {"vcodec": "none", "acodec": "mp4a"}],
        "requested_formats": [{"url": "https://v/1080", "vcodec": "avc1", "height": 1080, "fps": 60},
                              {"url": "https://a/a", "vcodec": "none", "acodec": "mp4a"}],
        "chapters": [{"start_time": 0, "end_time": 60, "title": "Intro"},
                     {"start_time": 60, "end_time": 300, "title": "The theory"}]})
    s = yt.streams("https://www.youtube.com/watch?v=x")
    assert s["heights"] == [1080, 720, 360] and s["height"] == 1080 and s["fps"] == 60
    assert [c["title"] for c in s["chapters"]] == ["Intro", "The theory"]


@pytest.mark.parametrize("text,action", [("next chapter", "next_chapter"), ("go to the previous chapter",
                                                                          "previous_chapter"),
                                         ("speed it up", "faster"), ("slow down", "slower")])
def test_player_voice_controls_while_playing(engine, text, action):
    engine.video_state["playing"] = True
    d = engine.intent.decide(dict(engine.agents()["jeeves"], id="jeeves"), f"Jeeves, {text}")
    assert d.function == "youtube" and d.args == {"action": action}


def test_player_controls(monkeypatch):
    pytest.importorskip("PySide6.QtMultimedia")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])          # noqa: F841
    from jeeves.video_player import VideoPlayer
    calls = []
    p = VideoPlayer(lambda method, **kw: calls.append((method, kw)))
    p.play({"title": "T", "channel": "C", "video": "file:///dev/null", "audio": None, "page": "https://y/x",
            "fps": 50, "height": 1080, "heights": [1080, 720], "fullscreen": False,
            "chapters": [{"start": 0, "title": "Intro"}, {"start": 60, "title": "Part 2"}, {"start": 120, "title": "End"}]})
    p.change_speed(1)
    assert p.rate == 1.25 and p.speed_btn.text() == "1.25×"     # (no multimedia backend in a build sandbox:
    p.set_speed(9)                                               #  the player keeps its own speed)
    assert p.rate == 2.0
    assert p.chapter_at(75_000)["title"] == "Part 2" and p.chapter_at(5_000)["title"] == "Intro"
    p.set_quality(720)
    assert calls[-1][0] == "video.quality" and calls[-1][1]["height"] == 720
    p.controls_shown = True
    p.video.pause()
    p.hide_controls()
    p.close()
    assert calls[-1] == ("video.closed", {})
