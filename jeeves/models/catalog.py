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
    # Hugging Face repo + filename suffix: the exact file is looked up through the
    # HF API at download time (used when ``url`` is empty, or if it 404s)
    hf_repo: str | None = None
    hf_suffix: str = "Q4_K_M.gguf"


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
    speed: int = 3                # 1 (slow) .. 5 (instant), on a typical CPU
    quality: int = 3              # 1 (basic) .. 5 (best): accuracy for STT, smarts for LLMs, naturalness for voices
    params_b: float = 0.0         # LLMs: billions of parameters
    active_b: float = 0.0         # LLMs: parameters used per word (mixture-of-experts are much lower)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _whisper(id_: str, file: str, name: str, size: int, ram: int, desc: str) -> ModelEntry:
    return ModelEntry(id_, "stt", name, "whisper.cpp", size, ram, desc,
                      [ModelFile(f"{HF}/ggerganov/whisper.cpp/resolve/main/{file}", file)])


def _gguf(id_: str, repo: str, file: str, name: str, size: int, ram: int, desc: str) -> ModelEntry:
    return ModelEntry(id_, "llm", name, "llama.cpp", size, ram, desc,
                      [ModelFile(f"{HF}/{repo}/resolve/main/{file}", file, hf_repo=repo)])


def _hf(id_: str, repo: str, name: str, size: int, ram: int, desc: str, suffix: str = "Q4_K_M.gguf") -> ModelEntry:
    """A GGUF model whose exact file name is resolved from the repo at download time."""
    return ModelEntry(id_, "llm", name, "llama.cpp", size, ram, desc,
                      [ModelFile("", "model.gguf", hf_repo=repo, hf_suffix=suffix)])


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
    # fastest (tiny; fine for intent picking and one-line answers)
    _hf("gemma3-270m", "unsloth/gemma-3-270m-it-GGUF", "Gemma 3 270M", 250, 600,
        "Fastest option. Instant on any CPU; simple requests only."),
    _hf("smollm2-360m", "bartowski/SmolLM2-360M-Instruct-GGUF", "SmolLM2 360M", 270, 650,
        "Tiny and very fast; short answers."),
    _hf("qwen3-0.6b", "bartowski/Qwen_Qwen3-0.6B-GGUF", "Qwen3 0.6B", 480, 900,
        "Very fast, surprisingly capable for intent picking."),
    _hf("llama3.2-1b", "bartowski/Llama-3.2-1B-Instruct-GGUF", "Llama 3.2 1B", 810, 1400,
        "Fast, friendly short answers."),
    _hf("gemma3-1b", "bartowski/google_gemma-3-1b-it-GGUF", "Gemma 3 1B", 810, 1400,
        "Fast, good general knowledge for its size."),
    _hf("qwen3-1.7b", "bartowski/Qwen_Qwen3-1.7B-GGUF", "Qwen3 1.7B", 1280, 2200,
        "Fast and reliable; a good intent model."),
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
    # smarter (bigger = better answers, slower; GPU strongly recommended past ~8B)
    _hf("qwen3-4b", "bartowski/Qwen_Qwen3-4B-GGUF", "Qwen3 4B", 2500, 4000,
        "Smart for its size; good default for local responses on a CPU."),
    _hf("qwen3-8b", "bartowski/Qwen_Qwen3-8B-GGUF", "Qwen3 8B", 5000, 7000,
        "Strong answers and macro writing; comfortable on an 8 GB GPU."),
    _hf("gemma3-12b", "bartowski/google_gemma-3-12b-it-GGUF", "Gemma 3 12B", 7300, 10000,
        "Very good writing and general knowledge."),
    _hf("gpt-oss-20b", "ggml-org/gpt-oss-20b-GGUF", "gpt-oss 20B (MoE)", 12100, 14000,
        "OpenAI's open model; mixture-of-experts, so much faster than its size suggests.", suffix="mxfp4.gguf"),
    _hf("qwen3-14b", "bartowski/Qwen_Qwen3-14B-GGUF", "Qwen3 14B", 9000, 12000,
        "Excellent answers; needs a 12 GB+ GPU to be quick."),
    _hf("qwen3-30b-a3b", "bartowski/Qwen_Qwen3-30B-A3B-GGUF", "Qwen3 30B-A3B (MoE)", 18600, 21000,
        "30B-class smarts at roughly 3B speed (only 3B active per word). Best smart-and-fast pick if you have the RAM."),
    _hf("mistral-small-3.2-24b", "bartowski/mistralai_Mistral-Small-3.2-24B-Instruct-2506-GGUF",
        "Mistral Small 3.2 24B", 14300, 17000, "Very capable, follows instructions well; 16 GB+ GPU."),
    _hf("gemma3-27b", "bartowski/google_gemma-3-27b-it-GGUF", "Gemma 3 27B", 16500, 20000,
        "Top-tier local answers; 24 GB GPU."),
    _hf("qwen3-32b", "bartowski/Qwen_Qwen3-32B-GGUF", "Qwen3 32B", 19800, 23000,
        "Among the smartest models that fit a 24 GB GPU."),
    _hf("llama3.3-70b", "bartowski/Llama-3.3-70B-Instruct-GGUF", "Llama 3.3 70B", 42500, 46000,
        "Smartest option here; needs ~48 GB of VRAM (or lots of RAM and patience)."),

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

