#!/usr/bin/env python3
"""Background knowledge base: chunk ``.md`` files and retrieve relevant context.

Chunking is dependency-free. Embeddings come from a pluggable backend:

  * ``sentence-transformers`` -- fully local (needs the extra package);
  * ``ollama`` -- local embeddings over its OpenAI-compatible endpoint;
  * ``openai`` -- remote embeddings using the configured key.

Vectors are stored and compared in pure Python (no numpy), so the KB works with
any of the above and the retrieved snippets -- never the whole corpus -- are the
only notes that join an answer prompt.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

DEFAULT_CACHE = Path.home() / ".cache" / "zoom-recorder" / "kb"
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


# --------------------------------------------------------------------------
# Pure chunking (unit-tested, no third-party deps)
# --------------------------------------------------------------------------
def chunk_markdown(text: str, source: str, target_chars: int = 2400,
                   overlap_chars: int = 300) -> List[Dict[str, str]]:
    """Split markdown into heading-anchored, paragraph-aligned chunks."""
    if not text or not text.strip():
        return []
    heading_stack: List[str] = []
    blocks: List["tuple[str, str]"] = []  # (heading, paragraph)

    for raw_line in text.splitlines():
        m = HEADING_RE.match(raw_line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(title)
            continue
        blocks.append((" > ".join(heading_stack), raw_line))

    # Re-group lines into paragraphs separated by blank lines.
    paragraphs: List["tuple[str, str]"] = []
    current_heading = ""
    current_lines: List[str] = []
    for heading, line in blocks:
        if line.strip() == "":
            if current_lines:
                paragraphs.append((current_heading, "\n".join(current_lines).strip()))
                current_lines = []
            current_heading = heading or current_heading
            continue
        if heading and heading != current_heading and current_lines:
            paragraphs.append((current_heading, "\n".join(current_lines).strip()))
            current_lines = []
        current_heading = heading or current_heading
        current_lines.append(line)
    if current_lines:
        paragraphs.append((current_heading, "\n".join(current_lines).strip()))

    chunks: List[Dict[str, str]] = []
    buf = ""
    buf_heading = ""
    for heading, para in paragraphs:
        if not para:
            continue
        if buf and (len(buf) + len(para) + 2 > target_chars or heading != buf_heading):
            chunks.append(_make_chunk(buf_heading, buf, source))
            tail = buf[-overlap_chars:] if overlap_chars else ""
            buf = (tail + "\n\n" + para) if tail else para
        else:
            buf = (buf + "\n\n" + para) if buf else para
        buf_heading = heading
    if buf:
        chunks.append(_make_chunk(buf_heading, buf, source))
    return chunks


def _make_chunk(heading: str, body: str, source: str) -> Dict[str, str]:
    return {"source": source, "heading": heading or source, "text": body.strip()}


def _iter_markdown_files(dirs: List[str]):
    for d in dirs:
        root = Path(os.path.expanduser(d))
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(root.rglob("*.md"))
        for path in paths:
            if path.is_file() and path.suffix.lower() in (".md", ".markdown"):
                yield path


def _fingerprint(files: List[Path], salt: str = "") -> str:
    h = hashlib.sha256()
    h.update(salt.encode("utf-8"))
    for path in sorted(files):
        try:
            stat = path.stat()
        except OSError:
            continue
        h.update(str(path).encode("utf-8"))
        h.update(str(int(stat.st_mtime)).encode())
        h.update(str(stat.st_size).encode())
    return h.hexdigest()


def _norm(vector: List[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# --------------------------------------------------------------------------
# Embedding backends
# --------------------------------------------------------------------------
class LocalEmbedder:
    """sentence-transformers, fully on-device."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # lazy, optional

        self.model = SentenceTransformer(model_name)
        self.model_name = model_name
        self.label = "sentence-transformers:{}".format(model_name)

    def encode(self, texts: List[str]) -> List[List[float]]:
        vectors = self.model.encode(
            list(texts), normalize_embeddings=True, batch_size=32,
            show_progress_bar=False)
        return [[float(x) for x in v] for v in vectors]


