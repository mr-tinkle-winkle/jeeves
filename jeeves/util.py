"""Small helpers shared by the daemon, functions and CLI (stdlib only)."""
from __future__ import annotations

import datetime as _dt
import difflib
import os
import re
import shutil
import subprocess
import time
from typing import Any

_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?|s|sec|secs|seconds?|ms)?", re.I)
_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
            "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "half": 0.5}


def parse_duration(value: Any) -> float:
    """'5 minutes' / '1h30m' / '90' / 'half an hour' / 2.5 -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        raise ValueError("empty duration")
    text = text.replace("half an hour", "30 minutes").replace("an hour", "1 hour").replace("a minute", "1 minute")
    for word, num in sorted(_WORDNUM.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"\b{re.escape(word)}\b", str(num), text)
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    m = re.fullmatch(r"(\d+):(\d{2})(?::(\d{2}))?", text)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
        return a * 3600 + b * 60 + int(c) if c else a * 60 + b
    total, found = 0.0, False
    for num, unit in _DUR.findall(text):
        found = True
        n = float(num)
        u = (unit or "s").lower()
        if u.startswith("h"):
            total += n * 3600
        elif u == "ms":
            total += n / 1000
        elif u.startswith("m"):
            total += n * 60
        else:
            total += n
    if not found:
        raise ValueError(f"can't read a duration from {value!r}")
    return total


def parse_clock(value: str, now: _dt.datetime | None = None) -> _dt.datetime:
    """'7:30 pm', '19:30', 'tomorrow 9am', 'in 10 minutes' -> next matching datetime."""
    now = now or _dt.datetime.now()
    text = value.strip().lower()
    if text.startswith("in "):
        return now + _dt.timedelta(seconds=parse_duration(text[3:]))
    day = 0
    if "tomorrow" in text:
        day, text = 1, text.replace("tomorrow", "").strip()
    text = text.replace("at ", "").strip()
    if text in ("noon", "midday"):
        text = "12:00"
    elif text == "midnight":
        text = "0:00"
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", text)
    if not m:
        raise ValueError(f"can't read a time from {value!r}")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + _dt.timedelta(days=day)
    if target <= now and day == 0:
        target += _dt.timedelta(days=1)
    return target


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def best_match(needle: str, options: list[str], cutoff: float = 0.6) -> str | None:
    if not options:
        return None
    lowered = {o.lower(): o for o in options}
    if needle.lower() in lowered:
        return lowered[needle.lower()]
    scored = sorted(((similarity(needle, o), o) for o in options), reverse=True)
    return scored[0][1] if scored[0][0] >= cutoff else None


def which(*names: str) -> str | None:
    for n in names:
        if not n:
            continue
        p = shutil.which(n)
        if p:
            return p
    return None


def run(cmd: list[str], timeout: float = 10, input_text: str | None = None,
        check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text, check=check)


def desktop() -> str:
    """'kde' | 'hyprland' | 'other'."""
    sync_graphical_env()
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    cur = (os.environ.get("XDG_CURRENT_DESKTOP", "") + os.environ.get("DESKTOP_SESSION", "")).lower()
    if "kde" in cur or "plasma" in cur:
        return "kde"
    if "hyprland" in cur:
        return "hyprland"
    return "other"


def graphical_env() -> dict[str, str]:
    """The current env plus the graphical session's display variables from the
    systemd user manager (the daemon may start before the desktop)."""
    env = dict(os.environ)
    if env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
        return env
    try:
        out = run(["systemctl", "--user", "show-environment"], timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return env
    for ln in out.splitlines():
        k, _, v = ln.partition("=")
        if k in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
                 "HYPRLAND_INSTANCE_SIGNATURE", "QT_QPA_PLATFORMTHEME", "DBUS_SESSION_BUS_ADDRESS") \
                and k not in env:
            env[k] = v
    return env


_synced = 0.0


def sync_graphical_env() -> None:
    """Copy the desktop's display variables into this process's environment once the
    desktop is up. The daemon is a user service that often starts before Plasma or
    Hyprland: without this it believed it was on an unknown desktop, and spectacle,
    kdotool and grim ran without WAYLAND_DISPLAY -- so it couldn't read the screen or
    find the mouse."""
    global _synced
    now = time.monotonic()
    if not os.environ.get("INVOCATION_ID"):       # only systemd services miss the desktop's env
        return
    if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_CURRENT_DESKTOP") or now - _synced < 5:
        return
    _synced = now
    try:
        out = run(["systemctl", "--user", "show-environment"], timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    for ln in out.splitlines():
        k, _, v = ln.partition("=")
        if k in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
                 "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "HYPRLAND_INSTANCE_SIGNATURE",
                 "DBUS_SESSION_BUS_ADDRESS") and v and not os.environ.get(k):
            os.environ[k] = v


def speakable(text: str) -> str:
    """Text as it should be said: models write markdown, links and citations even when told not to,
    and a voice reading out "asterisk asterisk" or a whole URL sounds broken. The text shown on
    screen keeps them."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)                     # code blocks aren't read out
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)                          # images
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)                       # [text](link) -> text
    t = re.sub(r"https?://(?:www\.)?([^/\s)]+)[^\s)]*", r"\1", t)         # a link -> its site
    t = re.sub(r"\s*\[\d+(?:\s*[,–-]\s*\d+)*\]", "", t)                   # citations [2]
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)                              # **bold**
    t = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", t)   # *italic*
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)                    # headings
    t = re.sub(r"^\s*(?:[-*•+]|\d{1,2}[.)])\s+", "", t, flags=re.M)         # list markers
    t = re.sub(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$", "", t, flags=re.M)   # table rules
    t = re.sub(r"^[ \t]*\|(.*?)\|?[ \t]*$", r"\1", t, flags=re.M)        # | a | b | rows
    t = re.sub(r"[ \t]*\|[ \t]*", ", ", t)
    t = re.sub("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\uFE0F]", "", t)   # emoji
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    # a list item or heading without punctuation still ends where it ends
    t = " ".join(ln if re.search(r"[.!?:;,…]$", ln) else ln + "." for ln in lines)
    return re.sub(r"\s{2,}", " ", t).strip()


def truncate(text: str, n: int = 400) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# What a model needs to see besides the request
# ---------------------------------------------------------------------------

_BACK = re.compile(r"\b(you (just )?said|said that|that again|(i|you) just (made|said|did|played|opened|asked)|the one "
                   r"(i|you)|that one|the same|the last one|the previous|earlier|instead|tell me more|more about "
                   r"(it|that|this|them)|why('s| is| was)? (that|it)|how come|go on|elaborate|explain (that|it|this)|what "
                   r"do you mean|what about|how about|what else|such as|like what)\b", re.I)
_PRONOUN = re.compile(r"\b(it|that|this|those|these|them|they|he|she|him|her|there|one|again)\b", re.I)


def refers_back(text: str) -> bool:
    """Whether a request leans on what was said before ("why is that?", "play it again", "and the
    second one?", "is he still alive?"). Only then are earlier turns worth showing a model: otherwise
    a small model answers them, or mixes them into the answer, instead of the question. A whole
    question that only uses a pronoun for something it names itself ("... on their first try") doesn't."""
    t = text.strip().lower()
    words = re.findall(r"[\w']+", t)
    if not words:
        return False
    if words[0] in ("and", "but", "so", "also", "then", "or") or _BACK.search(t):
        return True
    if re.search(r"\bwhat (time|day|date|year|month) is it\b|\bis it (raining|sunny|cold|hot|late)\b", t):
        return False                                  # "it" with nothing to refer to
    if len(words) <= 2 and words[0] in ("why", "really", "how", "what", "huh", "seriously", "wait"):
        return True                                   # "why?", "really?", "how so?"
    return len(words) <= 6 and bool(_PRONOUN.search(t)) and len(content_words(t)) <= 2


_COMMON = set("the a an and or of to in on for with what which who whom whose when where why how is are was "
              "were be been being do does did can could should would will shall may might must i me my mine you "
              "your yours it its this that these those there here about from at by as into than then them they "
              "we our us he him his she her please tell know find out get got have has had just like some any all "
              "not no yes so if but one thing things something anything really very much more most also too"
              .split())


def content_words(text: str) -> set[str]:
    """The words that carry meaning ("my dog's name" -> {dog, name}), plurals folded."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) > 2 and w not in _COMMON:
            out.add(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
    return out


# ---------------------------------------------------------------------------
# How alike two words sound (no pronunciation dictionary needed)
# ---------------------------------------------------------------------------

_SOUND_RULES = [
    (r"[^a-z]", ""), (r"tch", "C"), (r"ch", "C"), (r"sh", "S"), (r"th", "T"), (r"ph", "f"), (r"gh", ""),
    (r"ck", "k"), (r"qu", "kw"), (r"x", "ks"), (r"^wh", "w"), (r"^wr", "r"), (r"^kn", "n"), (r"mb$", "m"),
    (r"dg(?=[eiy])", "j"), (r"c(?=[eiy])", "s"), (r"g(?=[eiy])", "j"), (r"c", "k"),
    (r"(?<=[^aeiouIUAOV])e$", ""), (r"(?<=[^aeiouIUAOV])es$", "s"),
    (r"ee|ea|ie|ei|ey|(?<=[^aeiou])y$", "I"), (r"oo|ou|ue|ew", "U"), (r"ai|ay|a(?=[^aeiou]e)", "A"),
    (r"oa|ow|o(?=[^aeiou]e)", "O"), (r"igh|i(?=[^aeiou]e)", "Y"),
    (r"[aeiouy]+", "V"), (r"v", "f"), (r"z", "s"), (r"d$", "t"), (r"(.)\1+", r"\1"),
]
_SIMILAR_SOUNDS = {frozenset(p) for p in ("Cj", "sS", "ft", "kg", "td", "pb", "IV", "UV", "AV", "OV", "YV")}


def sound_key(word: str) -> str:
    """A rough spelling-to-sound key: 'Jeeves' and 'Jeevs' -> 'jIfs', 'cheese' -> 'CIs'."""
    w = word.lower()
    for pat, rep in _SOUND_RULES:
        w = re.sub(pat, rep, w)
    return w


def same_onset(a: str, b: str) -> bool:
    """Do the two words start with the same sound ('Jeevs'/'Geeves'/'Jeeves' do; 'Reeves', 'eves' and
    'Keeves' don't)? Mishearings keep a name's first sound far more often than not."""
    ka, kb = sound_key(a), sound_key(b)
    if not ka or not kb:
        return False
    vowels = "IUAOYV"
    return ka[0] == kb[0] or (ka[0] in vowels and kb[0] in vowels)


def sound_similarity(a: str, b: str) -> float:
    """1 = sounds the same .. 0 = nothing alike (edit distance over sound keys; close sounds cost half)."""
    return _key_similarity(sound_key(a), sound_key(b))


def ends_like(word: str, name: str) -> float:
    """How much the word -- or its ending -- sounds like the name: 'believes' ends like 'Jeeves' (the
    wake word model hears the name in it), 'gives' sounds like it."""
    kw, kn = sound_key(word), sound_key(name)
    best = _key_similarity(kw, kn)
    if len(kw) > len(kn):
        best = max(best, _key_similarity(kw[-len(kn):], kn) - 0.1)
    return best


def _key_similarity(ka: str, kb: str) -> float:
    if not ka or not kb:
        return 0.0
    prev = [float(j) for j in range(len(kb) + 1)]
    for i, ca in enumerate(ka, 1):
        cur = [float(i)] + [0.0] * len(kb)
        for j, cb in enumerate(kb, 1):
            sub = 0.0 if ca == cb else (0.5 if frozenset((ca, cb)) in _SIMILAR_SOUNDS else 1.0)
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + sub)
        prev = cur
    return 1.0 - prev[-1] / max(len(ka), len(kb))
