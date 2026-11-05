"""Custom Sources (Main > Agents > Custom sources): sites you point an agent at for its research, each
with a line on what it's for:

    https://parkour-reborn.fandom.com/wiki/Movement = Parkour Reborn Movement Wiki, for information on any movement techniques

When a question fits one of them -- by the words in that line, or the model's pick when a question
doesn't share any ("how do I wallhop" and "movement techniques") -- research reads that site first:
the page itself, the wiki's own search when it's a wiki, or a link on the page that names what was
asked about. Only if that doesn't answer it does research go on to the rest of the web, and then its
first searches are inside those sites ("site:parkour-reborn.fandom.com wumpy").
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from ..util import content_words
from .base import FunctionError

URL = re.compile(r"(?i)\b((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/\S*)?)")
SEPARATORS = " \t=:-–—|>,;"
# words in a site's line that say nothing about what it covers
PLAIN = {"wiki", "wikis", "fandom", "game", "games", "information", "info", "site", "website", "web", "page",
         "pages", "guide", "guides", "official", "list", "lists", "stuff", "tip", "tips", "help", "question",
         "questions", "anything", "any", "every", "everything", "www", "com", "org", "net", "gg", "http", "https",
         "html", "index", "main", "home", "for", "about", "other", "use", "used", "good", "best", "reddit", "the"}
MAX_PICKED = 2


def parse(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """Sites from lines of "address = what it's for" (any of = : - | between them, or the address
    last). Returns (sites, problems with lines that couldn't be read)."""
    sites: list[dict[str, str]] = []
    problems: list[str] = []
    for n, line in enumerate((text or "").splitlines(), 1):
        ln = line.strip()
        if not ln or ln.startswith("#"):
            continue
        m = URL.search(ln)
        if not m:
            problems.append(f"line {n} has no web address")
            continue
        url = m.group(1).rstrip(".,;)")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        about = (ln[:m.start()].strip(SEPARATORS) + " " + ln[m.end():].strip(SEPARATORS)).strip()
        sites.append({"url": url, "about": about})
    return sites, problems


def to_text(sites: list[dict[str, Any]]) -> str:
    return "\n".join(f"{s['url']} = {s['about']}" if s.get("about") else str(s["url"])
                     for s in sites or [] if isinstance(s, dict) and s.get("url"))


def sites_for(agent: dict[str, Any] | None) -> list[dict[str, str]]:
    """The agent's sites, if it uses them."""
    cs = (agent or {}).get("custom_sources") or {}
    if not isinstance(cs, dict) or not cs.get("enabled", True):
        return []
    return [s for s in cs.get("sites") or [] if isinstance(s, dict) and s.get("url")]


def host(url: str) -> str:
    h = urllib.parse.urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _words(site: dict[str, str]) -> set[str]:
    u = urllib.parse.urlparse(site["url"])
    from_url = " ".join(re.split(r"[/._#?=&+-]+", urllib.parse.unquote(u.netloc + " " + u.path)))
    return {w for w in content_words(f"{site.get('about', '')} {from_url}") if w not in PLAIN}


def score(site: dict[str, str], text: str) -> int:
    """How many of the words that say what the site covers are in the text."""
    return len(_words(site) & content_words(text))


def matches(agent: dict[str, Any] | None, question: str) -> bool:
    """A question plainly about one of the agent's sites ("... in parkour reborn" with the Parkour
    Reborn wiki on the list): research it, don't answer from memory."""
    return any(score(s, question) >= 2 for s in sites_for(agent))


def pick(ctx: Any, question: str, plan: dict[str, Any], sites: list[dict[str, str]] | None = None
         ) -> list[dict[str, str]]:
    """The sites that fit this question: by the words in their lines, else the model's pick."""
    sites = sites if sites is not None else sites_for(ctx.agent)
    if not sites:
        return []
    text = " ".join([question, str(plan.get("goal") or ""), " ".join(plan.get("context") or []),
                     " ".join(plan.get("terms") or [])])
    scored = sorted(((score(s, text), i, s) for i, s in enumerate(sites)), key=lambda x: (-x[0], x[1]))
    best = [s for sc, _i, s in scored if sc >= 2][:MAX_PICKED]
    if best:
        return best
    shown = sites[:20]
    listing = "\n".join(f"{i + 1}. {s.get('about') or host(s['url'])} ({host(s['url'])})" for i, s in enumerate(shown))
    reply = ctx.engine.models.respond(
        ctx.agent, f"Question: {plan.get('goal') or question}\n\nSites:\n{listing}\n\nWhich of these sites would "
        "have the answer? Reply with their numbers (at most two), or NONE.",
        ctx=None, raw=True, temperature=0.0, max_tokens=20)
    if reply is None:                              # no model: a site sharing a word with the question
        return [s for sc, _i, s in scored if sc >= 1][:1]
    nums = [int(n) for n in re.findall(r"\d+", reply)]
    out: list[dict[str, str]] = []
    for n in nums:
        if 1 <= n <= len(shown) and shown[n - 1] not in out:
            out.append(shown[n - 1])
    return out[:MAX_PICKED]


def _wiki_title(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    m = re.search(r"/wiki/(.+)$", path)
    return urllib.parse.unquote(m.group(1)).replace("_", " ") if m else ""


def read(ctx: Any, site: dict[str, str], goal: str, terms: list[str], sources: list[dict[str, Any]],
         seen: set[str], per_page: int, wikis: set[str]) -> list[dict[str, Any]] | None:
    """Look in one of your sites the way you'd want it done: its page; if what you asked about isn't
    on it, the wiki's own search (a wiki) or the link on the page that names it. Returns the sources
    that answer the question, if they do."""
    from .partials.web import html_to_text, page_links, request_website, wiki_article, wiki_base
    from .research import assess, read_wiki, relevant_passages, term_in, term_passages
    url = site["url"]
    name = re.split(r",|;| - | – | for ", site.get("about") or "", maxsplit=1)[0].strip() or host(url)
    base, title = wiki_base(url), _wiki_title(url)
    ctx.check_cancelled()
    ctx.think(f"Checking your source: {name}", looking_at=url)
    seen.add(url)
    text = page_html = ""
    if base and title:
        try:
            text = wiki_article(base, title)              # a wiki page: just the article, no menus
        except FunctionError:
            text = ""
    if not text:
        try:
            page_html = request_website(ctx, url, raw=True, max_chars=600000, timeout=10)
            text = html_to_text(page_html) if "<" in page_html[:2000] else page_html
        except FunctionError as exc:
            ctx.think(f"  couldn't read it: {exc}")
    found = [t for t in terms if term_in(text, t) >= 0]
    if terms and not found:
        if base and base not in wikis:                # not on that page: the wiki's own search, its pages
            wikis.add(base)
            ctx.think(f"  no mention of {', '.join(terms)} on that page; searching the wiki")
            done = read_wiki(ctx, base, goal, terms, "", [], sources, seen, per_page)
            if done:
                return done
            return None
        link = next((ln for ln in page_links(page_html, url) if ln["url"] not in seen and
                     any(term_in(ln["text"], t) >= 0 for t in terms)), None) if page_html else None
        if link is None:
            ctx.think(f"  no mention of {', '.join(terms)} there")
            return None
        seen.add(link["url"])
        ctx.think(f"  following the link '{link['text']}'", looking_at=link["url"])
        try:
            text = request_website(ctx, link["url"], max_chars=80000, timeout=10)
        except FunctionError as exc:
            ctx.think(f"  couldn't read it: {exc}")
            return None
        url, name = link["url"], f"{name} > {link['text']}"
        found = [t for t in terms if term_in(text, t) >= 0]
    if len(text.strip()) < 80:
        return None
    passages = term_passages(text, found, per_page) if found else relevant_passages(text, goal, " ".join(terms),
                                                                                     per_page)
    sources.append({"title": name, "url": url, "text": passages})
    verdict, detail = assess(ctx, goal, sources)
    if verdict == "answered":
        return [sources[n - 1] for n in detail] if detail else sources[-1:]
    return None
