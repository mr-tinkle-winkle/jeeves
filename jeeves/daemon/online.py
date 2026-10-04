"""Online Prompt Mode providers.

* GPT    -> Codex CLI (``codex exec``), signed in with a ChatGPT account.
* Gemini -> Google AI Studio API key (free tier).
* Claude, Grok -> the Jeeves browser: a separate browser profile that you log
  into once. Jeeves opens it with the request filled in; you press send and
  then the site's copy button; Jeeves reads the answer from the clipboard.
  Jeeves never presses send or copy itself.

Each provider is its own class so a changed free tier only means swapping one.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import config, paths
from ..functions.base import FunctionError
from ..util import graphical_env

BROWSER_SITES = {
    "claude": {"login": "https://claude.ai/login", "prompt": "https://claude.ai/new?q={q}"},
    "grok": {"login": "https://grok.com/", "prompt": "https://grok.com/?q={q}"},
}
CHROMIUM_LIKE = ["chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "brave", "vivaldi"]


class Provider:
    name = ""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "ready": False, "detail": ""}

    def ask(self, prompt: str, ctx: Any) -> str:
        raise NotImplementedError


class Codex(Provider):
    name = "codex"

    def _exe(self) -> str | None:
        return shutil.which(self.settings.get("accounts.codex.binary", "codex") or "codex")

    def status(self) -> dict[str, Any]:
        exe = self._exe()
        if not exe:
            return {"name": self.name, "ready": False, "detail": "Codex CLI isn't installed"}
        try:
            out = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=10)
            text = (out.stdout + out.stderr).strip()
            ok = out.returncode == 0 and "not logged in" not in text.lower()
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, text = False, str(exc)
        return {"name": self.name, "ready": ok, "detail": text or ("signed in" if ok else "run `codex login`")}

    def ask(self, prompt: str, ctx: Any) -> str:
        exe = self._exe()
        if not exe:
            raise FunctionError("Codex CLI isn't installed (Settings > Accounts)")
        with tempfile.TemporaryDirectory(prefix="jeeves-codex-") as tmp:
            last = Path(tmp) / "answer.txt"
            cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--output-last-message",
                   str(last), prompt]
            try:
                proc = subprocess.Popen(cmd, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        stdin=subprocess.DEVNULL)
            except OSError as exc:
                raise FunctionError(f"couldn't start Codex: {exc}") from exc
            assert proc.stdout is not None
            for line in proc.stdout:          # progress -> the indicator's thoughts view
                ctx.think(line, append=True)
                if ctx.is_cancelled():
                    proc.terminate()
                    break
            proc.wait()
            if last.exists() and last.read_text().strip():
                return last.read_text().strip()
            if proc.returncode != 0:
                raise FunctionError("Codex failed (is it signed in? run `codex login`)")
            raise FunctionError("Codex returned nothing")


class Gemini(Provider):
    name = "gemini"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def status(self) -> dict[str, Any]:
        key = config.get_secret("gemini_api_key")
        return {"name": self.name, "ready": bool(key),
                "detail": "API key saved" if key else "add a free Google AI Studio API key"}

    def ask(self, prompt: str, ctx: Any) -> str:
        key = config.get_secret("gemini_api_key")
        if not key:
            raise FunctionError("no Gemini API key (Settings > Accounts)")
        model = self.settings.get("accounts.gemini.model", "gemini-2.5-flash")
        system = ctx.agent.get("prompt") or ""
        body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        req = urllib.request.Request(self.URL.format(model=urllib.parse.quote(model)),
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise FunctionError(f"Gemini refused the request ({exc.code}): {detail}") from exc
        except (urllib.error.URLError, ValueError) as exc:
            raise FunctionError(f"couldn't reach Gemini: {exc}") from exc
        try:
            return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
        except (KeyError, IndexError) as exc:
            raise FunctionError(f"Gemini returned no answer: {json.dumps(data)[:200]}") from exc


class JeevesBrowser:
    """A dedicated browser profile under ~/.local/share/jeeves/browser."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    @property
    def profile(self) -> Path:
        return paths.data_dir() / "browser"

    def binary(self) -> tuple[str | None, str]:
        chosen = self.settings.get("accounts.browser.binary") or ""
        if chosen:
            exe = shutil.which(chosen)
            return exe, "firefox" if "firefox" in chosen or "librewolf" in chosen else "chromium"
        for n in CHROMIUM_LIKE:
            exe = shutil.which(n)
            if exe:
                return exe, "chromium"
        for n in ("firefox", "librewolf"):
            exe = shutil.which(n)
            if exe:
                return exe, "firefox"
        return None, ""

    def open(self, url: str) -> None:
        exe, family = self.binary()
        if not exe:
            raise FunctionError("no browser found for the Jeeves browser (Settings > Accounts)")
        self.profile.mkdir(parents=True, exist_ok=True)
        if family == "firefox":
            cmd = [exe, "--profile", str(self.profile), "--no-remote", "--new-window", url]
        else:
            cmd = [exe, f"--user-data-dir={self.profile}", "--new-window", url]
        subprocess.Popen(cmd, env=graphical_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)


