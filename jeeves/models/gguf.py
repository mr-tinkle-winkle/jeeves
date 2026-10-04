"""Read the few GGUF metadata values Auto GPU layers needs (layer count and the
attention shape, for the size of the conversation memory). Stdlib only; skips
everything else, including the large tokenizer arrays."""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, BinaryIO

_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
WANTED = ("block_count", "embedding_length", "attention.head_count", "attention.head_count_kv",
          "attention.key_length", "attention.value_length", "expert_count")


def _read(f: BinaryIO, fmt: str) -> Any:
    size = struct.calcsize(fmt)
    data = f.read(size)
    if len(data) != size:
        raise ValueError("truncated GGUF file")
    return struct.unpack(fmt, data)[0]


def _string(f: BinaryIO) -> str:
    n = _read(f, "<Q")
    return f.read(n).decode("utf-8", "replace")


def _value(f: BinaryIO, vtype: int, keep: bool) -> Any:
    if vtype in _SCALARS:
        return _read(f, _SCALARS[vtype])
    if vtype == 8:
        return _string(f)
    if vtype == 9:
        itype, count = _read(f, "<I"), _read(f, "<Q")
        if itype in _SCALARS and not keep:
            f.seek(struct.calcsize(_SCALARS[itype]) * count, 1)
            return None
        items = [_value(f, itype, keep) for _ in range(count)]
        return items if keep else None
    raise ValueError(f"unknown GGUF value type {vtype}")


def metadata(path: str | Path) -> dict[str, Any]:
    """{'arch': 'qwen3', 'block_count': 36, ...} with the architecture prefix removed."""
    out: dict[str, Any] = {}
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        version = _read(f, "<I")
        if version < 2:
            raise ValueError("GGUF v1 isn't supported")
        _read(f, "<Q")                   # tensor count (not needed)
        kv_count = _read(f, "<Q")
        raw: dict[str, Any] = {}
        for _ in range(kv_count):
            key = _string(f)
            vtype = _read(f, "<I")
            short = key.split(".", 1)[1] if "." in key else key
            keep = key == "general.architecture" or short in WANTED
            val = _value(f, vtype, keep)
            if keep:
                raw[key] = val
    arch = raw.get("general.architecture", "")
    out["arch"] = arch
    for k in WANTED:
        v = raw.get(f"{arch}.{k}")
        if isinstance(v, list):          # per-layer values (some models): use the largest
            v = max(v) if v else None
        if v is not None:
            out[k] = v
    return out
