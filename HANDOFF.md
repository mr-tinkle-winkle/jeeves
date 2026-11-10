# Jeeves — handoff: optimization and bug-fix pass

Build at handoff: `2026-11-10a` (`jeeves/__init__.py: __build__`), commit `2802c50` on `main`.
First round of the pass: build `2026-11-10b` (see "Pass progress" below).

## Goals of the next pass

1. **Smarter responses**, above all when looking things up (research).
2. **Faster responses**, measured from the end of speech to the first spoken word.
3. **Less performance impact**: CPU, GPU, heat and RAM while idle and while working.

Where these conflict, the default order is **smarter > faster > lighter**. Where a conflict can be
offered as a setting (for example, research depth or a heavier/lighter model), it should be offered as
one, with that order deciding the default.

Constraints for the pass:

- Default models may be changed (a faster speech model, a different intent or answer model) when
  measurably better. Choices already stored in a user's settings are kept.
- Lookups stay **free and keyless**: no search APIs that need a key. Current sources are DuckDuckGo
  (html, then lite), Mojeek, Bing, an optional self-hosted SearXNG, offline Wikipedia (.zim), and the
  pages themselves.
- Requested UI addition (Models page): a **"Download all selected models"** button, and, whenever a
  model that isn't downloaded is selected in a dropdown, a **"Download model"** button beside that
  dropdown.

## Pass progress

Build `2026-11-10b` (first round; nothing in it has been measured on real hardware yet):

- Done: Models page download buttons ("Download model" beside a dropdown, "Download all selected models").
- Faster: long replies start at their first sentence while the rest is synthesized (`audio.stream_speech`);
  in-character rewording reuses three kept variants per line and is skipped while the model is slower than
  `persona.every_line_max_seconds`.
- Smarter: Wikipedia first for plain facts (offline copy, else wikipedia.org; `research.wikipedia_first`,
  `research.online_wikipedia`), disambiguation pages passed over, BM25 passage ranking, searches / pages /
  subject context kept for follow-ups (`research.cache_minutes`).
- Settings added: Listening & Keys > Reply speed, and > Looking things up (research depth, time limit).
- Build `2026-11-10c`: "tell me how to..." / "can you explain what..." requests go to research (they were answered
  from nothing by `local_response`); Observe Thoughts has an organized view (`thoughts_view.py`,
  `indicators.organize_thoughts`), fed by a kind per thought (intent / reasoning / answer / step). The Thoughts
  window itself (`overlay.py: Thoughts`) has no automated test: only `organize`/`render` and the kinds do.
- To measure next on a real setup: the `speech` trace's `synth_s` before/after with Kokoro and Piper; how often
  `wikipedia_first` ends a research and whether its answers are right; `in_character_skipped` counts on
  CPU-only setups.
- Not started: the end-of-speech wait (candidate: transcribe speculatively at a shorter silence and keep the
  result if no more speech follows), speech model defaults by hardware, intent prompt size, idle footprint,
  the Vulkan "no usable GPU" doctor check, batching research's per-page checks.

## What the app does (from the outside)

A local voice assistant for Linux (NixOS, KDE Plasma / Wayland). Named **agents** (each with call names,
a persona, a voice, an enabled set of functions, and optional "rudimentary" single-function mode) are
woken by saying their name. Speech is recognised locally (whisper.cpp), the request is matched to a
**function** by rules or a small local model (llama.cpp), the function runs (timers, YouTube, Jellyfin,
research, screen reading, control mode, Steam games, math, custom functions...), and the reply is spoken
locally (Piper / Kokoro / eSpeak). On-screen indicators (a layer-shell overlay) show listening,
thinking, researching and responding. A PySide6 settings app (themable via `gui/ui_kit`) configures
everything. A daemon (systemd user service) does the work; the GUI and the overlays talk to it over a
Unix socket.

## Measuring before changing

Every change should be judged by numbers, not impressions. The tools exist:

- **Activity > Logs** in the GUI (also `~/.local/state/jeeves/diagnostics.txt`, written a minute after
  each daemon start): hardware, GPU access (Vulkan loader messages, `/dev/dri`, drivers), each model
  server's exact command line and load output, every language-model call's prompt/write tokens per
  second and thinking size, speech recognition's x-realtime and concurrency, recent requests with a
  timeline of where the time went, listening setup, speed-relevant settings, warnings since start, and a
  "What stands out" section. **Run a speed test** times the same job on each model.
  Code: `jeeves/daemon/diagnostics.py`; stats are collected in `models/backends.py: STATS`.
