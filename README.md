# Jeeves

A local-first voice assistant for Linux (NixOS). Named agents respond to spoken or typed requests; an intent model maps each request to a function from a user-editable dictionary of composable functions, and runs it.

`SPEC.md` is the authoritative specification. Anything not in `SPEC.md` is out of scope. This README only adds context: status, decisions made while building, and how to install and use it.

## Status

First full implementation of `SPEC.md`: daemon, settings GUI, on-screen overlay, the function dictionary with every listed function, a NixOS module and a test suite (72 tests). See [Verified vs. not](#verified-vs-not) for what has run on real hardware and what hasn't.

## Installing (NixOS)

```nix
{
  inputs.jeeves.url = "github:mr-tinkle-winkle/jeeves";
  inputs.jeeves.inputs.nixpkgs.follows = "nixpkgs";

  outputs = { nixpkgs, jeeves, ... }: {
    nixosConfigurations.yourhost = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./configuration.nix
        jeeves.nixosModules.default
        {
          services.jeeves = {
            enable = true;
            user = "yourname";
            # Optional. Every key set here is LOCKED in the GUI.
            settings = {
              models.stt.model = "whisper-base-en";
              run_command.trusted = [ "puppetry --list" ];
            };
            # geminiApiKeyFile = "/run/secrets/gemini";
          };
        }
      ];
    };
  };
}
```

Rebuild, then log out and back in (the `input` group needs a new session). The daemon runs as `systemctl --user status jeeves`; open the settings with `jeeves` or **Jeeves** in the app launcher.

First run: **Models** → download a wake word model (Vosk small), a speech-to-text model (Whisper base) and a text model (Qwen2.5 1.5B is a good start for intent; use a bigger one for Local Response). Nothing downloads until you pick it.

Without NixOS: `pip install .[gui,input,wake,wikipedia]`, put `whisper-server`, `llama-server`, `piper`/`espeak-ng`, `tesseract`, `wl-clipboard`, `pw-record` on PATH, and run `jeeves daemon` and `jeeves`.

## Using it

| | |
|---|---|
| Say an agent's name | "Jeeves, set a timer for ten minutes." |
| Text Request | keybind (default Meta+J), or `jeeves --manual_request=text` |
| Voice Request | keybind per agent, or `jeeves --manual_request=voice --agent=jeeves` |
| Voice Request: Unknown | its own keybind, or `jeeves --manual_request=voice` with no agent: listens now, and you start with the agent's name ("Claude, …"). With **Always listen for wake words** off, the microphone is only open during voice requests and answers, so this is push-to-talk. |
| Manual Response Review | keybind (Meta+Shift+R), or `jeeves --review` |
| Abort | Pause key, or `jeeves --abort`: stops every agent, releases every Control Mode key/button |
| Off switch | **Jeeves on** in the GUI sidebar, `jeeves --toggle` (or `jeeves on` / `jeeves off`), or a keybind under Listening & Keys. Off stops every agent, stops listening and unloads every AI model; timers still ring, without a voice. Dry runs keep working. |
| Dry run | GUI **Dry Run** page, or `jeeves dry-run "Jeeves, open OBS"` |
| Settings from a terminal | `jeeves get models`, `jeeves set wake_word.global_threshold 0.7` |
| Functions | `jeeves functions`, `jeeves dictionary --agent jeeves`, `jeeves export-functions -o mine.json`, `jeeves import-functions theirs.json` |

Keybinds read the keyboard read-only (never grabbed), like Puppetry. You can also bind the commands above in KDE/Hyprland shortcut settings instead.

Onscreen: click the microphone to listen 5 s longer, hold it to keep listening until you let go (+1 s). Click a spinner to see the agent's thoughts (and what it's reading when researching), pause a response, or confirm a command. Right-click the microphone or a spinner for **Suspend** (pauses that request or listen, toggles to Resume) and **Close** (stops just that one). **Indicators → Screen** picks the monitor: the one the mouse is on (default), the desktop's primary, or a named output.

## Pipeline (summary of SPEC.md)

1. Wake word model detects an agent's name (or a manual text/voice request bypasses it).
2. Local STT model transcribes the request.
3. Intent model selects a function and arguments using the Dictionary, limited to the agent's enabled functions.
4. The function runs (composed from partial functions); output is spoken via the agent's TTS voice and/or shown.
5. Onscreen indicators reflect each stage (listening, transcript, thinking, researching, responding, asking for input, unclear).

## Architecture

```
jeeves daemon (systemd --user)          jeeves (GUI)        jeeves overlay (started by the daemon)
  engine.py  -- pipeline, sessions,  <-- JSON lines over $XDG_RUNTIME_DIR/jeeves/daemon.sock -->
               abort, handoff                                indicators, timers, screen marks,
  listener.py -- mic / desktop audio                         Review / Text Request / import popups
  intent.py   -- text -> function
  models/     -- whisper-server, llama-server, piper/kokoro/espeak, vosk (load/unload per app rules)
jeeves overlay            -- indicators/timers/marks/notices as layer-shell surfaces (native/ shim)
jeeves overlay --popups   -- Review, Text Request, answers, thoughts, import approval (normal windows)
  functions/  -- the Dictionary: builtins.py (full), partials/*.py, composer.py (control flow)
  puppetry.py -- macros through Puppetry's control socket + config files
  control.py  -- jeeves-keyboard / jeeves-mouse / jeeves-controller (uinput)
```

