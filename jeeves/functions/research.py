"""Deep research: several searches, the best pages, the relevant parts of them, and
follow-up searches until the question is answered (or the rounds run out).

1. The model writes a few search queries (exact names kept; for games, ones aimed at
   wikis, Reddit and guides). Without a model: the question, plus "wiki" / "reddit".
2. Results are deduplicated and ranked: specific sources (fandom/wiki pages, Reddit,
   Steam community, GameFAQs, official sites, Wikipedia) and titles matching the
   question come first.
3. Each page is read in full and only its relevant passages are kept -- the fact you
   want is often in the middle of a long wiki page, past the first few thousand
   characters.
4. After each page the model checks whether the question is now actually answered (for
   "how do I..." that means the steps, not a description of the thing). As soon as it is,
   reading stops and only the pages that answer it are used. If something is missing, it
   names it as a new search and another round runs (Settings: research depth).
5. The answer gives just what was asked -- steps for a how-to, the fact for a question --
   cites sources [n], and says plainly when the sources don't confirm something.
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


def plan_queries(ctx: Any, question: str) -> list[str]:
    fallback = [question]
    if HOWTO.search(question):
        fallback += [f"{question} guide"]
    elif GAME_HINT.search(question):
        fallback += [f"{question} wiki", f"{question} reddit"]
    else:
        fallback += [f"{question} explained"]
    reply = ctx.engine.models.respond(
        ctx.agent,
        f"Question: {question}\n\nWrite 3 different web search queries that would find the answer. Keep exact "
        "names (games, items, people, products). For game questions make one aimed at the game's wiki and one "
        "at Reddit or a guide. For 'how do I...' questions aim them at guides and tutorials. One query per line, "
        "nothing else.",
        ctx=None, raw=True)
    lines = [re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", ln).strip().strip('"') for ln in (reply or "").splitlines()]
    queries = [q for q in lines if 3 <= len(q) <= 150][:3]
    out: list[str] = []
    for q in queries + fallback:
        if q.lower() not in (x.lower() for x in out):
            out.append(q)
    return out[:4]


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


def assess(ctx: Any, question: str, sources: list[dict[str, Any]]) -> tuple[str, Any]:
    """After each page: ("answered", [source numbers that answer it]) to stop reading,
    ("search", query) for something missing, or ("more", None) to keep reading."""
    material = "\n\n".join(f"[{i + 1}] {s['title']}\n{s['text'][:1500]}" for i, s in enumerate(sources))
    reply = ctx.engine.models.respond(
        ctx.agent,
        f"Question: {question}\n\nWhat was found so far:\n{material}\n\nDoes this material actually answer the "
        f"question? That means {what_counts(question)}. Reply ANSWERED: followed by the numbers of the sources "
        "that answer it (e.g. ANSWERED: 2). If not, reply SEARCH: followed by one web search query for exactly "
        "what's missing (keep the exact names), or MORE if the next pages might have it.",
        ctx=None, raw=True) or ""
    head = reply.upper().split("SEARCH")[0]
    if "ANSWERED" in head:
        after = reply[reply.upper().index("ANSWERED") + 8:]
        nums = [int(n) for n in re.findall(r"\d+", after.split("\n")[0])]
        return "answered", [n for n in nums if 1 <= n <= len(sources)]
    m = re.search(r"SEARCH:\s*(.+)", reply)
    if m:
        return "search", m.group(1).strip().strip('"')[:150]
    return "more", None


def run(ctx: Any, question: str, depth: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """(sources that answer it -- or everything read, if nothing clearly did --, all search results).

    Pages are read best first, and after each one the model checks whether the question is now
    actually answered (for "how do I..." that means the steps, not a description). As soon as it is,
    reading stops and only the pages that answer it are kept. Also stops at the time budget."""
    import time as _time
    from .partials.web import request_website
    started = _time.time()
    budget = float(ctx.settings.get("research.max_seconds", 45))
    depth = str(depth or ctx.settings.get("research.depth", "normal"))
    rounds, per_round = DEPTHS.get(depth, DEPTHS["normal"])
    per_page = int(ctx.settings.get("research.max_chars_per_page", 4000))
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    results_all: list[dict[str, str]] = []
    kws = set(keywords(question))
    if ctx.engine.wikipedia is not None and ctx.engine.wikipedia.available():
        try:
            hit = ctx.call("wikipedia", query=question, max_chars=40000)
            sources.append({"title": f"Wikipedia: {hit['title']}", "url": "offline Wikipedia",
                            "text": relevant_passages(hit["text"], question, limit=per_page)})
        except FunctionError:
            pass
    queries = plan_queries(ctx, question)
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
        for r in rank(found, question):
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
            passages = relevant_passages(text, question, " ".join(queries), per_page)
            sources.append({"title": r["title"], "url": r["url"], "text": passages})
            read += 1
            if kws and not kws & set(keywords(passages)):
                continue                           # nothing on this page about it: no need to ask
            verdict, detail = assess(ctx, question, sources)
            if verdict == "answered":
                keep = [sources[n - 1] for n in detail] if detail else sources
                ctx.think(f"That answers it ({', '.join(s['title'] for s in keep)}); done reading.")
                return keep, results_all
            if verdict == "search":
                follow = detail
                break                              # what's missing needs a new search, not more of these
        if rnd == rounds - 1 or not sources or _time.time() - started > budget:
            break
        if follow is None:                         # these pages ran out without an answer: what's missing?
            verdict, detail = assess(ctx, question, sources)
            if verdict == "answered":
                return ([sources[n - 1] for n in detail] if detail else sources), results_all
            follow = detail if verdict == "search" else None
        if not follow:
            break
        ctx.think(f"Not answered yet -- digging deeper: {follow}")
        queries = [follow]
    return sources, results_all