- `jeeves cpu 60`: per-second CPU of the daemon and its programs, with the hottest code lines.
- Each request's **trace** (Activity page, or click a row in an agent's log): every stage with
  timestamps (`intent`, `call`, `say`, `speech`, `sources`, `result`, ...).
- `jeeves version`: installed and running build (bump `__build__` in `jeeves/__init__.py` every round,
  so a rebuild that didn't pick up new code is obvious).

Representative numbers from reports at handoff:

| Setup | Writing speed | Speech recognition | Typical research answer |
|---|---|---|---|
| GPU-offloaded models (CUDA) | fast (not the bottleneck) | ~0.5–1 s per request | ~13 s ("what's 2+2" before the math function existed) |
| CPU-only (GPU build that found no GPU) | 2–4.5 tokens/s with Qwen3 1.7B | 0.1–0.7x realtime with large-v3-turbo | minutes |

## Where the time goes (known)

Voice request path: wake word (Vosk, partial results) → listen session → end of speech
(`general.end_of_speech_seconds`, 1.2 s) → Whisper transcription → intent (rules → keywords → model) →
function → reply text → optional in-character rewording → TTS → playback.

Known costs and suspects, for the next pass to measure and attack:

- **End-of-speech wait** (1.2 s fixed) before anything starts.
- **Whisper large-v3-turbo** on CPU is slow (0.2–0.7x realtime on a 16-core CPU when contended);
  `models.stt.gpu` auto/on/off. Smaller/distilled models (small.en, distil-large-v3, base.en) are
  untested candidates; whisper.cpp `-ng` on CPU, `-nfa` on GPU, threads capped at 8.
- **Every line in character** (`persona.every_line`, per agent `persona_every_line`): each spoken line
  of a function can go through the language model again to restyle it. Adds a full model call to
  simple replies; candidate for caching, skipping short lines, or doing it in the same call as the
  answer.
- **Research** (`functions/research.py: run`, `functions/builtins.py: research`): plan queries with the
  model → 3–4 searches in parallel → fetch pages → extract passages → assess → write the answer.
  Two or more model calls per question plus network. "Jenius" (`models/jeenius.py`, level 2 default)
  decides when the model thinks first; thinking on small models is long and slow on CPU.
- **Intent model** prompt size: the dictionary of enabled functions is in every intent prompt (~1.4k
  tokens observed); prompt read speed matters on CPU. llama-server slot reuse / prompt caching is
  partially effective (logs show LCP reuse).
- **Model server threads**: llama.cpp now writes with physical-core count (`-t`) and reads prompts with
  all allowed threads (`-tb`); whisper with ≤ 8. Untested on GPU-offloaded setups whether lower is
  better there.
- **Idle cost**: Vosk on every listener (microphone, desktop, per-agent devices), Summary transcription
  (now the wake model's light text by default; `summary.transcriber`), level metering, the overlay's
  30 fps animation timer while visible.

## Smarter lookups — current behaviour and weaknesses

- `research.run` understands the question (subject, kind, terms), builds queries, ranks results
  (`rank`), prefetches pages, picks passages (`relevant_passages`, `term_passages`), reads offline
  Wikipedia (`read_wiki`), assesses whether the sources answer it (`assess`), and writes the answer with
  numbered citations; `tidy_answer` strips meta talk ("the sources don't say...", "[Answer]" labels,
  repeats).
- On failure: other spellings → "did you mean" related names → ask the user to spell it → ask what it is
  → give up (`builtins._research_until_found`).
- Observed weaknesses: small models plan irrelevant queries (e.g. "2+2" → RPG guides, now routed to
  the math function); wrong-subject pages are sometimes read; offline Wikipedia's first hit can be a
  disambiguation page; answers can be padded. Short, plain factual questions now skip the thinking step.
- Ideas not yet tried: answer-first from offline Wikipedia for encyclopedic questions; query
  rewriting without the model for simple questions; reranking passages by embedding or BM25 instead of
  keyword overlap; a cheap "is this already known" pass before searching; caching recent research per
  subject; reading fewer, better pages.

## Current problems (open)

- **GPU build that finds no GPU** (NixOS): with `services.jeeves.acceleration = "vulkan"`, the
  model servers can still report "no usable GPU found" at start. Most likely cause: Jeeves built from a
  different nixpkgs than the system whose graphics driver lives in `/run/opengl-driver`. The documented
  fix is making the `jeeves` flake input follow the system's nixpkgs; the Logs report now shows the
  loader's own messages to confirm. Not yet confirmed; a mitigation in Jeeves itself (or a doctor check
  that runs `llama-server --list-devices` and reports plainly) would help.
- **False wakes** from short sounds ("guh", "gee") with Vosk small: the grammar now includes decoy words
  and learned decoys/length from marked wake-ups (`daemon/wakeclips.py`), but this is unverified on real
  audio. Call names unknown to the Vosk vocabulary (e.g. "jellyfin") can't wake an agent.
- **Missing response audio** reported on one setup earlier; `jeeves speak-test` exists; unresolved.
- **On/off**: the daemon now exits ≤ 6 s after SIGTERM and `jeeves on` waits for an old process to go;
  verify after a few toggles.
- **Control mode**: plans now see screen text (tesseract) and planned moves snap to text; real-screen
  accuracy unverified.

## Architecture / file map

- `jeeves/daemon/server.py` — daemon entry, IPC methods (`methods` table), shutdown (bounded),
  diagnostics wiring.
- `jeeves/daemon/engine.py` — the core: listeners ↔ sessions, wake handling (`on_wake`,
  `_start_listen`, `wake_maybe`), session finish/transcription (`end_session`, `_finish_session`),
  whole-sentence and backup paths (`on_utterance`, `on_backup_utterance`), `handle_text` → `_run`
  (intent, function, reply, speech), indicators (`set_indicator`), misfire/confidence gate (`_misfire`,
  `general.act_confidence`), name disambiguation (`named_agent`, `_name_at`), control planning
  (`plan_control`, `_tidy_plan`).
- `jeeves/daemon/listener.py` — per-source audio loop: VAD, wake recognizer, segmenter, sessions, level.
- `jeeves/daemon/intent.py` — rules (`rule_decide`: math, Jellyfin, YouTube, ...), keywords, model
  intent (`DICTIONARY_SYSTEM` prompt), rudimentary agents (`decide_one`, `_whole_request`).
- `jeeves/models/manager.py` — model selection, loading/unloading (`prune`, never while busy), GPU
  layer decisions (`hardware.auto_gpu_layers`), threads, `respond` (agent replies), hybrid mode.
- `jeeves/models/backends.py` — whisper-server, llama-server, Vosk (`WakeRecognizer`: grammar, decoys,
  `pick_names`, `long_enough`), TTS backends; `ManagedServer` (output always drained, head/tail kept);
  `STATS` (speed numbers).
- `jeeves/models/jeenius.py` — when the model thinks first. `models/catalog.py` — downloadable models.
  `models/download.py` — downloads.
- `jeeves/functions/builtins.py` — built-in functions (research, youtube, jellyfin, math, timers,
  control mode, ...). `functions/research.py` — the research pipeline. `functions/partials/` — shared
  pieces (web search engines in `web.py`, `youtube.py`, `jellyfin.py`, `calc.py`, screen, desktop...).
- `jeeves/daemon/diagnostics.py` — the Logs report. `daemon/wakeclips.py` — recent wake-up clips and
  learning. `daemon/summary.py`, `daemon/jumpin.py`, `daemon/timers.py`, `daemon/training.py` (voice
  recordings, Whisper prompt vocabulary).
- `jeeves/overlay.py` + `overlay_skin.py` — indicators and popups (separate processes).
  `video_player.py` — the built-in player.
- `jeeves/gui/` — settings app: `app.py` (window, pages, Activity tabs), `pages_agents.py`,
  `pages_models.py` (where the download buttons go), `pages_misc.py` (Listening & keys, Indicators,
  Training, Activity, Logs), `pages_functions.py`, `shell.py` (agent overview, history rows, Look),
  `autosave.py`, `ui_kit/` (themes; see `docs/UI_THEMING_GUIDE.md`).
- `jeeves/config.py` — every setting's default, with comments. `README.md` — features and a long
  "Decisions and rationale" list (newest first). `SPEC.md` — original spec.
- `nix/package.nix`, `nix/module.nix` — packaging and the user service (`acceleration`, PATH,
  `TimeoutStopSec`, `KillMode`).

## Build / test / environment

- Tests: `QT_QPA_PLATFORM=offscreen python3 -m pytest -q` (436 passing at handoff, ~40–60 s).
  `python3 -m pyflakes jeeves tests` is kept clean.
- **PySide6 must be 6.11.2** for the tests: 6.12 segfaults in QMediaPlayer under the test runner.
- `tests/conftest.py` sets `persona.every_line` off (in-character rewording otherwise consumes scripted
  model answers in tests).
- Engine in tests: `Engine(start_io=False)` via the `engine` fixture; `handle_text(..., wait=True)`
  returns `{"id", ...}`; the full entry is `engine.history.get(id)`.
- Deployment: the flake's NixOS module (`services.jeeves.enable`, `user`, `acceleration` =
  `"vulkan" | "cuda" | "rocm"`). Minimal example:

  ```nix
  inputs.jeeves = { url = "github:mr-tinkle-winkle/jeeves"; inputs.nixpkgs.follows = "nixpkgs"; };
  # modules:
  inputs.jeeves.nixosModules.default
  { services.jeeves = { enable = true; user = "<user>"; acceleration = "vulkan"; }; }
  ```

  The `jeeves` input should follow the same nixpkgs the system is built from (GPU driver
  compatibility). Users update with `nix flake update` and a rebuild; `jeeves version` confirms.
