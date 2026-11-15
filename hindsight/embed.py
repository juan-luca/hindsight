"""Hindsight embedder — deterministic, dependency-free text embeddings.

Feature-hashed word + character-trigram vectors (L2-normalized, cosine-ready).
Not a neural embedding: it captures lexical similarity, which is exactly what
"have we seen this error before?" needs — error strings repeat their tokens.

The embedder is pluggable: set HINDSIGHT_EMBEDDER to a `module:function` path
resolving to `fn(text) -> list[float]`, and HINDSIGHT_EMBED_DIM to its
dimension (e.g. 384 for all-MiniLM-L6-v2). The vector index is created with
`dim()` at ingest time; after switching embedders, delete graph/ and rerun
`hindsight ingest` — the store rebuilds losslessly from events + git +
transcripts.
"""
import hashlib
import math
import os
import re

DIM = 256


def dim():
    """Embedding dimension: the custom embedder's when configured, else DIM."""
    try:
        value = int(os.environ.get("HINDSIGHT_EMBED_DIM", ""))
        if value > 0:
            return value
    except ValueError:
        pass
    return DIM

_TOKEN = re.compile(r"[a-z0-9_./:-]+")


def _grams(text):
    tokens = _TOKEN.findall((text or "").lower())
    for token in tokens:
        yield token
        padded = "^%s$" % token
        for i in range(len(padded) - 2):
            yield padded[i:i + 3]


def _hashed(text, dim):
    vec = [0.0] * dim
    for gram in _grams(text):
        digest = hashlib.md5(gram.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[index] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


def _custom():
    spec = os.environ.get("HINDSIGHT_EMBEDDER", "")
    if ":" not in spec:
        return None
    module_name, func_name = spec.split(":", 1)
    try:
        import importlib
        return getattr(importlib.import_module(module_name), func_name)
    except Exception:
        return None


def embed(text):
    """Embed `text` into a dim()-dimensional vector (list of floats)."""
    custom = _custom()
    if custom is not None:
        try:
            vec = list(custom(text))
            if len(vec) == dim():
                return vec
        except Exception:
            pass
    return _hashed(text, dim())
