"""Speak, Ask User, Wait/Delay, Local model text generation."""
from __future__ import annotations

from ...util import parse_duration
from ..base import Arg, FunctionError, partial


@partial("speak", "Says something out loud in the agent's voice (and shows it on screen if enabled).",
         args=[Arg("text", "string", "What to say")], category="interaction")
def speak(ctx, text):
    ctx.say(str(text))
    return str(text)


@partial("ask_user", "Asks the user a question and waits for the spoken or typed answer.",
         args=[Arg("question", "string", "The question"),
               Arg("choices", "list", "Allowed answers, if any", required=False, default=None),
               Arg("timeout", "duration", "How long to wait", required=False, default="60s")],
         how="The indicator flashes white while waiting. The next thing you say (no wake word needed) or type "
             "is the answer.",
         returns="the answer text, or empty if nobody answered", category="interaction")
def ask_user(ctx, question, choices=None, timeout="60s"):
    answer = ctx.ask(str(question), choices=choices, timeout=parse_duration(timeout))
    return answer or ""


@partial("wait", "Waits for a length of time before continuing.",
         args=[Arg("time", "duration", "How long to wait")], category="flow")
def wait(ctx, time):
    seconds = parse_duration(time)
    ctx.wait(seconds)
    return seconds


@partial("generate_text", "Has the local response model write text from a prompt (no tools, no actions).",
         args=[Arg("prompt", "string", "Instructions for the model"),
               Arg("system", "string", "Extra system instructions", required=False, default="")],
         how="Uses the Local AI Model for Full Responses with the agent's default prompt.",
         returns="the generated text", category="interaction")
def generate_text(ctx, prompt, system=""):
    if ctx.dry_run:
        return f"<text generated for: {prompt}>"
    reply = ctx.engine.models.respond(ctx.agent, str(prompt), system=str(system or ""), ctx=ctx)
    if reply is None:
        raise FunctionError("no local response model is loaded")
    return reply