- Commits go straight to `main`; each round bumps `__build__` and adds a README "Decisions and
  rationale" entry at the top.

## Session changelog (this conversation, newest first)

- Logs: GPU access probe (Vulkan loader messages, `/dev/dri`, drivers); "GPU build found no GPU" finding.
- llama.cpp writing threads = physical cores (`-t`), prompt threads = all (`-tb`); a device that is the
  chosen microphone gets no second listener; unused-GPU warning in the daemon log.
- Settings autosave (agents, custom functions, Look colors), on by default (`gui.autosave`).
- Activity > Logs diagnostics report + speed test; model-server output always drained.
- Off/on reliability (pidfile, "stopping" ping, wait for exit); clickable history rows.
- Math function (safe evaluator, number words, percentages, roots); research answers without meta
  talk; short plain questions skip thinking.
- Speech model switch mid-transcription no longer counts as a crash; busy models aren't unloaded.
- Control mode: stray keys released after every run; plans get screen text; moves snap to text;
  "Confidence required to do something" (`general.act_confidence`).
- Wake: decoy words, duration-weighted confidence, learned decoys and length from marked clips,
  nested names (Jeeves / Evil Jeeves) disambiguated, Summary no longer disables the wake model, no
  double calls, one listener per mic.
- Jellyfin: sign-in with a user account, remote-control playback in the Jellyfin app, fuzzy titles with
  a model fallback over the library.
