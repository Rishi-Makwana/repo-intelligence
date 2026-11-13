"""The purpose line (LLM) and embeddings (Ollama), both cached by input hash in the `cache` table.

`complete` and `embed_texts` are the only functions that reach a provider; tests replace them with fakes.
LLM text is a search aid only: it is stored in repos.search_aid_purpose and a features(kind='purpose') row.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx

from .config import Settings

README_BYTES = 8 * 1024
MAX_LIBRARIES = 80
PURPOSE_MAX_TOKENS = 120
EMBED_BATCH = 64
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
SENTENCE_END = re.compile(r"(?<=[.!?])\s")
PURPOSE_SYSTEM = ("You describe what a software repository is for, in one plain sentence of at most 30 words. "
                  "The sentence only helps rank search results. Do not guess versions or owners.")


class ProviderError(Exception):
    pass


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cached(conn: sqlite3.Connection, kind: str, input_hash: str, model: str) -> str | None:
    row = conn.execute("SELECT output FROM cache WHERE kind = ? AND input_hash = ? AND model = ?",
                       (kind, input_hash, model)).fetchone()
    return row[0] if row else None


def _store(conn: sqlite3.Connection, kind: str, input_hash: str, model: str, output: str) -> None:
    conn.execute("INSERT OR REPLACE INTO cache (kind, input_hash, model, output) VALUES (?, ?, ?, ?)",
                 (kind, input_hash, model, output))


# --- providers ---------------------------------------------------------------------------------

def complete(settings: Settings, system: str, prompt: str) -> str:
    """One Anthropic Messages API call; returns the concatenated text blocks."""
    import anthropic

    if settings.llm_provider != "anthropic":
        raise ProviderError(f"LLM_PROVIDER {settings.llm_provider!r} is not supported; use anthropic")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60)
    message = client.messages.create(model=settings.llm_model, max_tokens=PURPOSE_MAX_TOKENS, system=system,
                                     messages=[{"role": "user", "content": prompt}])
    return "".join(block.text for block in message.content if block.type == "text")


def embed_texts(settings: Settings, texts: list[str]) -> list[list[float]]:
    """Ollama POST /api/embed with {model, input: [...]} -> {"embeddings": [[...], ...]}."""
    if settings.embed_provider != "ollama":
        raise ProviderError(f"EMBED_PROVIDER {settings.embed_provider!r} is not supported; use ollama")
    response = httpx.post(f"{settings.ollama_url.rstrip('/')}/api/embed",
                          json={"model": settings.embed_model, "input": texts}, timeout=120)
    response.raise_for_status()
    return response.json()["embeddings"]


def embedder_is_local(settings: Settings) -> bool:
    return urlparse(settings.ollama_url).hostname in LOCAL_HOSTS


# --- cached wrappers ---------------------------------------------------------------------------

def one_sentence(text: str) -> str:
    text = " ".join(text.split())
    return SENTENCE_END.split(text, maxsplit=1)[0] if text else ""


def purpose_prompt(readme: str, libraries: list[str], folders: list[str]) -> str:
    return (f"README (truncated):\n{readme}\n\nLibraries: {', '.join(libraries) or 'none'}\n"
            f"Top-level folders: {', '.join(folders) or 'none'}\n\nWhat is this repository for? One sentence.")


def purpose_line(conn: sqlite3.Connection, settings: Settings, prompt: str) -> str:
    key = _hash(PURPOSE_SYSTEM + "\n" + prompt)
    cached = _cached(conn, "purpose", key, settings.llm_model)
    if cached is not None:
        return cached
    sentence = one_sentence(complete(settings, PURPOSE_SYSTEM, prompt))
    if not sentence:
        raise ProviderError("the LLM returned no text")
    _store(conn, "purpose", key, settings.llm_model, sentence)
    return sentence


def embed(conn: sqlite3.Connection, settings: Settings, texts: list[str]) -> list[list[float]]:
    """Embeddings for `texts`, calling the provider only for texts not cached under EMBED_MODEL."""
    keys = [_hash(t) for t in texts]
    vectors: dict[str, list[float]] = {}
    for key in dict.fromkeys(keys):
        cached = _cached(conn, "embedding", key, settings.embed_model)
        if cached is not None:
            vectors[key] = json.loads(cached)
    missing = list(dict.fromkeys(t for t, k in zip(texts, keys, strict=True) if k not in vectors))
    for start in range(0, len(missing), EMBED_BATCH):
        batch = missing[start:start + EMBED_BATCH]
        result = embed_texts(settings, batch)
        if len(result) != len(batch):
            raise ProviderError(f"embedder returned {len(result)} vectors for {len(batch)} texts")
        for text, vector in zip(batch, result, strict=True):
            if len(vector) != settings.embed_dim:
                raise ProviderError(f"{settings.embed_model} returned {len(vector)} dimensions; "
                                    f"EMBED_DIM is {settings.embed_dim}")
            vectors[_hash(text)] = vector
            _store(conn, "embedding", _hash(text), settings.embed_model, json.dumps(vector))
    return [vectors[k] for k in keys]


def embed_query(conn: sqlite3.Connection, settings: Settings, query: str) -> list[float]:
    return embed(conn, settings, [settings.embed_query_prefix + query])[0]


# --- the stage ---------------------------------------------------------------------------------

def _readme(root: Path, files: list[str]) -> str:
    top = sorted((f for f in files if "/" not in f and PurePosixPath(f).name.lower().startswith("readme")),
                 key=lambda f: (not f.lower().endswith(".md"), f))
    if not top:
        return ""
    with open(root / top[0], "rb") as handle:
        return handle.read(README_BYTES).decode("utf-8", "replace")


def purpose_and_embeddings(conn: sqlite3.Connection, settings: Settings, root: Path, files: list[str],
                           libraries: list[dict], features: list[dict], folders: list[str], exclude_from_llm: bool,
                           commit_sha: str) -> tuple[dict, list[str]]:
    """Returns ({"features": [purpose row], "repo": {...}}, warnings). Sets "embedding" on every feature
    row in `features` in place, and on the purpose row."""
    warnings: list[str] = []
    new_features: list[dict] = []
    purpose = None
    if not exclude_from_llm and not (settings.llm_model and settings.anthropic_api_key):
        warnings.append("purpose line skipped: LLM_MODEL or ANTHROPIC_API_KEY is not set")
    elif not exclude_from_llm:
        names = sorted({lib["name"] for lib in libraries})[:MAX_LIBRARIES]
        try:
            purpose = purpose_line(conn, settings, purpose_prompt(_readme(root, files), names, folders))
        except Exception as exc:  # noqa: BLE001 - a search aid must not block the facts; recorded as an error
            # ponytail: retried only on the repo's next rebuild; add a retry queue if the LLM is often down.
            warnings.append(f"purpose line failed: {type(exc).__name__}")
    if purpose:
        new_features.append({"kind": "purpose", "text": purpose, "path": None, "line": None,
                             "commit_sha": commit_sha})

    to_embed = features + new_features
    if exclude_from_llm and not embedder_is_local(settings):
        warnings.append("embeddings skipped: exclude_from_llm is set and OLLAMA_URL is not a local host")
    elif to_embed:
        for row, vector in zip(to_embed, embed(conn, settings, [f["text"] for f in to_embed]), strict=True):
            row["embedding"] = vector
    return {"features": new_features, "repo": {"search_aid_purpose": purpose}}, warnings
