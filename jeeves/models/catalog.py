"""Models Jeeves can download and run. Nothing is downloaded by default.

The options are picked to cover different resource levels without being
redundant: each kind has a small/fast choice up to a large/accurate one.

Kinds:
  wake   -- wake word (listens for agent call names constantly; must be light)
  stt    -- speech to text (local only)
  llm    -- text models, used for Intention Processing and/or Local Response
  tts    -- text-to-speech engines; each has its own voices
  voice  -- a voice for a tts engine (voices are tied to their engine)

URLs follow each project's published download layout (HuggingFace
``resolve/main`` paths, Kiwix mirror, GitHub releases).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

HF = "https://huggingface.co"


@dataclass
class ModelFile:
    url: str
    path: str                    # relative to the model's folder
    sha256: str | None = None
    unzip: bool = False


@dataclass
class ModelEntry:
    id: str
    kind: str
    name: str
    engine: str                  # whisper.cpp | vosk | llama.cpp | piper | kokoro | espeak-ng | endpoint
    size_mb: int
    ram_mb: int
    description: str
    files: list[ModelFile] = field(default_factory=list)
    tts_model: str | None = None  # for voices: which tts entry they belong to
    voice: str | None = None      # engine-specific voice name
    builtin: bool = False         # nothing to download (e.g. espeak-ng)
    language: str = "en"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _whisper(id_: str, file: str, name: str, size: int, ram: int, desc: str) -> ModelEntry:
    return ModelEntry(id_, "stt", name, "whisper.cpp", size, ram, desc,
                      [ModelFile(f"{HF}/ggerganov/whisper.cpp/resolve/main/{file}", file)])


def _gguf(id_: str, repo: str, file: str, name: str, size: int, ram: int, desc: str) -> ModelEntry:
    return ModelEntry(id_, "llm", name, "llama.cpp", size, ram, desc,
                      [ModelFile(f"{HF}/{repo}/resolve/main/{file}", file)])


def _piper(lang: str, speaker: str, quality: str, name: str, desc: str) -> ModelEntry:
    region = lang
    family = lang.split("_")[0]
    base = f"{HF}/rhasspy/piper-voices/resolve/main/{family}/{region}/{speaker}/{quality}/{region}-{speaker}-{quality}"
    size = {"low": 20, "medium": 63, "high": 114}[quality]
    return ModelEntry(f"piper-{region}-{speaker}-{quality}", "voice", name, "piper", size, 150, desc,
                      [ModelFile(base + ".onnx", "voice.onnx"), ModelFile(base + ".onnx.json", "voice.onnx.json")],
                      tts_model="piper", voice=f"{region}-{speaker}-{quality}")


def _kokoro_voice(code: str, name: str, desc: str) -> ModelEntry:
    return ModelEntry(f"kokoro-{code}", "voice", name, "kokoro", 0, 0, desc, [], tts_model="kokoro", voice=code)


CATALOG: list[ModelEntry] = [
    # ---- wake word -------------------------------------------------------
    ModelEntry("vosk-small-en", "wake", "Vosk small English", "vosk", 40, 300,
               "Light keyword spotter: listens only for your agents' names. Recommended.",
               [ModelFile("https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip", ".", unzip=True)]),
    ModelEntry("stt-match", "wake", "Use the STT model", "stt-match", 0, 0,
               "No extra model: every utterance is transcribed and checked for an agent name. Heavier and "
               "slower to react, but needs nothing else.", builtin=True),

    # ---- speech to text --------------------------------------------------
    _whisper("whisper-tiny-en", "ggml-tiny.en.bin", "Whisper tiny (English)", 75, 390,
             "Fastest; fine for short commands on weak CPUs."),
    _whisper("whisper-base-en", "ggml-base.en.bin", "Whisper base (English)", 142, 500,
             "Good default for commands."),
    _whisper("whisper-small-en", "ggml-small.en.bin", "Whisper small (English)", 466, 1000,
             "Noticeably more accurate; still real-time on most CPUs."),
    _whisper("whisper-medium-en", "ggml-medium.en.bin", "Whisper medium (English)", 1500, 2600,
             "High accuracy; best with a GPU."),
    _whisper("whisper-large-v3-turbo-q5", "ggml-large-v3-turbo-q5_0.bin", "Whisper large v3 turbo (quantized)",
             547, 1600, "Multilingual, near large-v3 accuracy at a fraction of the cost."),
    _whisper("whisper-large-v3-turbo", "ggml-large-v3-turbo.bin", "Whisper large v3 turbo", 1600, 3100,
             "Most accurate option; GPU recommended."),
    ModelEntry("vosk-en-large", "stt", "Vosk English (large)", "vosk", 1800, 2500,
               "Streaming recognizer; lower latency than Whisper, less accurate on unusual words.",
               [ModelFile("https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip", ".", unzip=True)]),

    # ---- text models (intent + local response) --------------------------
    _gguf("qwen2.5-0.5b", "bartowski/Qwen2.5-0.5B-Instruct-GGUF", "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf",
          "Qwen2.5 0.5B Instruct", 400, 900, "Tiny; quick intent picking for simple dictionaries."),
    _gguf("qwen2.5-1.5b", "bartowski/Qwen2.5-1.5B-Instruct-GGUF", "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
          "Qwen2.5 1.5B Instruct", 990, 1800, "Good default for intent processing."),
    _gguf("qwen2.5-3b", "bartowski/Qwen2.5-3B-Instruct-GGUF", "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
          "Qwen2.5 3B Instruct", 1930, 3000, "More reliable arguments; usable for short answers."),
    _gguf("llama3.2-3b", "bartowski/Llama-3.2-3B-Instruct-GGUF", "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
          "Llama 3.2 3B Instruct", 2020, 3200, "Friendly conversational answers at a small size."),
    _gguf("qwen2.5-7b", "bartowski/Qwen2.5-7B-Instruct-GGUF", "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
          "Qwen2.5 7B Instruct", 4680, 6500, "Good local responses and macro writing."),
    _gguf("qwen2.5-14b", "bartowski/Qwen2.5-14B-Instruct-GGUF", "Qwen2.5-14B-Instruct-Q4_K_M.gguf",
          "Qwen2.5 14B Instruct", 8990, 11000, "Best local answers; needs a strong GPU or lots of RAM."),

    # ---- tts engines -----------------------------------------------------
    ModelEntry("espeak-ng", "tts", "eSpeak NG", "espeak-ng", 0, 20,
               "Instant and tiny, robotic. Always available as a fallback.", builtin=True),
    ModelEntry("piper", "tts", "Piper", "piper", 0, 150,
               "Fast natural-sounding neural voices; runs well on CPU. Recommended.", builtin=True),
    ModelEntry("kokoro", "tts", "Kokoro 82M", "kokoro", 340, 800,
               "Very natural voices; a bit heavier than Piper.",
               [ModelFile("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
                          "kokoro-v1.0.onnx"),
                ModelFile("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
                          "voices-v1.0.bin")]),

    # ---- voices ----------------------------------------------------------
    ModelEntry("espeak-en", "voice", "eSpeak English (US)", "espeak-ng", 0, 0, "Robotic.", tts_model="espeak-ng",
               voice="en-us", builtin=True),
    ModelEntry("espeak-en-gb", "voice", "eSpeak English (UK)", "espeak-ng", 0, 0, "Robotic.", tts_model="espeak-ng",
               voice="en-gb", builtin=True),
    _piper("en_US", "lessac", "medium", "Lessac (US, female)", "Clear and neutral."),
    _piper("en_US", "amy", "medium", "Amy (US, female)", "Bright."),
    _piper("en_US", "ryan", "high", "Ryan (US, male)", "Warm, high quality."),
    _piper("en_US", "joe", "medium", "Joe (US, male)", "Casual."),
    _piper("en_US", "hfc_female", "medium", "HFC Female (US)", "Smooth."),
    _piper("en_US", "hfc_male", "medium", "HFC Male (US)", "Deep."),
    _piper("en_GB", "alan", "medium", "Alan (UK, male)", "Very butler."),
    _piper("en_GB", "northern_english_male", "medium", "Northern English (UK, male)", "Regional accent."),
    _piper("en_GB", "jenny_dioco", "medium", "Jenny (UK, female)", "Soft."),
    _kokoro_voice("af_heart", "Heart (US, female)", "Kokoro's most natural voice."),
    _kokoro_voice("af_bella", "Bella (US, female)", "Expressive."),
    _kokoro_voice("am_michael", "Michael (US, male)", "Calm."),
    _kokoro_voice("am_fenrir", "Fenrir (US, male)", "Deep."),
    _kokoro_voice("bf_emma", "Emma (UK, female)", "Crisp."),
    _kokoro_voice("bm_george", "George (UK, male)", "Distinguished."),
    _kokoro_voice("bm_fable", "Fable (UK, male)", "Storyteller."),
]

BY_ID = {m.id: m for m in CATALOG}


def get(model_id: str | None) -> ModelEntry | None:
    if not model_id:
        return None
    if model_id.startswith("endpoint:"):
        # "endpoint:http://localhost:11434/v1|llama3.2" -- an OpenAI-compatible server
        # you already run (Ollama, LM Studio, a remote llama-server)
        url, _, name = model_id[9:].partition("|")
        return ModelEntry(model_id, "llm", f"{name or 'model'} @ {url}", "endpoint", 0, 0,
                          "External OpenAI-compatible endpoint", builtin=True)
    return BY_ID.get(model_id)


def of_kind(kind: str) -> list[ModelEntry]:
    return [m for m in CATALOG if m.kind == kind]


def voices_for(tts_id: str) -> list[ModelEntry]:
    return [m for m in CATALOG if m.kind == "voice" and m.tts_model == tts_id]
