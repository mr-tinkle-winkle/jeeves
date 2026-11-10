"""Organize Thoughts: an agent's thoughts as a readable view instead of one run of text.

A request's thoughts are a list of strings with a kind each (``FunctionContext.think``):
``intent`` (the function it chose, as the intent model wrote it), ``reasoning`` (a thinking model's
reasoning), ``answer`` (the reply as it is written) and plain notes -- the steps functions report
("Searching the web for: ...", "Reading https://...", "  found wumpy on this page").

``organize`` turns them into sections (what it decided, what it thought, each search with its results,
each page read with what came of it, the answer); ``render`` draws those as HTML for the Thoughts
window. Nothing here needs Qt, so it can be tested on its own.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urlparse

READ = re.compile(r"^(Reading the wiki|Reading Wikipedia article|Reading Wikipedia|Reading|Opening|Following a link|"
                  r"Checking your source)\s*:?\s+(.+)$")
RESULT = re.compile(r"^-\s+(.*?)\s*\((https?://[^\s)]+)\)\s*$")
DONE = re.compile(r"(^That answers it|^Your source answers it|done reading\.?$)", re.I)


def _pretty(name: str) -> str:
    return (name or "").replace("_", " ").strip().capitalize()


def _decision(text: str) -> dict[str, Any]:
    """The intent model's JSON ({"function": ..., "args": {...}}), or what there is of it so far."""
    raw = text.strip()
    try:
        data = json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(m.group(0)) if m else None
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return {"type": "decision", "function": "", "args": {}, "raw": raw}
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    extra = {k: v for k, v in data.items() if k not in ("function", "args") and v not in (None, "", [], {})}
    return {"type": "decision", "function": str(data.get("function") or ""), "args": args, "extra": extra,
            "raw": raw}


def organize(thoughts: list[str], kinds: list[str] | None = None) -> list[dict[str, Any]]:
    """Sections, in order: {"type": decision | thinking | answer | plan | context | search | read |
    done | note, ...}."""
    kinds = list(kinds or [])
    kinds += [""] * (len(thoughts) - len(kinds))
    out: list[dict[str, Any]] = []

    def last(*types: str) -> dict[str, Any] | None:
        return out[-1] if out and out[-1]["type"] in types else None

    for text, kind in zip(thoughts, kinds):
        if not str(text).strip():
            continue
        text = str(text)
        if kind == "intent" or (not kind and text.lstrip().startswith('{"function"')):
            out.append(_decision(text))
            continue
        if kind in ("reasoning", "answer"):
            typ = "thinking" if kind == "reasoning" else "answer"
            prev = last(typ)
            if prev is not None:
                prev["text"] += text
            else:
                out.append({"type": typ, "text": text})
            continue
        for line in text.split("\n"):
            if not line.strip():
                continue
            indented = line.startswith((" ", "\t"))
            body = line.strip()
            if indented and out and out[-1]["type"] in ("read", "context", "search", "plan"):
                out[-1].setdefault("notes", []).append(body)
                continue
            m = RESULT.match(body)
            if m and last("search") is not None:
                out[-1]["results"].append({"title": m.group(1), "url": m.group(2)})
                continue
            if body.startswith("Question:"):
                out.append({"type": "plan", "question": body[9:].strip(), "notes": []})
            elif body.startswith(("Context:", "To look up:")) and last("plan") is not None:
                key, val = body.split(":", 1)
                out[-1]["context" if key == "Context" else "terms"] = val.strip()
            elif body.startswith("Searching the web for:"):
                out.append({"type": "search", "query": body.split(":", 1)[1].strip(), "results": [], "notes": []})
            elif body.startswith("Searching:"):
                continue                              # (each of its searches follows with its results)
            elif body.startswith("First, what is "):
                out.append({"type": "context", "subject": body[15:].rstrip("?").strip(), "notes": []})
            elif DONE.search(body):
                out.append({"type": "done", "text": body})
            elif READ.match(body):
                m = READ.match(body)
                out.append({"type": "read", "how": m.group(1), "target": m.group(2).strip(), "notes": []})
            else:
                prev = last("note")
                if prev is not None and not body.startswith(("Probably:", "Not answered yet", "Character check")):
                    prev["lines"].append(body)
                else:
                    out.append({"type": "note", "lines": [body]})
    return out


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except ValueError:
        return ""


