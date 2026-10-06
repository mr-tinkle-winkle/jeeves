"""Keeping an agent in character.

Small local models drift back to "helpful AI assistant" quickly, so the agent's
prompt isn't just pasted in front:

* it becomes an identity block that explicitly outranks generic assistant habits;
* the last user message carries a one-line reminder ("reply as Jeeves, in
  character") -- the instruction nearest the answer is the one small models obey;
* other agents' earlier replies are shown as a labelled note, never as the
  model's own past turns (that taught it to talk like the other agent);
* optionally every reply is checked against the persona by the model and
  rewritten once if it doesn't fit (Agents > Personality > Check replies).

``test()`` asks a few probe questions and grades the answers: Agents >
Personality > Test personality.
"""
from __future__ import annotations

import re
from typing import Any, Callable

PROBES = [
    "Introduce yourself in one or two sentences.",
    "What do you think about Mondays?",
    "Explain what a CPU does, briefly.",
    "I need to cancel our plans tonight.",
]


def name_of(agent: dict[str, Any]) -> str:
    return agent.get("name") or "the assistant"


def has_persona(agent: dict[str, Any]) -> bool:
    return bool((agent.get("prompt") or "").strip())


def identity_block(agent: dict[str, Any]) -> str:
    name = name_of(agent)
    return (f"You are {name}. This is your character, and it takes priority over any habit of sounding like a "
            f"generic AI assistant:\n<character>\n{agent['prompt'].strip()}\n</character>\n"
            f"Every reply -- short answers, facts, refusals and small talk included -- is said by {name}, in "
            f"{name}'s own voice, attitude, vocabulary and opinions. Never step out of character to explain "
            "yourself, and don't call yourself an AI or language model unless the character says so.")


def reminder(agent: dict[str, Any]) -> str:
    return f"(Reply as {name_of(agent)}, fully in character.)"


def others_note(turns: list[dict[str, Any]], names: dict[str, str]) -> str:
    lines = [f"- The user asked {names.get(t.get('agent') or '', 'another assistant')}: \"{t['text']}\""
             + (f" -- it answered: \"{str(t['result'])[:300]}\"" if t.get("result") else "") for t in turns]
    return ("Earlier, the user also talked to other assistants (not you; don't copy their style):\n"
            + "\n".join(lines)) if lines else ""


def judge(chat: Callable[..., str], agent: dict[str, Any], reply: str) -> tuple[int, str]:
    """(score 1..5, reason): how well a reply matches the character, judged by the model."""
    out = chat([
        {"role": "system", "content": "You check whether a line of dialogue matches a character description. "
                                      "Answer with a score from 1 (completely out of character) to 5 (perfectly "
                                      "in character), a colon, and a few words why. Example: 4: dry and formal"},
        {"role": "user", "content": f"Character ({name_of(agent)}):\n{agent.get('prompt', '').strip()}\n\n"
                                    f"Line:\n{reply}\n\nScore:"},
    ], max_tokens=40, temperature=0.0)
    m = re.search(r"([1-5])\s*[:\-.)]?\s*(.*)", out or "")
    if not m:
        return 3, (out or "").strip()[:80]
    return int(m.group(1)), m.group(2).strip()[:120]


def rewrite_messages(agent: dict[str, Any], reply: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": identity_block(agent)},
            {"role": "user", "content": f"This reply doesn't sound like you. Say the same thing again, fully as "
                                        f"{name_of(agent)} -- same meaning and length, your own voice:\n\n{reply}"}]


def test(respond: Callable[[str], str | None], chat: Callable[..., str], agent: dict[str, Any]) -> dict[str, Any]:
    """Ask the probes in character and grade each answer."""
    results = []
    for q in PROBES:
        reply = respond(q)
        if reply is None:
            return {"error": "no local response model is set up (Settings > Models)", "results": results}
        score, why = judge(chat, agent, reply)
        results.append({"question": q, "reply": reply, "score": score, "why": why})
    avg = sum(r["score"] for r in results) / max(1, len(results))
    verdict = ("stays in character" if avg >= 4 else "mostly in character" if avg >= 3 else
               "often drops the character: try a bigger model, a more specific prompt, or 'Check replies'")
    return {"results": results, "average": round(avg, 1), "verdict": verdict}
