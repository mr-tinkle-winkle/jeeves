"""Request a Website, and the offline Wikipedia lookup."""
from __future__ import annotations

import html
import re
import urllib.error
import urllib.request

from ..base import Arg, FunctionError, partial

UA = "Jeeves/0.1 (local voice assistant)"


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(raw)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


@partial("request_website", "Downloads a web page and returns its readable text (or raw content).",
         args=[Arg("url", "url", "The address"),
               Arg("raw", "boolean", "Return raw HTML/JSON instead of readable text", required=False, default=False),
               Arg("max_chars", "integer", "Truncate to this many characters", required=False, default=20000)],
         how="A plain HTTP request in the background -- no window opens, so pages that need JavaScript may "
             "come back mostly empty.",
         returns="page text", category="web")
def request_website(ctx, url, raw=False, max_chars=20000, timeout=20):
    url = str(url)
    if not re.match(r"^https?://", url):
        url = "https://" + url
    ctx.state("researching", f"Reading {url}")
    # a browser's User-Agent: wikis (Fandom) and forums often refuse unknown clients
    req = urllib.request.Request(url, headers=dict(BROWSER_HEADERS, Accept="text/html,application/json,*/*"))
    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            body = resp.read(5_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            ctype = resp.headers.get_content_type()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise FunctionError(f"couldn't load {url}: {exc}") from exc
    text = body.decode(charset, "replace")
    if not raw and ctype == "text/html":
        text = html_to_text(text)
    return text[: int(max_chars)]


@partial("wikipedia", "Looks something up in the downloaded offline Wikipedia.",
         args=[Arg("query", "string", "Article title or search words"),
               Arg("max_chars", "integer", "How much of the article", required=False, default=6000)],
         how="Searches the local Wikipedia copy (Settings > Wikipedia). The indicator shows 'researching' "
             "while it reads.",
         returns="{title, text} of the best article", category="web")
def wikipedia(ctx, query, max_chars=6000):
    ctx.state("researching", f"Wikipedia: {query}")
    wiki = ctx.engine.wikipedia
    if wiki is None or not wiki.available():
        raise FunctionError("Wikipedia hasn't been downloaded yet (Settings > Wikipedia)")
    hit = wiki.lookup(str(query), int(max_chars))
    if not hit:
        raise FunctionError(f"nothing in Wikipedia for '{query}'")
    ctx.think(f"Reading Wikipedia article: {hit['title']}", looking_at=hit["title"])
    return hit


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------

def _attr(tag: str, name: str) -> str:
    m = re.search(rf"""\b{name}\s*=\s*(["'])(.*?)\1""", tag, re.S)
    return html.unescape(m.group(2)) if m else ""


def _real_url(href: str) -> str:
    import urllib.parse
    if href.startswith("//"):
        href = "https:" + href
    q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    return q.get("uddg", [href])[0]          # DuckDuckGo wraps results in a redirect


def parse_ddg(page: str, count: int) -> list[dict[str, str]]:
    """Results from DuckDuckGo's HTML page (class result__a / result__snippet) or its
    Lite page (class result-link / result-snippet). Attribute order doesn't matter."""
    out: list[dict[str, str]] = []
    links = list(re.finditer(r"<a\b([^>]*\bclass\s*=\s*[\"'][^\"']*\bresult(?:__a|-link)\b[^\"']*[\"'][^>]*)>(.*?)</a>",
                             page, re.S))
    for i, m in enumerate(links):
        url = _real_url(_attr(m.group(1), "href"))
        if not url.startswith("http") or "duckduckgo.com/y.js" in url:      # ads / internal links
            continue
        tail = page[m.end(): links[i + 1].start() if i + 1 < len(links) else len(page)]
        snip = re.search(r"class\s*=\s*[\"'][^\"']*\bresult(?:__snippet|-snippet)\b[^\"']*[\"'][^>]*>(.*?)</(?:a|td|div)>",
                         tail, re.S)
        out.append({"title": html_to_text(m.group(2)), "url": url,
                    "snippet": html_to_text(snip.group(1)) if snip else ""})
        if len(out) >= count:
            break
    return out


BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Accept-Language": "en-US,en;q=0.8"}


class SearchBlocked(Exception):
    """The engine answered with a bot check instead of results."""


def _fetch(url: str, data: dict[str, str] | None = None, timeout: float = 15) -> str:
    import urllib.parse
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    headers = dict(BROWSER_HEADERS)
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(3_000_000).decode(resp.headers.get_content_charset() or "utf-8", "replace")


def _ddg(query: str, count: int) -> list[dict[str, str]]:
    """DuckDuckGo (no account or API key): the HTML page, then the Lite page. Both are asked with
    a form POST like the pages themselves do -- a plain GET is what DuckDuckGo most often answers
    with a bot check, which used to come back as "no results" without saying why."""
    blocked = False
    for base in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        page = _fetch(base, {"q": query, "kl": "us-en"})
        results = parse_ddg(page, count)
        if results:
            return results
        if re.search(r"anomaly|challenge-form|bots use DuckDuckGo", page, re.I):
            blocked = True
    if blocked:
        raise SearchBlocked("DuckDuckGo asked for a bot check")
    return []


def _bing_url(href: str) -> str:
    """Bing wraps result links (bing.com/ck/a?...&u=a1<base64 url>)."""
    import base64
    import urllib.parse
    if "bing.com/ck/" not in href:
        return href
    u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
    if u.startswith("a1"):
        try:
            b = u[2:]
            return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            pass
    return href


def parse_blocks(page: str, block: str, count: int, title_link: str, snippet: str,
                 fix_url=lambda u: u) -> list[dict[str, str]]:
    """Results laid out as one element per result (regex `block` marks where each starts)."""
    out: list[dict[str, str]] = []
    starts = [m.start() for m in re.finditer(block, page)]
    for i, st in enumerate(starts):
        chunk = page[st: starts[i + 1] if i + 1 < len(starts) else st + 6000]
        a = re.search(title_link, chunk, re.S)
        if not a:
            continue
        url = fix_url(_attr(a.group(1), "href"))
        if not url.startswith("http"):
            continue
        sn = re.search(snippet, chunk, re.S)
        out.append({"title": html_to_text(a.group(2)), "url": url, "snippet": html_to_text(sn.group(1)) if sn else ""})
        if len(out) >= count:
            break
    return out


def parse_mojeek(page: str, count: int) -> list[dict[str, str]]:
    return parse_blocks(page, r"<a\b[^>]*\bclass\s*=\s*[\"']title\b", count,
                        r"<a\b([^>]*\bclass\s*=\s*[\"']title\b[^>]*)>(.*?)</a>",
                        r"<p\b[^>]*\bclass\s*=\s*[\"']s\b[^>]*>(.*?)</p>")


def parse_bing(page: str, count: int) -> list[dict[str, str]]:
    return parse_blocks(page, r"<li\b[^>]*\bclass\s*=\s*[\"'][^\"']*\bb_algo\b", count,
                        r"<h2\b[^>]*>\s*<a\b([^>]*)>(.*?)</a>",
                        r"<p\b[^>]*>(.*?)</p>", _bing_url)


def _mojeek(query: str, count: int) -> list[dict[str, str]]:
    import urllib.parse
    page = _fetch("https://www.mojeek.com/search?" + urllib.parse.urlencode({"q": query}))
    return parse_mojeek(page, count)


def _bing(query: str, count: int) -> list[dict[str, str]]:
    import urllib.parse
    page = _fetch("https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "en"}))
    return parse_bing(page, count)


def _searxng(base: str, query: str, count: int) -> list[dict[str, str]]:
    import json
    import urllib.parse
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as resp:
        data = json.loads(resp.read().decode())
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in data.get("results", [])[:count]]


ENGINES = {"duckduckgo": _ddg, "mojeek": _mojeek, "bing": _bing}


def search(settings, query: str, count: int = 5, problems: list[str] | None = None) -> list[dict[str, str]]:
    """Results from the first engine that gives any: your SearXNG if set, then DuckDuckGo, Mojeek
    and Bing (any of them can refuse a script now and then). problems collects why engines failed."""
    problems = problems if problems is not None else []
    base = settings.get("research.searxng_url") or ""
    order = []
    if settings.get("research.engine", "duckduckgo") == "searxng" and base:
        order.append(("SearXNG", lambda q, c: _searxng(base, q, c)))
    first = settings.get("research.engine", "duckduckgo")
    names = [first] + [n for n in ENGINES if n != first] if first in ENGINES else list(ENGINES)
    order += [(n, ENGINES[n]) for n in names]
    for name, fn in order:
        try:
            results = fn(query, count)
        except SearchBlocked as exc:
            problems.append(str(exc))
            continue
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            problems.append(f"{name}: {exc}")
            continue
        if results:
            return results
        problems.append(f"{name}: no results")
    return []


@partial("web_search", "Searches the web and returns the top results (title, address and a snippet).",
         args=[Arg("query", "string", "What to search for"),
               Arg("count", "integer", "How many results", required=False, default=5)],
         how="DuckDuckGo by default (no account or key), falling back to Mojeek and Bing when it refuses; or "
             "your own SearXNG instance (Settings > Listening & Keys > Research).",
         returns="list of {title, url, snippet}", category="web")
def web_search(ctx, query, count=5):
    ctx.state("researching", f"Searching: {query}")
    ctx.think(f"Searching the web for: {query}", looking_at=f"search: {query}")
    problems: list[str] = []
    results = search(ctx.settings, str(query), int(count), problems)
    if not results:
        failed = [p for p in problems if not p.endswith("no results")]
        if failed and len(failed) == len(problems):
            raise FunctionError("web search failed: " + "; ".join(failed))
        ctx.think("  no results (" + "; ".join(problems) + ")")
    for r in results:
        ctx.think(f"- {r['title']} ({r['url']})")
    return results


# ---------------------------------------------------------------------------
# Wikis (Fandom, wiki.gg, any MediaWiki): search inside the wiki, read an article cleanly
# ---------------------------------------------------------------------------

def wiki_base(url: str) -> str | None:
    """The wiki a page belongs to ("https://parkour-reborn.fandom.com"), when it looks like a MediaWiki
    (fandom.com, wiki.gg, or a /wiki/ path)."""
    import urllib.parse
    u = urllib.parse.urlparse(url)
    if not u.scheme or not u.netloc:
        return None
    host = u.netloc.lower()
    if host.endswith(".fandom.com") or host.endswith(".wiki.gg") or host.endswith("wikipedia.org") \
            or "/wiki/" in u.path or host.startswith("wiki."):
        if host in ("www.fandom.com", "fandom.com", "community.fandom.com"):
            return None
        lang = re.match(r"^/([a-z]{2}(?:-[a-z]+)?)/wiki/", u.path)        # fandom: /es/wiki/...
        return f"{u.scheme}://{u.netloc}" + (f"/{lang.group(1)}" if lang else "")
    return None


def _wiki_api(base: str, params: dict[str, str]) -> dict:
    import json
    import urllib.parse
    q = urllib.parse.urlencode(dict(params, format="json"))
    last: Exception | None = None
    for api in ("/api.php", "/w/api.php"):
        try:
            return json.loads(_fetch(base + api + "?" + q, timeout=12))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            last = exc
    raise FunctionError(f"couldn't reach the wiki at {base}: {last}")


def wiki_search(base: str, query: str, count: int = 5) -> list[dict[str, str]]:
    """Articles in this wiki matching the query: [{title, url, snippet}]."""
    import urllib.parse
    data = _wiki_api(base, {"action": "query", "list": "search", "srsearch": query, "srlimit": str(count)})
    out = []
    for r in (data.get("query") or {}).get("search", []):
        title = r.get("title", "")
        out.append({"title": title, "url": f"{base}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                    "snippet": html_to_text(r.get("snippet", ""))})
    return out


def wiki_article(base: str, title: str) -> str:
    """An article's text without the site around it (menus, ads, other pages' links)."""
    data = _wiki_api(base, {"action": "parse", "page": title, "prop": "text", "redirects": "1"})
    html_text = ((data.get("parse") or {}).get("text") or {}).get("*", "")
    if not html_text:
        raise FunctionError(f"no article '{title}' in that wiki")
    html_text = re.sub(r'(?is)<(table|div)[^>]*class="[^"]*\b(navbox|toc|mw-references-wrap|reflist)\b.*?</\1>', " ",
                       html_text)
    return html_to_text(html_text)