def _luminance(color: str) -> float:
    c = color.lstrip("#")
    try:
        rgb = [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    except ValueError:
        return 0.5
    lin = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _value(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def render(sections: list[dict[str, Any]], accent: str = "#4ea1ff", muted: str = "#8a8f98",
           text: str = "#e8e8e8", card: str = "#22252b", live: bool = False) -> str:
    """The sections as HTML (Qt's rich text: tables and inline styles only)."""
    esc = html.escape
    # a theme's accent can be close to its card color: labels and links must still be readable on it
    accent = accent if _contrast(accent, card) >= 3.0 else text

    def link(url: str, label: str) -> str:
        return f'<a href="{esc(url)}" style="color:{accent};text-decoration:none">{esc(label)}</a>'

    def target(t: str) -> str:
        if t.startswith(("http://", "https://")):
            path = urlparse(t).path.rstrip("/")
            tail = path.rsplit("/", 1)[-1].replace("_", " ") if path else ""
            return link(t, _host(t) + (f" › {tail}" if tail else ""))
        return f"<b>{esc(t)}</b>"

    def notes(sec: dict[str, Any]) -> str:
        return "".join(f'<div style="color:{muted};margin-left:4px">{esc(n)}</div>' for n in sec.get("notes") or [])

    def card_html(label: str, body: str, bar: str = "", dim: bool = False) -> str:
        bar = bar or accent
        return (f'<table width="100%" cellspacing="0" cellpadding="0" style="margin-bottom:8px"><tr>'
                f'<td width="4" bgcolor="{bar}"></td>'
                f'<td bgcolor="{card}" style="padding:7px 10px">'
                f'<div style="color:{bar};font-size:10px;font-weight:700;letter-spacing:1px">{esc(label.upper())}</div>'
                f'<div style="color:{muted if dim else text}">{body}</div></td></tr></table>')

    parts: list[str] = []
    for sec in sections:
        typ = sec["type"]
        if typ == "decision":
            if not sec.get("function"):
                body = f'<span style="font-family:monospace">{esc(sec.get("raw", ""))}</span>'
            else:
                rows = "".join(f'<tr><td style="color:{muted};padding-right:10px">{esc(_pretty(k))}</td>'
                               f'<td>{esc(_value(v))}</td></tr>'
                               for k, v in list(sec.get("args", {}).items()) + list((sec.get("extra") or {}).items()))
                body = f'<b style="font-size:14px">{esc(_pretty(sec["function"]))}</b>' + \
                    (f'<table cellspacing="0" cellpadding="1" style="margin-top:3px">{rows}</table>' if rows else "")
            parts.append(card_html("Decided to use", body))
        elif typ == "thinking":
            body = f'<i>{esc(sec["text"].strip()).replace(chr(10), "<br>")}</i>'
            parts.append(card_html("Thinking", body, bar=muted, dim=True))
        elif typ == "answer":
            parts.append(card_html("Answer", esc(sec["text"].strip()).replace("\n", "<br>")))
        elif typ == "plan":
            body = esc(sec.get("question", ""))
            for key, lab in (("context", "About"), ("terms", "To look up")):
                if sec.get(key):
                    body += f'<br><span style="color:{muted}">{lab}:</span> {esc(sec[key])}'
            parts.append(card_html("Question", body + notes(sec)))
        elif typ == "context":
            parts.append(card_html("Learning the context", f'What is <b>{esc(sec["subject"])}</b>?' + notes(sec)))
        elif typ == "search":
            n = len(sec["results"])
            body = f'<b>{esc(sec["query"])}</b> <span style="color:{muted}">· {n} result{"" if n == 1 else "s"}</span>'
            body += "".join(f'<div style="margin-left:4px">{link(r["url"], r["title"] or r["url"])} '
                            f'<span style="color:{muted};font-size:10px">{esc(_host(r["url"]))}</span></div>'
                            for r in sec["results"])
            parts.append(card_html("Searched", body + notes(sec)))
        elif typ == "read":
            how = {"Reading": "Read", "Reading the wiki": "Read the wiki", "Opening": "Opened",
                   "Following a link": "Followed a link", "Checking your source": "Checked your source",
                   "Reading Wikipedia": "Read Wikipedia", "Reading Wikipedia article": "Read Wikipedia"}
            parts.append(card_html(how.get(sec["how"], sec["how"]), target(sec["target"]) + notes(sec)))
        elif typ == "done":
            parts.append(f'<div style="color:{accent};font-weight:700;margin:2px 0 8px 2px">✓ {esc(sec["text"])}</div>')
        else:
            body = "<br>".join(esc(ln) for ln in sec["lines"])
            parts.append(f'<div style="color:{muted};margin:2px 0 8px 2px">{body}</div>')
    if not parts:
        return f'<div style="color:{muted}">{"Nothing yet…" if live else "No thoughts were recorded."}</div>'
    return "".join(parts)