# (speed, quality, params_b, active_b) -- speed is for a typical desktop CPU
RATINGS: dict[str, tuple] = {
    "vosk-small-en": (5, 3), "stt-match": (2, 4),
    "whisper-tiny-en": (5, 2), "whisper-base-en": (4, 3), "whisper-small-en": (3, 4), "whisper-medium-en": (2, 4),
    "whisper-large-v3-turbo-q5": (2, 5), "whisper-large-v3-turbo": (1, 5), "vosk-en-large": (4, 3),
    "gemma3-270m": (5, 1, 0.27, 0.27), "smollm2-360m": (5, 1, 0.36, 0.36), "qwen2.5-0.5b": (5, 1, 0.5, 0.5),
    "qwen3-0.6b": (5, 2, 0.6, 0.6), "llama3.2-1b": (4, 2, 1.2, 1.2), "gemma3-1b": (4, 2, 1.0, 1.0),
    "qwen2.5-1.5b": (4, 2, 1.5, 1.5), "qwen3-1.7b": (4, 3, 1.7, 1.7), "qwen2.5-3b": (3, 3, 3.1, 3.1),
    "llama3.2-3b": (3, 3, 3.2, 3.2), "qwen3-4b": (3, 3, 4.0, 4.0), "qwen2.5-7b": (2, 4, 7.6, 7.6),
    "qwen3-8b": (2, 4, 8.2, 8.2), "gemma3-12b": (2, 4, 12.0, 12.0), "qwen2.5-14b": (2, 4, 14.8, 14.8),
    "qwen3-14b": (2, 4, 14.8, 14.8), "gpt-oss-20b": (3, 4, 21.0, 3.6), "qwen3-30b-a3b": (3, 5, 30.5, 3.3),
    "mistral-small-3.2-24b": (1, 4, 24.0, 24.0), "gemma3-27b": (1, 5, 27.0, 27.0), "qwen3-32b": (1, 5, 32.8, 32.8),
    "llama3.3-70b": (1, 5, 70.0, 70.0),
    "espeak-ng": (5, 1), "piper": (4, 4), "kokoro": (3, 5), "espeak-en": (5, 1), "espeak-en-gb": (5, 1),
}
for _m in CATALOG:
    _r = RATINGS.get(_m.id)
    if _r is None and _m.kind == "voice":
        _r = {"piper": (4, 4), "kokoro": (3, 5)}.get(_m.engine, (5, 1))
    if _r:
        _m.speed, _m.quality = _r[0], _r[1]
        if len(_r) > 2:
            _m.params_b, _m.active_b = _r[2], _r[3]

BY_ID = {m.id: m for m in CATALOG}

SPEED_LABELS = {1: "slow", 2: "moderate", 3: "quick", 4: "fast", 5: "instant"}
QUALITY_LABELS = {1: "basic", 2: "fair", 3: "good", 4: "very good", 5: "excellent"}


def hardware_label(m: ModelEntry) -> str:
    """Rough hardware tier for a model."""
    gb = m.ram_mb / 1000
    if gb <= 1.5:
        return "Any computer"
    if m.kind == "llm" and m.size_mb > 4000:
        vram = (m.size_mb + 1000) / 1000
        return f"{gb:.0f} GB RAM, or a {vram:.0f} GB GPU to be quick"
    return f"{gb:.0f} GB RAM" if gb >= 2 else f"{gb:.1f} GB RAM"


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