class BrowserSite(Provider):
    def __init__(self, settings: Any, name: str, browser: JeevesBrowser) -> None:
        super().__init__(settings)
        self.name = name
        self.browser = browser

    def status(self) -> dict[str, Any]:
        exe, _ = self.browser.binary()
        logged = bool(self.settings.get(f"accounts.{self.name}.enabled"))
        if not exe:
            return {"name": self.name, "ready": False, "detail": "no browser found"}
        return {"name": self.name, "ready": logged,
                "detail": "logged in to the Jeeves browser" if logged else "log in through Settings > Accounts"}

    def login(self) -> None:
        self.browser.open(BROWSER_SITES[self.name]["login"])

    def ask(self, prompt: str, ctx: Any) -> str:
        from ..functions.partials.desktop import clipboard_get
        site = self.name.title()
        if not ctx.wait_for_click(f"Click to open {site} with your request", timeout=300):
            raise FunctionError(f"{site} wasn't opened")
        before = _safe(clipboard_get)
        self.browser.open(BROWSER_SITES[self.name]["prompt"].format(q=urllib.parse.quote(prompt)))
        timeout = float(self.settings.get("accounts.browser.clipboard_timeout_seconds", 300))
        ctx.state("asking", f"In {site}: press send, then the copy button under the answer")
        deadline = time.time() + timeout
        while time.time() < deadline:
            ctx.check_cancelled()
            ctx.wait(0.5)
            now = _safe(clipboard_get)
            if now and now != before and now.strip() != prompt.strip():
                return now.strip()
        raise FunctionError(f"nothing was copied from {site} in time")


def _safe(fn) -> str:
    try:
        return fn()
    except Exception:
        return ""


class Online:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.browser = JeevesBrowser(settings)
        self.providers: dict[str, Provider] = {
            "codex": Codex(settings), "gemini": Gemini(settings),
            "claude": BrowserSite(settings, "claude", self.browser),
            "grok": BrowserSite(settings, "grok", self.browser),
        }

    def ask(self, name: str, prompt: str, ctx: Any) -> str:
        p = self.providers.get(name)
        if p is None:
            raise FunctionError(f"unknown online AI '{name}'")
        if self.settings.get("functions.online_use_mcp") and name == "codex":
            ctx.think("MCP: Codex CLI uses the MCP servers configured in ~/.codex/config.toml")
        return p.ask(prompt, ctx)

    def status(self) -> list[dict[str, Any]]:
        return [p.status() for p in self.providers.values()]

    def login(self, name: str) -> None:
        p = self.providers.get(name)
        if isinstance(p, BrowserSite):
            p.login()
        elif name == "codex":
            exe = shutil.which(self.settings.get("accounts.codex.binary", "codex") or "codex")
            if not exe:
                raise FunctionError("Codex CLI isn't installed")
            term = next((t for t in ("konsole", "kitty", "alacritty", "foot", "wezterm", "xterm") if shutil.which(t)), None)
            if term is None:
                raise FunctionError("run `codex login` in a terminal")
            flag = "-e"
            subprocess.Popen([term, flag, exe, "login"], env=graphical_env(), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            raise FunctionError(f"{name} uses an API key, not a login")


__all__ = ["Online"]
