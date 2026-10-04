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
def request_website(ctx, url, raw=False, max_chars=20000):
    url = str(url)
    if not re.match(r"^https?://", url):
        url = "https://" + url
    ctx.state("researching", f"Reading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
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


def _ddg(query: str, count: int) -> list[dict[str, str]]:
    """DuckDuckGo (no account or API key): the HTML page, then the Lite page if the
    first gave nothing (it sometimes serves a challenge page instead)."""
    import urllib.parse
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
               "Accept-Language": "en-US,en;q=0.8"}
    for base in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        req = urllib.request.Request(base + "?" + urllib.parse.urlencode({"q": query}), headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            page = resp.read().decode("utf-8", "replace")
        results = parse_ddg(page, count)
        if results:
            return results
    return []


def _searxng(base: str, query: str, count: int) -> list[dict[str, str]]:
    import json
    import urllib.parse
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as resp:
        data = json.loads(resp.read().decode())
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in data.get("results", [])[:count]]


def search(settings, query: str, count: int = 5) -> list[dict[str, str]]:
    base = settings.get("research.searxng_url") or ""
    if settings.get("research.engine", "duckduckgo") == "searxng" and base:
        return _searxng(base, query, count)
    return _ddg(query, count)


@partial("web_search", "Searches the web and returns the top results (title, address and a snippet).",
         args=[Arg("query", "string", "What to search for"),
               Arg("count", "integer", "How many results", required=False, default=5)],
         how="DuckDuckGo by default (no account or key), or your own SearXNG instance (Settings > Listening "
             "& Keys > Research).",
         returns="list of {title, url, snippet}", category="web")
def web_search(ctx, query, count=5):
    ctx.state("researching", f"Searching: {query}")
    ctx.think(f"Searching the web for: {query}", looking_at=f"search: {query}")
    try:
        results = search(ctx.settings, str(query), int(count))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise FunctionError(f"web search failed: {exc}") from exc
    for r in results:
        ctx.think(f"- {r['title']} ({r['url']})")
    return results
