"""Arithmetic, worked out rather than looked up: "what's 2+2", "fifteen percent of eighty", "square root of
144", "12 times 7 divided by 3", "2 to the power of 10".

Spoken words become an expression (number words included), which is evaluated with a small, safe
evaluator (numbers, + - * / // % **, brackets and a few math functions -- never Python's eval).
"""
from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any

SMALL = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000,
          "trillion": 1_000_000_000_000}
ORDINAL_POWERS = {"squared": "**2", "cubed": "**3"}

PHRASES = [   # longest first: spoken operators -> symbols
    (r"\bto the power of\b|\braised to(?: the power of)?\b|\bto the\b(?=\s*\d)", " ** "),
    (r"\bmultiplied by\b|\btimes\b|(?<=\d)\s*x\s*(?=\d)|×", " * "),
    (r"\bdivided by\b|\bover\b|÷", " / "),
    (r"\bplus\b|\band\b(?=\s*\d)", " + "),
    (r"\bminus\b|\btake away\b|\bless\b", " - "),
    (r"\bmod(?:ulo)?\b", " % "),
    (r"\bpercent of\b|% of\b", " /100* "),
    (r"\bpercent\b|%", " /100 "),
    (r"\bsquare root of\b|\broot of\b|√", " sqrt "),
    (r"\bcube root of\b", " cbrt "),
    (r"\bfactorial of\b", " factorial "),
    (r"\b(?:the )?absolute value of\b", " abs "),
    (r"\blog(?:arithm)? of\b", " log10 "),
    (r"\bnatural log(?:arithm)? of\b|\bln of\b", " ln "),
    (r"\bsine of\b|\bsin of\b", " sin "),
    (r"\bcosine of\b|\bcos of\b", " cos "),
    (r"\btangent of\b|\btan of\b", " tan "),
    (r"\bpoint\b", " . "),
]
LEAD = re.compile(r"^(?:hey\s+)?(?:what(?:'s| is)|whats|how much is|how much's|calculate|compute|work out|"
                  r"solve|evaluate|tell me|can you (?:tell me|calculate|work out)|do you know)\s+", re.I)
FUNCS = {"sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x), "abs": abs,
         "factorial": lambda x: math.factorial(int(x)) if float(x).is_integer() and 0 <= x <= 170 else _bad(),
         "log10": math.log10, "ln": math.log, "log": math.log10, "sin": lambda d: math.sin(math.radians(d)),
         "cos": lambda d: math.cos(math.radians(d)), "tan": lambda d: math.tan(math.radians(d)),
         "round": round, "floor": math.floor, "ceil": math.ceil}
CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
       ast.USub: operator.neg, ast.UAdd: operator.pos}


class MathError(ValueError):
    pass


def _bad() -> Any:
    raise MathError("that isn't something I can work out")


def _words_to_numbers(text: str) -> str:
    """"two hundred and fifty three" -> "253" (words that aren't numbers are left as they are)."""
    out: list[str] = []
    cur: int | None = None
    total = 0
    low = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text.lower())      # "twenty-one"
    for tok in re.findall(r"[a-z]+|\d+(?:\.\d+)?|\*\*|\S", low):
        if tok in SMALL or tok in TENS:
            cur = (cur or 0) + SMALL.get(tok, TENS.get(tok, 0))
            continue
        if tok == "hundred" and cur is not None:
            cur *= 100
            continue
        if tok in SCALES and (cur is not None or total):
            total += (cur or 1) * SCALES[tok]
            cur = None
            continue
        if tok == "and" and (cur is not None or total) and out[-1:] != ["+"]:
            continue                                     # "two hundred and five"
        if cur is not None or total:
            out.append(str(total + (cur or 0)))
            cur, total = None, 0
        out.append(tok)
    if cur is not None or total:
        out.append(str(total + (cur or 0)))
    return " ".join(out)


def to_expression(text: str) -> str | None:
    """The spoken request as an arithmetic expression, or None when it isn't one."""
    t = text.strip().strip("?.! ")
    t = LEAD.sub("", t).strip()
    t = re.sub(r"^(?:the answer to|the result of)\s+", "", t, flags=re.I)
    t = re.sub(r"\s*(?:equal|equals|make|is)\s*$", "", t, flags=re.I)
    if not t:
        return None
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)            # 1,000
    low = " " + t.lower() + " "
    for word, sym in ORDINAL_POWERS.items():
        low = re.sub(rf"\b{word}\b", f" {sym} ", low)
    for pat, sym in PHRASES:
        low = re.sub(pat, sym, low, flags=re.I)
    low = _words_to_numbers(low)
    low = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", low)      # "3 . 5" -> 3.5
    low = low.replace("^", "**")
    # functions given a bare number: "sqrt 144" -> sqrt(144)
    low = re.sub(r"\b(sqrt|cbrt|abs|factorial|log10|ln|sin|cos|tan)\s+(-?[\d.]+|pi|e)\b", r"\1(\2)", low)
    low = re.sub(r"\s+", " ", low).strip()
    if not re.fullmatch(r"[\d\s.+\-*/%()]*(?:\b(?:sqrt|cbrt|abs|factorial|log10|ln|sin|cos|tan|pi|e|tau)\b"
                        r"[\d\s.+\-*/%()]*)*", low):
        return None
    if not re.search(r"\d", low) or not re.search(r"[+\-*/%]|\b(?:sqrt|cbrt|abs|factorial|log10|ln|sin|cos|tan)\b", low):
        return None                                      # a bare number isn't a sum
    return low


def evaluate(expr: str) -> float | int:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise MathError("I couldn't make a sum out of that") from exc

    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name) and n.id in CONSTS:
            return CONSTS[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 1000:
                raise MathError("that power is too big to work out")
            return OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in OPS:
            return OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FUNCS and len(n.args) == 1:
            return FUNCS[n.func.id](ev(n.args[0]))
        raise MathError("I couldn't make a sum out of that")
    try:
        return ev(tree)
    except ZeroDivisionError as exc:
        raise MathError("that divides by zero") from exc
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, MathError):
            raise
        raise MathError(f"that can't be worked out ({exc})") from exc


def pretty(x: float | int) -> str:
    if isinstance(x, bool):
        return str(x)
    if isinstance(x, int) or (isinstance(x, float) and x.is_integer() and abs(x) < 1e15):
        return f"{int(x):,}"
    if abs(x) >= 1e15 or (abs(x) < 1e-6 and x != 0):
        return f"{x:.6g}"
    return f"{x:,.6f}".rstrip("0").rstrip(".")


def looks_like_math(text: str) -> bool:
    return to_expression(text) is not None
