"""Research the way a person would: work out what the question is really asking, learn
the context first, then look in the right place.

"How do I do a wumpy in Parkour Reborn?"
1. Understand: the goal restated, the CONTEXT it happens in (Parkour Reborn) and the
   unfamiliar TERMS (wumpy).
2. Learn the context: look up "Parkour Reborn" first and note what it is (a parkour
   movement game). Its wiki, if one turns up, is remembered.
3. Reason: with that context, what is a "wumpy" most likely (a movement technique), and
   which searches would find the answer ("parkour reborn wumpy movement").
4. Look in the right place: the game's wiki first -- searched with the wiki's own search
   and read as clean articles -- then the web, best pages first (guides first for
   "how do I..."). Each page keeps only its relevant passages.
5. After each page the model checks whether the ORIGINAL question is actually answered
   (for "how do I..." that means the steps, not a description). Reading stops as soon as
   it is, and only the pages that answer it are used. Something missing becomes a new search.
6. The answer is to the original question, from those pages and the context notes.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from .base import FunctionError

DEPTHS = {"quick": (1, 3), "normal": (2, 5), "deep": (3, 8)}       # rounds, pages read per round
GOOD_SITES = ("fandom.com", "wiki", "reddit.com", "steamcommunity.com", "gamefaqs", "fextralife", "ign.com",
              "polygon.com", "pcgamer.com", "gamerant.com", "thegamer.com", "wikipedia.org", "github.com",
              "stackexchange.com", "stackoverflow.com")
BAD_SITES = ("pinterest.", "facebook.", "tiktok.", "instagram.", "x.com", "twitter.com")   # nothing to read
STOP = set("the a an and or of to in on for with what which who whom whose when where why how is are was were be "
           "been do does did can could should would will i me my you your it its this that these those there "
           "about from at by as into than then them they we our us please tell know find out get".split())
GAME_HINT = re.compile(r"\b(boss|build|item|weapon|armou?r|quest|level|map|patch|dlc|achievement|trophy|"
                       r"walkthrough|spawn|drop|location|lore|ending|skill|perk|class|mod|speedrun|game)\b", re.I)


HOWTO = re.compile(r"\b(how (?:do|can|to|would|should|does one)|how'?s it done|steps? (?:to|for)|way to|"
                   r"guide (?:to|for)|tutorial|perform|pull off|execute)\b", re.I)


def keywords(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'+-]*", text.lower()) if w not in STOP and len(w) > 1]


def _lines(reply: str | None, key: str) -> str:
    m = re.search(rf"^\s*{key}\s*:\s*(.+)$", reply or "", re.I | re.M)
    v = (m.group(1).strip().strip('"') if m else "")
    return "" if v.upper().startswith("NONE") else v


def understand(ctx: Any, question: str) -> dict[str, Any]:
    """{goal, context: [things to learn about first], terms: [words to look up]} -- the question broken
    down before any searching, so the searches are about the right thing."""
    reply = ctx.engine.models.respond(
        ctx.agent,
        f"Question: {question}\n\nBefore searching, break this question down. Reply in exactly this format:\n"
        "GOAL: the question restated clearly and completely (keep exact names and spellings)\n"
        "CONTEXT: what the question happens in or is about, to learn about first -- a game, app, show, product, "
        "place or person -- or NONE\n"
        "TERMS: unfamiliar words or names in the question to look up, comma-separated, or NONE\n\n"
        "Example:\nQuestion: how do i do a wumpy in parkour reborn\n"
        "GOAL: How do I perform a 'wumpy' in the game Parkour Reborn?\nCONTEXT: Parkour Reborn\nTERMS: wumpy",
        ctx=None, raw=True, temperature=0.1, max_tokens=120)
    goal = _lines(reply, "GOAL") or question
    context = [c.strip() for c in re.split(r",|;| and ", _lines(reply, "CONTEXT")) if len(c.strip()) > 1][:2]
    terms = [t.strip() for t in _lines(reply, "TERMS").split(",") if len(t.strip()) > 1][:3]
    if not reply or not (context or terms):          # no model / no usable answer: "... in <Game>"
        m = re.search(r"\b(?:in|on|for|from)\s+(?:the\s+game\s+)?([A-Z0-9][\w'.:-]*(?:\s+[A-Z0-9][\w'.:-]*)*)\s*\??$",
                      question)
        if m and not context:
            context = [m.group(1)]
    return {"goal": goal, "context": context, "terms": terms}


def refine(ctx: Any, goal: str, notes: list[str], terms: list[str]) -> tuple[str, list[str]]:
    """(a guess at what the unfamiliar terms are, search queries) -- with the context learned so far."""
    fallback = [goal] + ([f"{goal} guide"] if HOWTO.search(goal) else
                         [f"{goal} wiki", f"{goal} reddit"] if GAME_HINT.search(goal) else [f"{goal} explained"])
    learned = "\n".join(f"- {n}" for n in notes) or "(nothing yet)"
    about = f" Think about what {', '.join(terms)} most likely is, given the context -- e.g. in a movement game " \
            "an unfamiliar word is probably a movement technique." if terms else ""
    reply = ctx.engine.models.respond(
        ctx.agent,
        f"Question: {goal}\n\nWhat you've learned:\n{learned}\n\n{about.strip()}\nThen write 3 different web "
        "search queries that would find the answer. Use the context's exact name and the right category word "
        "(movement, item, boss, setting, command...). For 'how do I...' questions aim at guides and the wiki. "
        "Reply with GUESS: <one sentence> on the first line, then one query per line, nothing else.",
        ctx=None, raw=True, temperature=0.2, max_tokens=160)
    guess = _lines(reply, "GUESS")
    lines = [re.sub(r"^\s*(?:\d+[.)]|[-*•]|QUERY\s*:)\s*", "", ln, flags=re.I).strip().strip('"')
             for ln in (reply or "").splitlines() if not re.match(r"^\s*GUESS\s*:", ln, re.I)]
    queries = [q for q in lines if 3 <= len(q) <= 150][:3]
    out: list[str] = []
    for q in queries + fallback:
        if q.lower() not in (x.lower() for x in out):
            out.append(q)
    return guess, out[:4]


GUIDE_WORDS = re.compile(r"\b(how to|guide|tutorial|tips|walkthrough|steps|technique|tech)\b", re.I)


def rank(results: list[dict[str, str]], question: str) -> list[dict[str, str]]:
    kws = set(keywords(question))
    howto = HOWTO.search(question) is not None

    def score(r: dict[str, str]) -> float:
        host = urlparse(r.get("url", "")).netloc.lower()
        s = 2.0 if any(g in host or g in r.get("url", "") for g in GOOD_SITES) else 0.0
        words = set(keywords(r.get("title", "") + " " + r.get("snippet", "")))
        s += len(kws & words) / max(1, len(kws)) * 3
        if howto and GUIDE_WORDS.search(r.get("title", "") + " " + r.get("snippet", "") + " " + r.get("url", "")):
            s += 1.5                                 # a how-to question: guides first
        return s
    useful = [r for r in results if not any(b in urlparse(r.get("url", "")).netloc.lower() for b in BAD_SITES)]
    return sorted(useful, key=score, reverse=True)


def relevant_passages(text: str, question: str, extra: str = "", limit: int = 4000) -> str:
    """The parts of a page that talk about the question, in page order, up to limit chars."""
    if len(text) <= limit:
        return text
    kws = keywords(question + " " + extra)
    chunks = [c.strip() for c in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", text) if len(c.strip()) > 30]
    if not chunks:
        return text[:limit]
    scored = []
    for i, c in enumerate(chunks):
        low = c.lower()
        hits = sum(low.count(k) for k in kws)
        distinct = sum(1 for k in kws if k in low)
        scored.append((distinct * 3 + min(hits, 10), i))
    keep, total = set(), 0
    for sc, i in sorted(scored, reverse=True):
        if sc == 0 or total >= limit:
            break
        for j in (i - 1, i, i + 1):                 # a little context around each hit
            if 0 <= j < len(chunks) and j not in keep and total < limit:
                keep.add(j)
                total += len(chunks[j]) + 1
    if not keep:
        return text[:limit]
    out, prev = [], None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            out.append("…")
        out.append(chunks[i])
        prev = i
    return "\n".join(out)[:limit]


def kind_of(question: str) -> str:
    """'howto' (wants steps), or 'fact' (wants the thing itself)."""
    return "howto" if HOWTO.search(question) else "fact"


def what_counts(question: str) -> str:
    if kind_of(question) == "howto":
        return ("the actual steps or inputs to do it. A page that only says what it is, why it's useful or its "
                "history does NOT answer a how-to question")
    return "a direct, specific answer to exactly what was asked -- not just general information about the topic"


def fit(sources: list[dict[str, Any]], budget: int, newest: int = 0, urls: bool = True) -> str:
    """The sources as numbered material within `budget` characters (the model's context is limited:
    ten pages of 4000 characters overflowed it and the answer came back empty). Each source gets an
    even share, the `newest` one up to three shares (it's the one being judged)."""
    if not sources:
        return ""
    weights = [3 if newest and i == len(sources) - 1 else 1 for i in range(len(sources))]
    unit = budget / sum(weights)
    parts = []
    for i, (src, w) in enumerate(zip(sources, weights)):
        room = max(300, int(unit * w))
        text = src["text"] if len(src["text"]) <= room else src["text"][:room].rsplit(" ", 1)[0] + " …"
        parts.append(f"[{i + 1}] {src['title']}" + (f" ({src['url']})" if urls and src.get("url") else "")
                     + f"\n{text}")
    return "\n\n".join(parts)


def assess(ctx: Any, question: str, sources: list[dict[str, Any]]) -> tuple[str, Any]:
    """After each page: ("answered", [source numbers that answer it]) to stop reading,
    ("search", query) for something missing, or ("more", None) to keep reading."""
    material = fit(sources[-6:], 6000, newest=1, urls=False)
    offset = len(sources) - len(sources[-6:])
    reply = ctx.engine.models.respond(
        ctx.agent,
        f"Question: {question}\n\nWhat was found so far:\n{material}\n\nDoes this material actually answer the "
        f"question? That means {what_counts(question)}. Reply ANSWERED: followed by the numbers of the sources "
        "that answer it (e.g. ANSWERED: 2). If not, reply SEARCH: followed by one web search query for exactly "
        "what's missing (keep the exact names), or MORE if the next pages might have it.",
        ctx=None, raw=True, temperature=0.0, max_tokens=60) or ""
    head = reply.upper().split("SEARCH")[0]
    if "ANSWERED" in head:
        after = reply[reply.upper().index("ANSWERED") + 8:]
        nums = [int(n) + offset for n in re.findall(r"\d+", after.split("\n")[0])]
        return "answered", [n for n in nums if 1 <= n <= len(sources)]
    m = re.search(r"SEARCH:\s*(.+)", reply)
    if m:
        return "search", m.group(1).strip().strip('"')[:150]
    return "more", None


def best_of(sources: list[dict[str, Any]], question: str, n: int = 5) -> list[dict[str, Any]]:
    """The n sources that talk most about the question, in their original order."""
    if len(sources) <= n:
        return sources
    kws = set(keywords(question))
    ranked = sorted(range(len(sources)), key=lambda i: -sum(1 for k in kws if k in sources[i]["text"].lower()))
    keep = set(ranked[:n])
    return [s for i, s in enumerate(sources) if i in keep]


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def learn_context(ctx: Any, subject: str, notes: list[str], results_all: list[dict[str, str]]) -> str | None:
    """Look the subject up and note what it is. Returns its wiki (a MediaWiki base URL) if one shows up."""
    from .partials.web import request_website, wiki_base
    ctx.think(f"First, what is {subject}?")
    found: list[dict[str, str]] = []
    for q in (subject, f"{subject} wiki"):
        try:
            found += ctx.call("web_search", query=q, count=6)
        except FunctionError as exc:
            ctx.think(f"  search failed: {exc}")
    results_all += found
    want = _squash(subject)
    wiki = None
    for r in found:                                    # the subject's own wiki: its name in the address
        base = wiki_base(r.get("url", ""))
        if base and want and (want[:12] in _squash(base) or want in _squash(r.get("title", "")) and "wiki" in
                              r.get("title", "").lower()):
            wiki = base
            break
    text = ""
    for r in rank(found, subject)[:2]:
        try:
            page = request_website(ctx, r["url"], max_chars=60000, timeout=8)
        except FunctionError:
            page = ""
        text = relevant_passages(page, subject, limit=2500) if len(page.strip()) > 200 else r.get("snippet", "")
        if text.strip():
            break
    if not text.strip():
        text = "\n".join(f"{r['title']}: {r.get('snippet', '')}" for r in found[:4])
    if text.strip():
        summary = ctx.engine.models.respond(
            ctx.agent, f"From this text, say in one or two sentences what {subject} is (what kind of thing, and what "
            f"it's about). If the text isn't about it, reply UNKNOWN.\n\n{text[:3000]}", ctx=None, raw=True,
            temperature=0.1, max_tokens=100)
        if summary and "UNKNOWN" not in summary.upper():
            notes.append(f"{subject}: {summary.strip()}")
            ctx.think(f"  {summary.strip()}")
    if wiki:
        ctx.think(f"  its wiki: {wiki}")
    return wiki


def read_wiki(ctx: Any, wiki: str, goal: str, terms: list[str], guess: str, queries: list[str],
              sources: list[dict[str, Any]], seen: set[str], per_page: int) -> list[dict[str, Any]] | None:
    """Search the subject's own wiki and read the best articles; the answering sources if they answer it."""
    from .partials.web import wiki_article, wiki_search
    tries = [t for t in terms] + ([f"{terms[0]} {w}" for w in keywords(guess)[:2]] if terms and guess else []) + \
        [goal]
    titles: list[dict[str, str]] = []
    for q in tries[:4]:
        try:
            hits = wiki_search(wiki, q, 4)
        except FunctionError as exc:
            ctx.think(f"  wiki search failed: {exc}")
            return None
        for h in hits:
            if h["url"] not in seen and h["url"] not in (t["url"] for t in titles):
                titles.append(h)
        if len(titles) >= 3:
            break
    want = set(keywords(" ".join(terms) or goal))
    titles.sort(key=lambda t: -len(want & set(keywords(t["title"]))))      # "Wumpy" before "Movement"
    for t in titles[:3]:
        ctx.check_cancelled()
        seen.add(t["url"])
        ctx.think(f"Reading the wiki: {t['title']}", looking_at=t["url"])
        try:
            text = wiki_article(wiki, t["title"])
        except FunctionError as exc:
            ctx.think(f"  couldn't read it: {exc}")
            continue
        sources.append({"title": t["title"], "url": t["url"],
                        "text": relevant_passages(text, goal, " ".join(terms + queries), per_page)})
        verdict, detail = assess(ctx, goal, sources)
        if verdict == "answered":
            return [sources[n - 1] for n in detail] if detail else sources[-1:]
    return None


def run(ctx: Any, question: str, depth: str | None = None
        ) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    """(sources that answer it -- or everything read, if nothing clearly did --, all search results,
    notes on the context learned along the way)."""
    import time as _time
    from .partials.web import request_website
    started = _time.time()
    budget = float(ctx.settings.get("research.max_seconds", 90))
    depth = str(depth or ctx.settings.get("research.depth", "normal"))
    rounds, per_round = DEPTHS.get(depth, DEPTHS["normal"])
    per_page = int(ctx.settings.get("research.max_chars_per_page", 4000))
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    results_all: list[dict[str, str]] = []
    notes: list[str] = []

    plan = understand(ctx, question)
    goal, terms = plan["goal"], plan["terms"]
    ctx.think(f"Question: {goal}" + (f"\nContext: {', '.join(plan['context'])}" if plan["context"] else "") +
              (f"\nTo look up: {', '.join(terms)}" if terms else ""))
    wiki = None
    for subject in plan["context"]:
        ctx.check_cancelled()
        wiki = learn_context(ctx, subject, notes, results_all) or wiki
    guess, queries = refine(ctx, goal, notes, terms)
    if guess:
        # only for searching: an unconfirmed guess in the answer's context got said as if it were a fact
        ctx.think(f"Probably: {guess}")
    if wiki:
        done = read_wiki(ctx, wiki, goal, terms, guess, queries, sources, seen, per_page)
        if done:
            ctx.think(f"That answers it ({', '.join(s['title'] for s in done)}).")
            return done, results_all, notes

    if ctx.engine.wikipedia is not None and ctx.engine.wikipedia.available() and not sources:
        try:
            hit = ctx.call("wikipedia", query=goal, max_chars=40000)
            sources.append({"title": f"Wikipedia: {hit['title']}", "url": "offline Wikipedia",
                            "text": relevant_passages(hit["text"], goal, limit=per_page)})
        except FunctionError:
            pass
    kws = set(keywords(goal))
    for rnd in range(rounds):
        ctx.check_cancelled()
        ctx.think(f"Searching: {' | '.join(queries)}")
        found: list[dict[str, str]] = []
        for q in queries:
            try:
                found += ctx.call("web_search", query=q, count=6)
            except FunctionError as exc:
                ctx.think(f"  search failed: {exc}")
        fresh = []
        for r in rank(found, goal + " " + " ".join(queries)):
            u = r.get("url", "")
            if u and u not in seen and u not in (x.get("url") for x in fresh):
                fresh.append(r)
        results_all += fresh
        read = 0
        follow = None
        for r in fresh:
            if read >= per_round or (_time.time() - started > budget and sources):
                break
            ctx.check_cancelled()
            seen.add(r["url"])
            ctx.think(f"Reading {r['url']}", looking_at=r["url"])
            try:
                text = request_website(ctx, r["url"], max_chars=80000, timeout=8)
            except FunctionError as exc:
                ctx.think(f"  couldn't read it: {exc}")
                text = ""
            if len(text.strip()) < 200:            # JavaScript-only or blocked page: use the snippet
                text = r.get("snippet", "")
            if not text.strip():
                continue
            passages = relevant_passages(text, goal, " ".join(queries + terms), per_page)
            sources.append({"title": r["title"], "url": r["url"], "text": passages})
            read += 1
            if kws and not kws & set(keywords(passages)):
                continue                           # nothing on this page about it: no need to ask
            verdict, detail = assess(ctx, goal, sources)
            if verdict == "answered":
                keep = [sources[n - 1] for n in detail] if detail else sources
                ctx.think(f"That answers it ({', '.join(s['title'] for s in keep)}); done reading.")
                return keep, results_all, notes
            if verdict == "search":
                follow = detail
                break                              # what's missing needs a new search, not more of these
        if rnd == rounds - 1 or not sources or _time.time() - started > budget:
            break
        if follow is None:                         # these pages ran out without an answer: what's missing?
            verdict, detail = assess(ctx, goal, sources)
            if verdict == "answered":
                return ([sources[n - 1] for n in detail] if detail else sources), results_all, notes
            follow = detail if verdict == "search" else None
        if not follow:
            break
        ctx.think(f"Not answered yet -- digging deeper: {follow}")
        queries = [follow]
    return sources, results_all, notes