- **Daemon + GUI split.** Only the daemon changes settings or runs functions; the GUI and CLI send requests. The daemon starts two overlay processes, so the indicators and every popup work with the GUI closed.
- **On-screen indicators** are Wayland layer-shell surfaces on the overlay layer, the same approach as afterglow's clip indicator. KDE's LayerShellQt has no Python bindings, so `native/jeeves_layershell.cpp` (copied from afterglow) is a tiny C shim loaded with ctypes. Layer-shell makes every window of a process a layer surface, so popups that need the keyboard run in a second process. Without the shim the overlay falls back to XWayland windows.
- **Settings**: `~/.config/jeeves/settings.json` (user) layered under `/etc/jeeves/settings.json` (NixOS, locked). Secrets (Gemini key) live separately in `secrets.json` (mode 600) or `geminiApiKeyFile`.
- **Custom partials**: Python files in `~/.config/jeeves/partials/` using `from jeeves.functions import partial, Arg`.
- **Custom full functions**: compositions of partials (JSON `steps`: `call`, `set`, `if`, `repeat`, `while`, `for_each`, `when`, `wait`, `return`), made in **Functions** and exportable as a manifest.
- **App-registered functions**: an app drops a manifest into `~/.local/share/jeeves/imports/` or runs `jeeves import-functions`. A popup lists each function and the default functions it uses. Imports are compositions only, so apps can't run their own Python inside the daemon.
- **UI**: PySide6 on the shared `ui_kit` (copied from the UI archive, per `docs/UI_THEMING_GUIDE.md`). No colors were provided, so it uses the kit's placeholder monochrome scheme; change it under **Appearance**.

## Decisions and rationale

- **Online agents.** Consumer chat services (claude.ai, chatgpt.com, Grok, Gemini web) prohibit automated access and programmatic extraction of output in their terms. Routing is therefore:
  - GPT → Codex CLI (`codex exec`), signed in with a ChatGPT account (free tier included, limited usage).
  - Gemini → free Google AI Studio API key (Flash-class models on the free tier). Gemini CLI's free Google-login path was discontinued in June 2026, and wrapping its OAuth in third-party software is flagged by Google as a policy violation.
  - Claude and Grok → no free programmatic path exists. The "Jeeves browser" (its own browser profile) opens with the request pre-filled; the user sends it and clicks the site's copy button; Jeeves reads the clipboard. Fully automating send/copy is technically possible but is not an intended or shipped feature.
  - Free tiers changed repeatedly during 2026, so each provider is its own class in `daemon/online.py` and can be swapped.