class RemoteEmbedder:
    """Embeddings over an OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(self, client, model: str, name: str = "remote",
                 batch_size: int = 64) -> None:
        self.client = client
        self.model_name = model
        self.label = "{}:{}".format(name, model)
        self.batch_size = batch_size

    def encode(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        items = list(texts)
        for start in range(0, len(items), self.batch_size):
            batch = items[start:start + self.batch_size]
            out.extend(self.client.embed(batch, self.model_name))
        return out


class HashingEmbedder:
    """Zero-dependency fallback embedder using the hashing trick.

    Tokenizes text, hashes each token into a fixed-dimensional space with a
    sign from a second hash (feature hashing), and L2-normalizes the result.
    This captures lexical overlap well enough for cosine-similarity retrieval
    when no neural model is available, and works with zero setup or downloads.
    Not as semantically rich as a transformer model, but always available.
    """

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self.label = "hashing-{}".format(dim)

    def _hash(self, token: str, seed: int) -> int:
        h = hashlib.md5("{}:{}".format(seed, token).encode("utf-8")).hexdigest()
        return int(h[:8], 16)

    def encode(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            tokens = re.findall(r"[a-z0-9']+", (text or "").lower())
            for token in tokens:
                idx = self._hash(token, 0) % self.dim
                sign = 1.0 if (self._hash(token, 1) % 2 == 0) else -1.0
                vec[idx] += sign
            norm = math.sqrt(sum(x * x for x in vec))
            if norm > 0:
                vec = [x / norm for x in vec]
            out.append(vec)
        return out


# --------------------------------------------------------------------------
# Embedding index
# --------------------------------------------------------------------------
@dataclass
class KBSnippet:
    source: str
    heading: str
    text: str
    score: float


class KBIndex:
    def __init__(self, dirs: List[str], embedder, cache_dir: Optional[str] = None,
                 log: Optional[Callable[[str], None]] = None,
                 min_score: float = 0.1, max_chunks: int = 0) -> None:
        self.dirs = [d for d in dirs if d]
        self.embedder = embedder
        self.cache_dir = Path(os.path.expanduser(cache_dir)) if cache_dir else DEFAULT_CACHE
        self.log = log or (lambda _m: None)
        self.min_score = min_score
        self.max_chunks = max_chunks
        self._chunks: List[Dict[str, str]] = []
        self._vectors: List[List[float]] = []
        self._ready = False
        self._lock = threading.Lock()

    # -- build -------------------------------------------------------------
    def build(self, force: bool = False) -> bool:
        if not self.dirs:
            return False
        files = list(_iter_markdown_files(self.dirs))
        if not files:
            self.log("KB: no markdown files found in {}".format(", ".join(self.dirs)))
            return False
        fingerprint = _fingerprint(files, salt=getattr(self.embedder, "label", ""))
        meta_file = self.cache_dir / "index.json"
        vectors_file = self.cache_dir / "vectors.json.gz"
        if not force and meta_file.is_file() and vectors_file.is_file():
            if self._load_cache(meta_file, vectors_file, fingerprint):
                self._ready = True
                return True

        chunks: List[Dict[str, str]] = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunks.extend(chunk_markdown(text, str(path)))
        if not chunks:
            return False
        try:
            vectors = self.embedder.encode([c["text"] for c in chunks])
        except Exception as exc:  # noqa: BLE001
            self.log("KB: failed to embed ({}); continuing without KB".format(exc))
            return False
        if len(vectors) != len(chunks):
            self.log("KB: embedding count mismatch ({} != {}); skipping KB".format(
                len(vectors), len(chunks)))
            return False

        self._chunks = chunks
        self._vectors = vectors
        self._ready = True
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            meta_file.write_text(json.dumps(
                {"fingerprint": fingerprint,
                 "embedder": getattr(self.embedder, "label", ""),
                 "chunks": chunks}), encoding="utf-8")
            with gzip.open(vectors_file, "wt", encoding="utf-8") as fh:
                json.dump(vectors, fh)
        except OSError as exc:
            self.log("KB: could not write cache ({})".format(exc))
        self.log("KB: indexed {} chunk(s) from {} file(s) [{}]".format(
            len(chunks), len(files), getattr(self.embedder, "label", "?")))
        return True

    def _load_cache(self, meta_file: Path, vectors_file: Path, fingerprint: str) -> bool:
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("fingerprint") != fingerprint:
                return False
            with gzip.open(vectors_file, "rt", encoding="utf-8") as fh:
                vectors = json.load(fh)
            self._chunks = meta.get("chunks") or []
            self._vectors = vectors
            if len(self._vectors) != len(self._chunks):
                return False
            self.log("KB: loaded cached index ({} chunks, {})".format(
                len(self._chunks), getattr(self.embedder, "label", "?")))
            return True
        except Exception:  # noqa: BLE001
            return False

    def init_empty(self) -> None:
        """Mark the index ready with no chunks (for incremental live indexing)."""
        with self._lock:
            self._ready = True

    def add_chunks(self, chunks: List[Dict[str, str]]) -> int:
        """Embed and append live chunks without rebuilding from disk.

        Used by the answer engine to keep the live conversation searchable
        alongside the static .md corpus. The embedding call happens outside
        the lock; the append is thread-safe. Returns the number added.
        """
        if not chunks:
            return 0
        try:
            vectors = self.embedder.encode([c["text"] for c in chunks])
        except Exception as exc:  # noqa: BLE001
            self.log("KB: live add_chunks embed failed ({})".format(exc))
            return 0
        if len(vectors) != len(chunks):
            self.log("KB: live add_chunks count mismatch ({} != {})".format(
                len(vectors), len(chunks)))
            return 0
        with self._lock:
            self._chunks.extend(chunks)
            self._vectors.extend(vectors)
            self._ready = True
            if self.max_chunks and len(self._chunks) > self.max_chunks:
                excess = len(self._chunks) - self.max_chunks
                del self._chunks[:excess]
                del self._vectors[:excess]
        return len(chunks)

    # -- query -------------------------------------------------------------
    def query(self, text: str, top_k: int = 5) -> List[KBSnippet]:
        if not self._ready or not text.strip():
            return []
        try:
            query_vec = self.embedder.encode([text])[0]
        except Exception as exc:  # noqa: BLE001
            self.log("KB query failed: {}".format(exc))
            return []
        qnorm = _norm(query_vec)
        if not qnorm:
            return []

        # Snapshot under the lock so a concurrent add_chunks can't mutate
        # the lists mid-iteration.
        with self._lock:
            chunks = list(self._chunks)
            vectors = list(self._vectors)

        scored: List["tuple[float, int]"] = []
        for idx, vec in enumerate(vectors):
            norm = _norm(vec)
            if not norm:
                continue
            score = _dot(query_vec, vec) / (qnorm * norm)
            if score > self.min_score:
                scored.append((score, idx))
        scored.sort(reverse=True)

        out: List[KBSnippet] = []
        for score, idx in scored[: max(1, top_k)]:
            if idx >= len(chunks):
                continue
            chunk = chunks[int(idx)]
            out.append(KBSnippet(
                source=Path(chunk["source"]).name,
                heading=chunk.get("heading", ""),
                text=chunk["text"],
                score=float(score),
            ))
        return out
