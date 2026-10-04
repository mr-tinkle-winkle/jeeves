# Jeeves

A local-first voice assistant for Linux (NixOS). Named agents respond to spoken or typed requests; an intent model maps each request to a function from a user-editable dictionary of composable functions, and runs it.

`SPEC.md` is the authoritative specification. Anything not in `SPEC.md` is out of scope. This README only adds context: status, decisions made while drafting the spec, and inputs required before building.

## Status

Specification only. No code exists yet.

## Inputs required before building

`SPEC.md` references two archives that are not included here and must be requested:

- **UI archive** — defines how the app should function and look.
- **Puppetry archive** — the existing keyboard/mouse macro daemon (virtual input devices, macro dictionary). Needed to decide between interfacing with Puppetry or reusing its code for Control Mode and Macros.

## Pipeline (summary of SPEC.md)

1. Wake word model detects an agent's name (or a manual text/voice request bypasses it).
2. Local STT model transcribes the request.
3. Intent model selects a function and arguments using the Dictionary, limited to the agent's enabled functions.
4. The function runs (composed from partial functions); output is spoken via the agent's TTS voice and/or shown.
5. Onscreen indicators reflect each stage (listening, transcript, thinking, researching, responding, asking for input, unclear).

## Architecture constraints (from SPEC.md)

- **Daemon + GUI split.** The daemon does all work; the GUI is a front-end that sends change requests to the daemon. The Manual Response Review popup works without the GUI open.
- **App-registered functions.** Other apps can add functions to Jeeves; imports appear as an approve/deny popup showing which default functions they use.
- **NixOS declarative settings.** Settings declared in Nix are shown as locked in the GUI.
- **User-extensible partial functions** written in Python; full functions are compositions of partials; custom functions are exportable.

## Decisions and rationale

- **Online agents.** Consumer chat services (claude.ai, chatgpt.com, Grok, Gemini web) prohibit automated access and programmatic extraction of output in their terms. Routing is therefore:
  - GPT → Codex CLI, signed in with a ChatGPT account (free tier included, limited usage).
  - Gemini → free Google AI Studio API key (Flash-class models on the free tier). Gemini CLI's free Google-login path was discontinued in June 2026, and wrapping its OAuth in third-party software is flagged by Google as a policy violation.
  - Claude and Grok → no free programmatic path exists. The "Jeeves browser" opens with the request pre-filled; the user sends it and clicks the site's copy button; Jeeves reads the clipboard. Fully automating send/copy is technically possible but is not an intended or shipped feature.
  - Free tiers changed repeatedly during 2026, so each provider should be a swappable backend.
- **Run Command safety.** Shell metacharacters (`;`, `&&`, `|`, `$()` etc.) are rejected; optional confirmation by clicking the indicator or saying the confirm keyword; trusted command list may skip confirmation.
- **Abort** stops all agents and releases every held Control Mode input.
- **Indicator colors** were chosen so states are distinguishable: thinking gray, researching blue, responding black with white outline, asking for input flashing white, unclear purple, unavailable-model flashing red.
- **TTS models** do exist and voices are generally tied to a specific model (e.g. Piper, Kokoro, XTTS/F5-TTS), so TTS model and TTS voice remain separate settings.

## Open questions

Items `SPEC.md` leaves explicitly undecided:

- Interface with Puppetry vs. reuse its code (pending the Puppetry archive).
- Whether Online Prompt Mode uses MCP where supported.
- Wikipedia "Smart Update" (download only changes) — feasibility and method.
- Request-website partial: whether to use a hidden window.
- Choice of specific STT, intent, TTS, wake word, and local response models and voices to offer.
