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


# What an error looks like, and the run-specific noise inside it.
_SIGNAL = re.compile(
    r"(?i)(error|exception|fatal|no such|not found|denied|refused|failed|cannot|can't|"
    r"could not|invalid|unknown|missing|timed out|errno)"
)
# Lines every failure shares: they say *that* it failed, not *how*.
_BOILERPLATE = re.compile(
    r"(?i)^(traceback \(most recent call last\):?|file \".*\", line \d+.*|"
    r"exit code \d+|exit=\d+)$"
)
_PATH = re.compile(r"(?:[A-Za-z]:)?[\w.~<>-]*(?:[\\/][\w.~<>-]+)+[\\/]?")
_HEX = re.compile(r"\b[0-9a-f]{7,64}\b")
_NUMBER = re.compile(r"\b\d+\b")


def error_signature(text, max_lines=4):
    """The part of an episode worth matching on: its error lines, with paths,
    hashes and numbers masked, so the same failure in another command, file
    or session lands next to it.

    Episode text is `<tool> <file> <command> → <error output>`; the command
    and any stdout around the error are noise for "have we seen this before".
    A short query (no arrow, no error-looking line) is its own signature.
    """
    part = (text or "").split("→", 1)[1] if "→" in (text or "") else (text or "")
    lines = [line.strip() for line in part.splitlines() if line.strip()]
    lines = [line for line in lines if not _BOILERPLATE.match(line)] or lines
    hits = [line for line in lines if _SIGNAL.search(line)]
    chosen = (hits or lines)[-max_lines:]
    signature = " | ".join(chosen)
    signature = _PATH.sub("<path>", signature)
    signature = _HEX.sub("<sha>", signature)
    signature = _NUMBER.sub("<n>", signature)
    return re.sub(r"\s+", " ", signature).strip()[:400]


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