- **Puppetry: interface for Macros, reuse the approach for Control Mode.** Macros stay in Puppetry, so they keep working with its editor, combos and profiles. Jeeves uses the same channels Puppetry's own CLI/GUI use: the control socket (`FIRE` with arguments, `ABORT`), the shared `macros.json`/profile files, and Puppetry's compiler (`puppetry-daemon --check`) to validate model-written code before saving. Control Mode needs held-key tracking that Abort can release and devices named `jeeves-*`, so Jeeves creates its own uinput devices the way Puppetry does rather than sending every keypress through Puppetry's macro runner.
- **MCP in Online Prompt Mode.** Codex uses whatever MCP servers are configured for Codex (`~/.codex/config.toml`). The Gemini API path and the browser sites take no tools from Jeeves. Jeeves doesn't run an MCP server of its own.
- **Wikipedia Smart Update.** The offline copy is a Kiwix ZIM (text only, built-in search index, read with python-libzim). ZIM has no official diff/patch format, so download-only-the-changes isn't possible for it. Smart Update skips the download when no newer edition exists, resumes interrupted downloads, and keeps the old copy usable until the new one finishes. Wikimedia's daily "adds-changes" XML dumps would need a full local MediaWiki-XML pipeline and search index, which costs more than the monthly re-download.
- **Request a Website** is a plain background HTTP request. No window opens, hidden or otherwise, so pages that need JavaScript can come back mostly empty.
- **Run Command safety.** Commands run without a shell. Shell metacharacters (`;`, `&&`, `|`, `$()` etc.) are rejected. Confirmation can be required, given by clicking the indicator or saying the confirm keyword. Commands on the trusted list skip confirmation; an entry with no arguments trusts every argument list.
- **Abort** stops all agents and releases every held Control Mode input.
- **Indicator colors** were chosen so states are distinguishable: thinking gray, researching blue, responding black with white outline, asking for input flashing white, unclear purple, unavailable-model flashing red.
- **TTS models** do exist and voices are generally tied to a specific model, so TTS model and TTS voice are separate settings.
- **GPU.** nixpkgs' llama.cpp and whisper.cpp are CPU-only builds, so a GPU sits idle unless you set `services.jeeves.acceleration = "vulkan";` (works on NVIDIA, AMD and Intel; the Vulkan builds come from the binary cache), `"cuda"` or `"rocm"`. Then set **Models → GPU layers** to 99.
- **Choosing models.** Every model is rated for speed and for smarts/accuracy/naturalness, and labelled with the RAM or GPU memory it needs. The Models page detects your RAM, CPU threads, GPUs and whether llama.cpp can use them. It recommends a wake word model, an STT model, a fast intent model, a responses model (plus faster and smarter alternatives) and a voice, each with a one-click **Use this**. The catalog can be filtered by kind and by what runs on your computer, sorted by capability, speed or size, and models can be starred as favorites (kept at the top of every list).
- **Indicator screen.** On Wayland, Qt's "primary screen" is just the first monitor the compositor announced, which is why indicators kept landing on your second screen. Jeeves now asks KDE (`kscreen-doctor`) or Hyprland for the real primary, and by default uses the monitor the mouse is on.
- **Right-click menu** is drawn inside the indicator's own surface rather than as a separate popup window, because popup windows attached to layer-shell surfaces aren't reliable across compositors.
- **Models offered** (`jeeves/models/catalog.py`). Wake word: Vosk small (a grammar of just your call names), or "use the STT model". STT: Whisper tiny/base/small/medium/large-v3-turbo through whisper.cpp, and Vosk large. Text: Qwen2.5 0.5B/1.5B/3B/7B/14B and Llama 3.2 3B (GGUF through llama.cpp), or any OpenAI-compatible endpoint you already run (`endpoint:http://localhost:11434/v1|model`). TTS: Piper (9 English voices), Kokoro (7 voices; its engine isn't in the Nix package yet, and the Models page says so) and eSpeak NG. If the chosen voice fails, Jeeves falls back to eSpeak NG and shows the reason on screen.
- **More text models.** Fastest: Gemma 3 270M, SmolLM2 360M, Qwen3 0.6B, Llama 3.2 1B, Gemma 3 1B, Qwen3 1.7B. Smartest: Qwen3 4B/8B/14B/32B, Gemma 3 12B/27B, gpt-oss 20B, Qwen3 30B-A3B (30B-class answers at about 3B speed), Mistral Small 3.2 24B, Llama 3.3 70B. These newer entries look up their exact file through the Hugging Face API at download time, so a renamed upload doesn't break them. Thinking models answer straight away by default; **Models → Thinking models** lets them think first (smarter, slower), and that reasoning goes to the thoughts view, never to speech.
- **Model unloading** stops the model's server process, so the memory is actually freed. While a model is unloaded for an app, using it flashes the indicator red and queues the request (audio included) until the app closes.
- **Summary on = wake word off.** Per the spec. While Summary is on, agent names are found in its continuous transcript instead.
- **Voice training.** Recordings are stored as a dataset, and their words plus your vocabulary list are passed to Whisper as its initial prompt. That prompt is whisper.cpp's supported way to bias recognition toward names. **Evaluate** measures the word error rate with and without it. Fine-tuning Whisper's weights needs a GPU training run outside Jeeves; **Export dataset** writes the standard `metadata.csv` + `wavs/` layout for that. Intent training uses training phrases and History ratings as worked examples in the Dictionary.
- **Handoff.** The receiving agent can't hand off again, so two agents can't bounce a request back and forth.

## Verified vs. not

Verified in the build sandbox (no audio hardware, no compositor, no GPU):
- 72 tests: settings layering and NixOS locks, Run Command safety, the composition language, the Dictionary, intent (keyword path and a stubbed model with retry), the full request pipeline, clarifying questions, confirmations by click and by keyword, abort, extended prompt mode, handoff permissions, imports and approval, memory, ratings, timers and schedules, triggers, listening sessions (wake → request, answers, queued requests while STT is unloaded), the Puppetry socket and file formats, the socket protocol and the CLI. The GUI is built and every page opened offscreen.
- The UI kit's own 26 checks (`tests/gui_ui_kit_checks.py`).
- The flake's package builds against nixos-unstable (2026-10-03), including Vosk and libzim (packaged from their PyPI wheels in `nix/python-extras.nix`, since nixpkgs has neither), and the NixOS module evaluates into a test system.

- In a headless Wayland compositor (sway, which supports layer-shell like KWin): the Nix-built daemon started both overlay processes, and the listening mic, response text, timer and notice all appeared as layer-shell overlays; the Review popup opened as a normal window.

Not yet exercised on a real desktop:
- Real microphone/desktop capture, whisper-server, llama-server, Piper/Kokoro and Vosk with downloaded models. The model download URLs follow each project's published layout but couldn't be fetched from the sandbox.
- The overlay's always-on-top behaviour under KWin and Hyprland (it uses XWayland like Puppetry's on-screen overlay), uinput devices and absolute pointer moves, kdotool/hyprctl window queries, screenshots + OCR, clipboard, Codex/Gemini/Jeeves-browser flows, Wikipedia download and search.