- Indicators: on from the intent step, subtle "checking" dot, misheard red spin.
- YouTube: punctuation and sound-alike channel names.

## Decisions + rationale (selected)

- **Measure, then change**: the Logs report exists so changes can be judged by tokens/s, x-realtime and
  request timelines.
- **Wake grammar with decoys** rather than a raw confidence threshold: Vosk with a names-only grammar
  reports 1.0 for any short sound.
- **Every wake is confirmed by the transcript** unless the wake-up reply was answered: a confident
  wake followed by a pause used to skip the check and let "cheese." through.
- **Summary uses the wake model's light text**: Whisper on every utterance kept the CPU hot.
- **No unloading of busy models**: cutting off a transcription looked like a crash and rested speech
  recognition.
- **Lookups keyless** by requirement.

## Planned (rough priority)

Next:
- Profile the voice path end to end on both a GPU and a CPU-only setup; cut the largest costs first
  (end-of-speech wait, transcription model/settings, extra model calls such as in-character rewording,
  research's model calls).
- Research quality: better query planning for small models, passage ranking, Wikipedia-first for
  encyclopedic questions, fewer/better pages, concise answers.
- Models page: "Download all selected models"; a "Download model" button beside any dropdown showing a
  model that isn't downloaded.
- Speed/quality presets exposed as settings where trade-offs exist (default smarter > faster > lighter).

Later:
- Idle footprint: listeners, Vosk instances, overlay timers, Summary.
- Confirm/mitigate the Vulkan "no usable GPU" case inside Jeeves (doctor check, clearer error).
- Reply streaming into TTS (speak the first sentence while the rest is written), if not already
  effective.

Someday:
- Larger-model routing (small model for intent, larger only for answers that need it) chosen by
  measured latency budget.

## Open questions

- Which speech model gives the best accuracy per second on CPU-only machines (large-v3-turbo vs
  distil vs small.en), and whether the default should depend on detected GPU.
- Whether in-character rewording should be off by default for function replies.
- Whether research depth should default to "quick" for voice and "full" for typed requests.
