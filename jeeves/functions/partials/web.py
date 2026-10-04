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
