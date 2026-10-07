"""Custom Sources: sites an agent's research reads first when a question fits them."""
from jeeves.functions import custom_sources as cs

MOVEMENT = {"url": "https://parkour-reborn.fandom.com/wiki/Movement",
            "about": "Parkour Reborn Movement Wiki, for information on any movement techniques"}
KNUCKLE = {"url": "https://www.reddit.com/r/whiteknuckle", "about": "White Knuckle subreddit, player stories and tips"}


def _agent(engine, sites, enabled=True):
    engine.settings.set("agents.jeeves.custom_sources", {"enabled": enabled, "sites": sites})
    return dict(engine.agents()["jeeves"], id="jeeves")


def _ctx(engine, agent, text="x"):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    return FunctionContext(engine, "jeeves", agent, new_entry(text, "jeeves", "text"))


def test_lines_are_read_as_address_and_what_its_for():
    text = ("https://parkour-reborn.fandom.com/wiki/Movement = Parkour Reborn Movement Wiki, for information on any "
            "movement techniques\nreddit.com/r/whiteknuckle - White Knuckle subreddit\n"
            "White Knuckle wiki = https://white-knuckle.wiki.gg\n# a note\nno address here")
    sites, problems = cs.parse(text)
    assert sites == [MOVEMENT, {"url": "https://reddit.com/r/whiteknuckle", "about": "White Knuckle subreddit"},
                     {"url": "https://white-knuckle.wiki.gg", "about": "White Knuckle wiki"}]
    assert problems == ["line 5 has no web address"]
    assert cs.parse(cs.to_text(sites))[0] == sites


def test_the_sites_that_fit_are_picked_by_their_words_or_by_the_model(engine, monkeypatch):
    agent = _agent(engine, [KNUCKLE, MOVEMENT])
    asked = []
    monkeypatch.setattr(engine.models, "respond", lambda agent, prompt, **kw: asked.append(prompt) or "2")
    plan = {"goal": "How do I do a wumpy in Parkour Reborn?", "context": ["Parkour Reborn"], "terms": ["wumpy"]}
    assert cs.pick(_ctx(engine, agent), "how do I do a wumpy in parkour reborn", plan) == [MOVEMENT]
    assert not asked                                       # its words gave it away: no model needed
    plan = {"goal": "How do I wallhop?", "context": [], "terms": ["wallhop"]}
    assert cs.pick(_ctx(engine, agent), "how do I wallhop", plan) == [MOVEMENT]     # the model's pick
    assert "movement techniques" in asked[0]
    assert cs.pick(_ctx(engine, _agent(engine, [MOVEMENT], enabled=False)),
                   "how do I do a wumpy in parkour reborn", {}) == []                  # turned off


def test_research_reads_your_source_first_and_stops_there(engine, monkeypatch):
    from jeeves.functions.partials import web
    agent = _agent(engine, [MOVEMENT])
    searched = []
    monkeypatch.setattr(web, "search", lambda settings, q, n, problems=None: searched.append(q) or [])
    monkeypatch.setattr(web, "wiki_article", lambda base, title: (
        "Movement covers running and vaulting.\n\n" + "Basic moves are listed below.\n\n" * 20 +
        "Wumpy | Wallrun, jump off and wallrun again within 0.5 s to keep your speed.\n\nRoll | Land softly.")
        if (base, title) == ("https://parkour-reborn.fandom.com", "Movement") else "")

    def respond(agent, prompt, **kw):
        if "Break the question down" in prompt:
            return "GOAL: How do I do a wumpy in Parkour Reborn?\nCONTEXT: Parkour Reborn\nTERMS: wumpy"
        if "actually answer the question" in prompt:
            return "ANSWERED: 1" if "within 0.5 s" in prompt else "MORE"
        return "Wallrun, jump off, then wallrun again within half a second [1]."
    monkeypatch.setattr(engine.models, "respond", respond)
    engine.speak = lambda ctx, t: None
    ctx = _ctx(engine, agent, "how do I do a wumpy in parkour reborn")
    ctx.call("research", question="how do I do a wumpy in parkour reborn")
    assert searched == []                                  # no web search needed at all
    src = ctx.entry["sources"]
    assert [s["url"] for s in src] == [MOVEMENT["url"]] and "Wumpy | Wallrun" in src[0]["text"]


def test_research_searches_inside_your_source_when_its_page_doesnt_answer(engine, monkeypatch):
    from jeeves.functions.partials import web
    agent = _agent(engine, [KNUCKLE])
    searched = []
    monkeypatch.setattr(web, "search", lambda settings, q, n, problems=None: searched.append(q) or [])
    monkeypatch.setattr(web, "request_website", lambda ctx, url, **kw: "Welcome to the subreddit. Rules: be nice. " * 9)
    monkeypatch.setattr(web, "find_wiki", lambda subject: None)

    def respond(agent, prompt, **kw):
        if "Break the question down" in prompt:
            return "GOAL: How likely is a first-try White Knuckle win?\nCONTEXT: White Knuckle\nTERMS: NONE"
        if "actually answer the question" in prompt:
            return "MORE"
        if "web search queries" in prompt:
            return "white knuckle first try win rate"
        return "Maybe one in ten, an estimate."
    monkeypatch.setattr(engine.models, "respond", respond)
    engine.speak = lambda ctx, t: None
    q = "what are the chances an average player beats a white knuckle campaign on their first try"
    _ctx(engine, agent, q).call("research", question=q, depth="quick")
    after_context = searched[searched.index("White Knuckle wiki") + 1:]
    assert after_context[0].startswith("site:reddit.com ")     # inside your source before the whole web


def test_a_question_one_of_your_sites_is_for_gets_researched(engine):
    agent = _agent(engine, [MOVEMENT])
    q = "Jeeves, should I learn wumpies or wallhops first in parkour reborn"
    assert engine.intent.decide(agent, q).function == "research"
    plain = dict(agent, custom_sources={"enabled": True, "sites": []})
    assert engine.intent.decide(plain, q).function != "research"
