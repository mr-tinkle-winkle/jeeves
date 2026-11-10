"""Smarter and faster lookups: passage ranking, Wikipedia first for plain facts, pages that only list
what a name can mean, and what research keeps for follow-ups."""


def _ctx(engine, text="x"):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    return FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry(text, "jeeves", "text"))


def test_passages_are_ranked_by_the_words_that_tell_them_apart():
    from jeeves.functions.research import best_of, bm25, relevant_passages
    filler = ["Parkour Reborn is a movement game where players run across the rooftops of a large city map."
              f" Section {n} of this page covers the game's history and the updates the game received over time."
              for n in range(40)]
    answer = ("To do a wumpy, jump toward a wall, then press crouch at the moment of contact and hold forward "
              "to keep the speed in Parkour Reborn.")
    page = "\n\n".join(filler[:25] + [answer] + filler[25:])
    got = relevant_passages(page, "How do I do a wumpy in the game Parkour Reborn?", limit=600)
    assert "press crouch" in got                       # the one paragraph with the rare word, not the name
    scores = bm25(["the game, a game and more of the game", "a wumpy is a jump in the game", ""], "wumpy game")
    assert scores[1] > scores[0] > scores[2] == 0
    assert bm25(["he was jumping walls"], "wall jumps")[0] > 0          # endings don't matter
    sources = [{"title": f"Page {n}", "url": f"u{n}", "text": filler[n]} for n in range(6)]
    sources.insert(3, {"title": "Wumpy", "url": "w", "text": answer})
    kept = best_of(sources, "how do i do a wumpy", n=2)
    assert any(s["url"] == "w" for s in kept) and len(kept) == 2


def test_a_plain_fact_is_answered_from_wikipedia_without_searching(engine, monkeypatch):
    from jeeves.functions import research
    article = ("The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. " * 4 +
               "\n\nThe tower is 330 metres tall, about the same height as an 81-storey building.\n\n" +
               "Unrelated paragraph about paint. " * 80)

    class Wiki:
        asked = []

        def available(self):
            return True

        def lookup(self, query, max_chars=6000, about=""):
            self.asked.append(query)
            return {"title": "Eiffel Tower", "text": article[:max_chars]}
    engine.wikipedia = Wiki()
    monkeypatch.setattr(research, "search_many",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("answered: no web search")))
    answers = ["GOAL: How tall is the Eiffel Tower?\nCONTEXT: Eiffel Tower\nTERMS: NONE", "ANSWERED: 1"]
    engine.models.respond = lambda agent, text, **kw: answers.pop(0)
    ctx = _ctx(engine, "how tall is the eiffel tower")
    sources, results, notes = research.run(ctx, "how tall is the eiffel tower")
    assert [s["title"] for s in sources] == ["Wikipedia: Eiffel Tower"] and "330 metres" in sources[0]["text"]
    assert sources[0]["text"].startswith("The Eiffel Tower is a wrought-iron")     # the opening is always kept
    assert Wiki.asked[0] == "Eiffel Tower" and not answers
    assert any(t["kind"] == "wikipedia_first" for t in ctx.entry["trace"])


def test_wikipedia_first_is_only_for_plain_facts_about_the_right_thing(engine, monkeypatch):
    from jeeves.functions import research
    assert research.encyclopedic("who wrote the novel Dune")
    assert not research.encyclopedic("how do I do a wumpy in Parkour Reborn")      # a how-to
    assert not research.encyclopedic("what does the final boss drop")              # a game's inner workings
    assert not research.encyclopedic("what is the latest version of Blender")      # changes
    assert not research.encyclopedic("what are the chances of rain")               # an estimate

    class Wiki:
        def available(self):
            return True

        def lookup(self, query, max_chars=6000, about=""):
            return {"title": "Parkour", "text": "Parkour is an athletic training discipline. " * 30}
    engine.wikipedia = Wiki()
    ctx = _ctx(engine)
    plan = {"goal": "What is Parkour Reborn?", "context": ["Parkour Reborn"], "terms": []}
    assert research.wikipedia_first(ctx, "what is parkour reborn", plan, 4000) is None    # another article


