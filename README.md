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
| Off switch | **Jeeves on** in the GUI sidebar, `jeeves --toggle`, or `jeeves on` / `jeeves off`. Off stops the daemon itself: every AI model, listening, keybinds, timers and the on-screen indicators go with it, and it stays off after logging in again. On starts a fresh daemon (through systemd when the `jeeves` user service exists). The keybind under Listening & Keys can only turn Jeeves off (it lives in the daemon); for a key that does both, bind `jeeves --toggle` in KDE's Shortcuts. |
| Dry run | GUI **Dry Run** page, or `jeeves dry-run "Jeeves, open OBS"` |
| Settings from a terminal | `jeeves get models`, `jeeves set wake_word.global_threshold 0.7` |
| Check screen reading & control | `jeeves doctor` (or **Listening & Keys → Check screen reading & control**): checks that Screen Reading and Control Mode are on for each agent, the microphone and Jeeves-Microphone, then takes a screenshot, reads it, reads the mouse position and briefly moves the mouse to test exact positioning, and says what to fix. `--no-move` skips the mouse test. |
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
- **GPU.** nixpkgs' llama.cpp and whisper.cpp are CPU-only builds, so a GPU sits idle unless you set `services.jeeves.acceleration = "vulkan";` (works on NVIDIA, AMD and Intel; the Vulkan builds come from the binary cache), `"cuda"` or `"rocm"`. **Models → GPU layers** defaults to **Auto**: when a model loads, Jeeves reads its layer count and attention shape from the GGUF file, measures free VRAM, and puts as many layers on the GPU as fit (each layer costs its share of the weights plus its share of the 8k-token conversation memory, with room left for llama.cpp's compute buffer). The Models page shows the last decision. CPU only, All layers or a fixed number are still available.
- **Choosing models.** Every model is rated for speed and for smarts/accuracy/naturalness, and labelled with the RAM or GPU memory it needs. The Models page detects your RAM, CPU threads, GPUs and whether llama.cpp can use them. It recommends a wake word model, an STT model, a fast intent model, a responses model (plus faster and smarter alternatives) and a voice, each with a one-click **Use this**. The catalog can be filtered by kind and by what runs on your computer, sorted by capability, speed or size, and models can be starred as favorites (kept at the top of every list).
- **Indicator screen.** On Wayland, Qt's "primary screen" is just the first monitor the compositor announced, which is why indicators kept landing on your second screen. Jeeves now asks KDE (`kscreen-doctor`) or Hyprland for the real primary, and by default uses the monitor the mouse is on.
- **Research.** The `research` function (on by default; "look up…", "search for…", "find out…") searches the web through DuckDuckGo's no-key HTML page (or your own SearXNG), reads the top pages plus the offline Wikipedia if downloaded, and has the local model answer from them, naming its sources. While it reads, the indicator is blue and its thoughts view shows each page.
- **Specific device.** An agent can listen to one exact source instead of "me / desktop / both": any microphone, or any output's monitor (e.g. only the headset your voice chat plays through).
- **Jump in.** Per agent, "Jump in whenever the AI wants to" with a frequency from 0 (never) to 1 (a full part of the conversation). The agent's sources are transcribed continuously; after lines not addressed to anyone it may ask the local model whether it has something to say (the model answers PASS otherwise). Higher frequency means it considers more lines, waits less after speaking (8 s at 1, about 3 minutes at 0) and is told to join in rather than only interrupt for something important.
- **Voice chat.** "Speaks through: Microphone/Both" plays into a virtual mic, **Jeeves-Microphone**, which also carries your real mic (looped in), so picking it in Discord lets friends hear both you and the agent. It is created as soon as any agent speaks through the microphone, so it's there before you open Discord. Desktop listening records the default output's monitor with `parec`, and ignores Jeeves' own voice while it's speaking.
- **Kokoro** now works in the Nix package: kokoro-onnx and phonemizer-fork come from their wheels, with espeak-ng from nixpkgs (checked by downloading the Kokoro model and synthesising speech).
- **Screen reading and Control Mode.** The daemon is a user service that often starts before Plasma, so it didn't know it was on KDE and ran spectacle and kdotool without the desktop's display variables; it now picks them up from the systemd user session once the desktop is there. Screenshots try spectacle, then the desktop portal (`org.freedesktop.portal.Screenshot`, KDE may ask once whether Jeeves may take screenshots), then grim, and report why each failed. Screenshot pixels are converted to desktop coordinates (HiDPI scaling, several monitors, monitors left of or above the primary), which is what mouse moves use. The absolute pointer device spans every monitor (0–65535), and relative moves are done as absolute moves from the current cursor position when kdotool can read it, so pointer acceleration doesn't change the distance.
- **Minimum untouched.** Models → Minimum untouched sets what the AIs must always leave free: RAM (a model only loads if that much is still free afterwards, otherwise Jeeves says why), VRAM (kept free on top of what Auto GPU layers leaves), CPU threads (model servers use fewer threads and are pinned to the other cores, so the kept ones stay idle) and GPU % (that share of each model's layers stays on the CPU; 100% = CPU only, speech to text included). Recommendations only pick models that fit in what's left, and the page shows both your hardware and what's left for the AIs. Defaults: 4 GB RAM, 1 GB VRAM, 1 thread, 0% GPU.
- **Screen Reading and Control Mode actually get used.** There was no function the agent could pick to read the screen (only internal building blocks), so "what does this error say?" got a made-up chat answer; **Screen Reading** is now a function of its own (on by default, read-only): it reads the screen or one area with OCR and answers from it, reads it out, or says and circles where something is. Unmistakable requests ("click Save", "type …", "press ctrl+s", "scroll down", "what's on my screen", "at 7pm …") now skip the intent model, which often turned them into chat replies, and Control Mode handles the simple ones itself (find the text, move there, click) instead of asking the model to plan. Control Mode is still off by default; asking for it while it's off now says so and where to turn it on, instead of answering in chat. "Read what it says at the top" no longer sets a timer.
- **Jeeves-Microphone** now always exists while Jeeves is on (before, only once an agent was set to speak through it). **Listening & Keys → Test Jeeves-Microphone** says a sentence into it and checks it comes out; `jeeves doctor` shows which mic it carries and which agents speak into it. If friends hear you but not the agent, the agent's Speaks through is probably Speakers, or it's Both and Discord's Echo Cancellation removes the agent's voice because it also comes out of your speakers.
- **NVIDIA GPUs** weren't detected under the service: NixOS gives user services a bare PATH, so the daemon never found `nvidia-smi`. Jeeves now asks the NVIDIA driver directly (NVML, from `/run/opengl-driver/lib`), falls back to `nvidia-smi` where NixOS puts it, and the service PATH now includes the system and user profiles. `jeeves doctor` has a GPU line. For speed on NVIDIA set `services.jeeves.acceleration = "cuda";` (or `"vulkan"`).
- **Voices.** 124 voices: every English Piper voice (US, UK, Scottish, Northern English, South Asian and Middle Eastern accents, young, old, gravelly…), Piper's multi-speaker models (VCTK's 109 speakers from across the British Isles and beyond, LibriTTS's 904 audiobook readers, L2-Arctic's accented English, CMU Arctic, and Semaine's four acted characters), named presets for the most distinctive of those speakers, and every Kokoro voice, including ones made for other languages that speak English with a strong accent. Agents → Voice has a search box, a speaker picker for multi-speaker voices, speed, pitch, expressiveness (Piper), effects (radio, telephone, robot, megaphone, hall, cave, underwater, alien…), mixing a second Kokoro voice in to make a new one, and **Preview voice**. Picking a voice picks its engine.
- **Calling a busy agent.** Say an agent's name while it's working or talking and it pauses (mid-sentence) and listens. "Carry on" or silence resumes, "wait" keeps it paused until you call it again, "stop" drops what it was doing, and anything else is a new request that replaces it. Its own voice saying its name through your speakers doesn't count.
- **Agents only hear their own sources.** A desktop-audio agent no longer gets what you said into the mic: not through jump-in's shared transcript, not through its memory of recent requests, not by answering a question it asked, and not through wake words or unnamed voice requests heard on the mic.
- **Random names** came from jump-in: the conversation was shown to the model as "User: … / Jeeves: …" lines, and small models often answered with a name or a "Jeeves:" label, which was then said aloud. Replies are now cleaned, and bare names are never spoken.
- **Several monitors.** Screen Reading reads the monitor you're working on (the focused window's, else the mouse's) unless you say otherwise: "what's on my right monitor", "read the top of my left screen", "what's on all my screens" (read one monitor at a time, left to right). Areas like "top left" are within that monitor. Finding something searches every monitor and says which one it's on ("near the top left of your right screen").
- **Staying in character.** The agent's prompt is now an identity block that outranks generic-assistant habits, each request ends with a short "reply as Jeeves, in character" reminder (small models obey the nearest instruction), and other agents' earlier replies are shown as a labelled note instead of as the agent's own past turns (which made agents copy each other). Research, Screen Reading and jump-in answers are in character too. **Agents → Personality → Test personality** asks a few questions and grades each answer 1–5 for character; **Check replies stay in character** grades every reply and rewrites it once if it's off (costs one or two extra model calls).
- **Speaks through a specific device.** Agents → Speaks through → "A specific device…" plays the agent's voice on one exact output (a headset, a second sound card, a stream mixer's input), and "A specific device + Jeeves-Microphone" also puts it into the virtual mic for voice chat. Preview voice plays where the agent speaks.
- **Research answers explain, with sources you can click.** Answers explain the what, why and key details in a few sentences instead of a one-liner, with [n] citations. The citations aren't read aloud. A **Sources** popup (it doesn't take focus) shows the answer with each [n] linked to its source, every source's title as a link that opens in your browser, and the text Jeeves read from each page ("show all the text read"). **History → Show sources** reopens it later. Summaries of the Summary log explain too.
- **Waking up then pausing** no longer gets an instant answer. Vosk reports the name only after a short silence, and whatever came after the name (breathing, a key press, the end of the name) counted as you having started talking, so listening ended a second later and Whisper turned the noise into "Thank you." Now only real speech after the name counts; otherwise Jeeves waits up to 6 seconds for you to start. Whisper's silence phrases ("Thank you.", "Thanks for watching!", "[BLANK_AUDIO]"…) on their own are ignored.
- **Watch the screen** (only when asked: "Jeeves, watch my screen", "commentate my game", "watch my screen and tell me when the render finishes", "stop watching"). Every couple of seconds (Settings: `watch.interval`) the monitor you're on is captured and compared with the last frame; unchanged frames are skipped (even two changed digits count as a change). Changed frames are looked at by a vision model if one is chosen (Models → Vision: Qwen2.5-VL or Gemma 3, which see the picture), otherwise through OCR and the window title. The agent keeps a short timeline, comments when it has something worth saying (talkativeness like Jump in; whatever you asked it to watch for is always mentioned), and its other answers know what's on screen, so "what was that?" works. It runs as a request: right-click Suspend pauses it, Close / "stop watching" / Abort end it, and calling the agent doesn't pause it.
- **YouTube in the Jeeves player.** "Pull up the newest video from moist critikal", "play lofi hip hop on YouTube", "the newest penguinz0 video about Elden Ring". yt-dlp finds the channel (matching the name you said), lists its uploads newest first or searches, and the video plays in the Jeeves player: up to 1080p as separate picture and sound kept in sync, with play/pause, seek, volume, fullscreen and open-on-YouTube. Space / arrows / F / Esc work, and so do "pause the video", "skip ahead 30 seconds", "louder" and "close the video".
- **Less "I'm not sure, could you clarify?".** A function the intent model picks runs even when it's unsure, questions that just restate your request are never asked, and when nothing fits Jeeves answers (or researches a factual question) instead of asking.
- **Deeper research.** Factual questions (games, items, bosses, builds, patches, products, people, dates) are researched instead of answered from memory. Research writes several searches (game wiki and Reddit for game topics), puts specific sources first (wikis, Reddit, Steam, GameFAQs, official sites; Pinterest-style pages are skipped), reads each page fully and keeps its relevant passages (the fact is often mid-page), then asks itself whether the question is answered and runs follow-up searches if not (Settings: `research.depth` quick / normal / deep). The answer says what the sources don't confirm instead of guessing.
- **Quiet microphones.** Speech detection now follows each source's own background level (the "speech level" setting is a ceiling, not a floor), and words the wake model hears after the name count as speech. The previous release judged audio after the name against the fixed level, so on quiet mics requests were thrown away as noise and Jeeves seemed to stop listening. Setting up Jeeves-Microphone can no longer stop the listeners from starting, and it never takes over your default microphone.
- **Memory per agent.** Agents → Memory: turn memory off (the agent starts fresh every time and refuses "remember…"), choose how many recent requests it sees (Default = the global count) and how many remembered notes, and whether it only sees its own requests and notes.
- **GUI crashes** when switching pages or models quickly came from deleting list rows while Qt was still using them, and from the page cross-fade racing itself; both fixed and stress-tested. Models you switch away from are now unloaded instead of piling up in memory.
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