def test_wikipedia_org_is_asked_when_no_copy_is_downloaded(engine, monkeypatch):
    from jeeves.functions import research
    from jeeves.functions.partials import web
    engine.settings.set("research.online_wikipedia", True)
    asked = []
    monkeypatch.setattr(web, "wiki_search", lambda base, q, n=5: asked.append((base, q)) or [
        {"title": "Dune", "url": f"{base}/wiki/Dune", "snippet": ""},
        {"title": "Dune (novel)", "url": f"{base}/wiki/Dune_(novel)", "snippet": ""}])
    monkeypatch.setattr(web, "wiki_article", lambda base, title: (
        "Dune may refer to:\n- Dune, a sand hill\n- Dune (novel)\n- Dune (1984 film)\n- Dune (2021 film)\n"
        if title == "Dune" else "Dune is a 1965 science fiction novel by American author Frank Herbert. " * 5))
    ctx = _ctx(engine)
    plan = {"goal": "Who wrote the novel Dune?", "context": ["Dune"], "terms": []}
    got = research.wikipedia_first(ctx, "who wrote dune", plan, 4000)
    assert got["title"] == "Wikipedia: Dune (novel)" and "Frank Herbert" in got["text"]
    assert got["url"].endswith("/wiki/Dune_(novel)") and asked[0] == ("https://en.wikipedia.org", "Dune")
    engine.settings.set("research.online_wikipedia", False)
    assert research.wikipedia_first(ctx, "who wrote dune", plan, 4000) is None


def test_the_downloaded_wikipedia_passes_over_pages_that_only_list_meanings(engine):
    from jeeves.daemon.wikipedia import Wikipedia, is_disambiguation
    assert is_disambiguation("Mercury (disambiguation)", "")
    assert is_disambiguation("Mercury", "Mercury may refer to:\n\nMercury (planet)\n\nMercury (element)\n\nMore\n\nx")
    assert not is_disambiguation("Mercury (planet)", "Mercury is the first planet from the Sun. " * 20)
    w = Wikipedia(engine.settings, lambda *a: None)
    pages = {"A/Mercury": ("Mercury", "Mercury may refer to:\n\nMercury (planet)\n\nMercury (element)\n\nA\n\nB"),
             "A/Mercury_(element)": ("Mercury (element)", "Mercury is a chemical element with symbol Hg. " * 9),
             "A/Mercury_(planet)": ("Mercury (planet)", "Mercury is the first planet from the Sun. " * 9)}
    w._open = lambda: object()
    w._candidates = lambda archive, query, n=5: list(pages)
    w._read = lambda archive, path: pages[path]
    assert w.lookup("Mercury", about="how far is the planet Mercury from the sun")["title"] == "Mercury (planet)"
    assert w.lookup("Mercury")["title"] == "Mercury (element)"         # nothing else to go on: the first article
    w._candidates = lambda archive, query, n=5: ["A/Mercury"]
    assert w.lookup("Mercury") is None


def test_a_follow_up_doesnt_search_and_load_the_same_things_again(engine, monkeypatch):
    from jeeves.functions import research
    from jeeves.functions.partials import web
    searches, loads = [], []
    monkeypatch.setattr(web, "search", lambda settings, q, count=5, problems=None: searches.append(q) or [
        {"title": "Parkour Reborn Wiki", "url": "https://parkour-reborn.fandom.com/wiki/Home", "snippet": "A game."}])
    monkeypatch.setattr(web, "request_website", lambda ctx, url, **k: loads.append(url) or
                        "<html><body>" + "Parkour Reborn is a parkour movement game on Roblox. " * 30 + "</body></html>")
    monkeypatch.setattr(web, "find_wiki", lambda subject: None)
    engine.models.respond = lambda agent, text, **kw: "A parkour movement game on Roblox."
    ctx = _ctx(engine)
    notes: list[str] = []
    wiki = research.learn_context(ctx, "Parkour Reborn", notes, [])
    assert wiki == "https://parkour-reborn.fandom.com" and notes == ["Parkour Reborn: A parkour movement game on Roblox."]
    n_search, n_load = len(searches), len(loads)
    engine.models.respond = lambda *a, **k: (_ for _ in ()).throw(AssertionError("already known: no model call"))
    again: list[str] = []
    assert research.learn_context(_ctx(engine), "parkour reborn", again, []) == wiki      # the next request
    assert again == notes and len(searches) == n_search and len(loads) == n_load
    research.search_many(ctx, ["Parkour Reborn"])                    # the same search: remembered too
    assert len(searches) == n_search
    fut = research.prefetch(ctx, ["https://example.org/a"])["https://example.org/a"]
    assert fut.result()[0] and research.prefetch(ctx, ["https://example.org/a"])["https://example.org/a"].result()[0]
    assert loads.count("https://example.org/a") == 1
    engine.settings.set("research.cache_minutes", 0)                 # off: everything is fetched fresh
    research.search_many(ctx, ["Parkour Reborn"])
    assert len(searches) == n_search + 1
