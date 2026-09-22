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
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

DEFAULT_CACHE = Path.home() / ".cache" / "zoom-recorder" / "kb"
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'_-]+", re.I)
IGNORED_DIRS = {".obsidian", ".git", ".trash", ".stfolder", "node_modules",
                "__pycache__"}
LEXICAL_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do",
    "for", "from", "how", "i", "in", "is", "it", "me", "of", "on", "or",
    "that", "the", "this", "to", "was", "we", "what", "when", "where", "which",
    "who", "why", "with", "would", "you", "your",
}


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
    metadata = _frontmatter(text)
    if metadata:
        for chunk in chunks:
            chunk["metadata"] = dict(metadata)
    return chunks


def _make_chunk(heading: str, body: str, source: str) -> Dict[str, str]:
    return {"source": source, "heading": heading or source, "text": body.strip()}


def _frontmatter(text: str) -> Dict[str, str]:
    """Read lightweight Obsidian YAML metadata without requiring PyYAML."""
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: Dict[str, str] = {}
    for line in lines[1:80]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip().strip("[]")
        if key and value:
            out[key] = value
    return out


class SQLiteLexicalIndex:
    """Persistent FTS5 side index for very large Markdown vaults."""

    def __init__(self, path: Path, log: Callable[[str], None]) -> None:
        self.path = path
        self.log = log
        self.enabled = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self.path)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, source TEXT, heading TEXT, text TEXT)")
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(source, heading, text, content='chunks', content_rowid='id')")
                db.execute("CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN INSERT INTO chunks_fts(rowid, source, heading, text) VALUES (new.id, new.source, new.heading, new.text); END")
                db.execute("CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN INSERT INTO chunks_fts(chunks_fts, rowid, source, heading, text) VALUES ('delete', old.id, old.source, old.heading, old.text); END")
                db.execute("CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN INSERT INTO chunks_fts(chunks_fts, rowid, source, heading, text) VALUES ('delete', old.id, old.source, old.heading, old.text); INSERT INTO chunks_fts(rowid, source, heading, text) VALUES (new.id, new.source, new.heading, new.text); END")
            self.enabled = True
        except (OSError, sqlite3.Error) as exc:
            self.log("KB: SQLite FTS unavailable; using in-memory lexical index ({})".format(exc))

    def replace(self, chunks: List[Dict[str, str]]) -> None:
        if not self.enabled:
            return
        try:
            with sqlite3.connect(str(self.path)) as db:
                db.execute("DELETE FROM chunks")
                db.executemany("INSERT INTO chunks(source, heading, text) VALUES (?, ?, ?)",
                                [(str(c.get("source", "")), str(c.get("heading", "")),
                                  str(c.get("text", ""))) for c in chunks])
        except sqlite3.Error as exc:
            self.log("KB: SQLite lexical update failed ({})".format(exc))

    def query(self, text: str, limit: int = 1000) -> List[Tuple[str, str, str]]:
        if not self.enabled:
            return []
        terms = [t for t in _tokens(text) if len(t) > 1]
        if not terms:
            return []
        match = " OR ".join('"{}"'.format(t.replace('"', '')) for t in terms[:24])
        try:
            with sqlite3.connect(str(self.path)) as db:
                rows = db.execute(
                    "SELECT source, heading, text FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT ?",
                    (match, int(limit))).fetchall()
            return [(str(a), str(b), str(c)) for a, b, c in rows]
        except sqlite3.Error:
            return []


def _iter_markdown_files(dirs: List[str], onerror: Optional[Callable[[str], None]] = None):
    for d in dirs:
        root = Path(os.path.expanduser(d))
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.lower() in (".md", ".markdown"):
                yield root
            continue
        def _walk_error(exc: OSError) -> None:
            if onerror:
                onerror(str(exc))
        for current, dirnames, filenames in os.walk(root, onerror=_walk_error):
            dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_DIRS)
            for name in sorted(filenames):
                path = Path(current) / name
                if path.suffix.lower() in (".md", ".markdown"):
                    yield path


def _fingerprint(files: List[Path], salt: str = "") -> str:
    h = hashlib.sha256()
    h.update(salt.encode("utf-8"))
    for path in sorted(files):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        h.update(str(path).encode("utf-8"))
        h.update(hashlib.sha256(content).digest())
    return h.hexdigest()


def _norm(vector: List[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _tokens(text: str) -> List[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "")
            if token.lower() not in LEXICAL_STOPWORDS]


def _lexical_score(query: str, chunk: Dict[str, str]) -> float:
    """Score exact meaningful-term coverage, independent of embeddings."""
    query_tokens = set(_tokens(query))
    if not query_tokens:
        return 0.0
    haystack = set(_tokens("{}\n{}".format(chunk.get("heading", ""),
                                        chunk.get("text", ""))))
    overlap = len(query_tokens & haystack) / len(query_tokens)
    phrase = " ".join(_tokens(query))
    body = "{} {}".format(chunk.get("heading", ""), chunk.get("text", "")).lower()
    if phrase and phrase in body:
        overlap = min(1.0, overlap + 0.15)
    return overlap


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
        self._token_index: Dict[str, List[int]] = {}
        self._ready = False
        self._lock = threading.Lock()
        self._sqlite = SQLiteLexicalIndex(self.cache_dir / "lexical.sqlite3", self.log)

    def _rebuild_token_index(self) -> None:
        index: Dict[str, List[int]] = {}
        for number, chunk in enumerate(self._chunks):
            for token in set(_tokens("{}\n{}".format(
                    chunk.get("heading", ""), chunk.get("text", "")))):
                index.setdefault(token, []).append(number)
        self._token_index = index

    def _embedder_label(self) -> str:
        # The suffix invalidates old body-only vectors after adding headings to
        # the embedding input.  It also keeps caches for different embedders
        # completely separate.
        return "{}:heading-v1".format(getattr(self.embedder, "label", ""))

    @staticmethod
    def _embedding_text(chunk: Dict[str, str]) -> str:
        return "{}\n{}".format(chunk.get("heading", ""), chunk.get("text", ""))

    def _file_cache_path(self, path: Path) -> Path:
        key = hashlib.sha256((str(path) + "\0" + self._embedder_label()).encode(
            "utf-8")).hexdigest()
        return self.cache_dir / "files" / (key + ".json.gz")

    def _load_file_cache(self, cache_file: Path, content_fingerprint: str
                         ) -> Optional[Tuple[List[Dict[str, str]], List[List[float]]]]:
        try:
            with gzip.open(cache_file, "rt", encoding="utf-8") as fh:
                cached = json.load(fh)
            chunks = cached.get("chunks") or []
            vectors = cached.get("vectors") or []
            if (cached.get("content_fingerprint") != content_fingerprint
                    or cached.get("embedder") != self._embedder_label()
                    or len(chunks) != len(vectors)):
                return None
            return chunks, vectors
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _atomic_write(path: Path, writer: Callable[[str], None]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=str(path.parent),
                    prefix=".kb-", suffix=".tmp", delete=False) as fh:
                temp_name = fh.name
                writer(fh.name)
            os.replace(temp_name, path)
        finally:
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def _save_file_cache(self, path: Path, content_fingerprint: str,
                         chunks: List[Dict[str, str]],
                         vectors: List[List[float]]) -> None:
        cache_file = self._file_cache_path(path)

        def write(temp_name: str) -> None:
            with gzip.open(temp_name, "wt", encoding="utf-8") as fh:
                json.dump({"content_fingerprint": content_fingerprint,
                           "embedder": self._embedder_label(),
                           "chunks": chunks, "vectors": vectors}, fh)

        self._atomic_write(cache_file, write)

    # -- build -------------------------------------------------------------
    def build(self, force: bool = False) -> bool:
        if not self.dirs:
            return False
        walk_errors: List[str] = []
        files = list(_iter_markdown_files(self.dirs, walk_errors.append))
        for error in walk_errors:
            self.log("KB: could not inspect a vault path ({}); macOS may need "
                     "Files & Folders access for this vault".format(error))
        if not files:
            self.log("KB: no markdown files found in {}".format(", ".join(self.dirs)))
            return False
        embedder_label = self._embedder_label()
        fingerprint = _fingerprint(files, salt=embedder_label)
        meta_file = self.cache_dir / "index.json"
        vectors_file = self.cache_dir / "vectors.json.gz"
        if not force and meta_file.is_file() and vectors_file.is_file():
            if self._load_cache(meta_file, vectors_file, fingerprint):
                self._ready = True
                return True

        entries: List[Tuple[Path, str, List[Dict[str, str]],
                             Optional[List[List[float]]]]] = []
        reused_files = 0
        unreadable_files = 0
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                unreadable_files += 1
                self.log("KB: could not read {}; macOS may need Files & Folders "
                         "access for this vault".format(path))
                continue
            content_fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
            cached = None if force else self._load_file_cache(
                self._file_cache_path(path), content_fingerprint)
            if cached is not None:
                cached_chunks, cached_vectors = cached
                entries.append((path, content_fingerprint, cached_chunks,
                                cached_vectors))
                reused_files += 1
                continue
            file_chunks = chunk_markdown(text, str(path))
            entries.append((path, content_fingerprint, file_chunks, None))

        embedded_files = 0
        pending_indices = [index for index, (_path, _fingerprint, file_chunks, vectors)
                           in enumerate(entries) if vectors is None and file_chunks]
        if pending_indices:
            pending_chunks = [chunk for index in pending_indices
                              for chunk in entries[index][2]]
            try:
                pending_vectors = self.embedder.encode(
                    [self._embedding_text(c) for c in pending_chunks])
            except Exception as exc:  # noqa: BLE001
                self.log("KB: failed to embed ({}); continuing without KB".format(exc))
                return False
            if len(pending_vectors) != len(pending_chunks):
                self.log("KB: embedding count mismatch ({} != {}); skipping KB".format(
                    len(pending_vectors), len(pending_chunks)))
                return False
            offset = 0
            for index in pending_indices:
                path, content_fingerprint, file_chunks, _old_vectors = entries[index]
                count = len(file_chunks)
                file_vectors = pending_vectors[offset:offset + count]
                offset += count
                entries[index] = (path, content_fingerprint, file_chunks, file_vectors)
                embedded_files += 1
                try:
                    self._save_file_cache(path, content_fingerprint,
                                          file_chunks, file_vectors)
                except OSError as exc:
                    self.log("KB: could not write per-file cache for {} ({})".format(
                        path, exc))

        # Cached chunks and newly embedded chunks are accumulated together in
        # file order, keeping the vector list aligned with the chunk list.
        chunks: List[Dict[str, str]] = []
        vectors: List[List[float]] = []
        for _path, _content_fingerprint, file_chunks, file_vectors in entries:
            chunks.extend(file_chunks)
            vectors.extend(file_vectors or [])
        if not chunks:
            return False
        if len(vectors) != len(chunks):
            self.log("KB: vector/chunk assembly mismatch ({} != {}); skipping KB".format(
                len(vectors), len(chunks)))
            return False

        self._chunks = chunks
        self._vectors = vectors
        self._rebuild_token_index()
        self._sqlite.replace(chunks)
        self._ready = True
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            def write_meta(temp_name: str) -> None:
                Path(temp_name).write_text(
                    json.dumps({"fingerprint": fingerprint,
                                "embedder": embedder_label,
                                "chunks": chunks}), encoding="utf-8")

            def write_vectors(temp_name: str) -> None:
                with gzip.open(temp_name, "wt", encoding="utf-8") as fh:
                    json.dump(vectors, fh)

            self._atomic_write(meta_file, write_meta)
            self._atomic_write(vectors_file, write_vectors)
        except OSError as exc:
            self.log("KB: could not write cache ({})".format(exc))
        self.log("KB: indexed {} chunk(s) from {} file(s) [{}]; reused {}, "
                 "embedded {}".format(len(chunks), len(files), embedder_label,
                                       reused_files, embedded_files))
        if unreadable_files:
            self.log("KB: {} file(s) were unreadable and were skipped".format(
                unreadable_files))
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
            self._rebuild_token_index()
            self._sqlite.replace(self._chunks)
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
            vectors = self.embedder.encode([self._embedding_text(c) for c in chunks])
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
            self._rebuild_token_index()
            self._ready = True
            if self.max_chunks and len(self._chunks) > self.max_chunks:
                excess = len(self._chunks) - self.max_chunks
                del self._chunks[:excess]
                del self._vectors[:excess]
                self._rebuild_token_index()
            snapshot = list(self._chunks)
        self._sqlite.replace(snapshot)
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

        with self._lock:
            token_index = {token: list(indices)
                           for token, indices in self._token_index.items()}

        # Large vaults benefit from a cheap exact-term candidate set. If the
        # query has no lexical anchor, fall back to the full semantic scan so
        # conceptual/synonym matches are never discarded.
        candidate_indices = range(len(vectors))
        if len(vectors) > 5000:
            lexical_rows = self._sqlite.query(text, limit=2000)
            if lexical_rows:
                wanted = {(source, heading, body)
                          for source, heading, body in lexical_rows}
                candidates = {idx for idx, chunk in enumerate(chunks)
                              if (str(chunk.get("source", "")),
                                  str(chunk.get("heading", "")),
                                  str(chunk.get("text", ""))) in wanted}
            else:
                query_tokens = set(_tokens(text))
                candidates = {idx for token in query_tokens
                              for idx in token_index.get(token, [])}
            if candidates and len(candidates) < len(vectors) * 0.75:
                candidate_indices = candidates

        scored: List["tuple[float, int]"] = []
        for idx in candidate_indices:
            vec = vectors[idx]
            norm = _norm(vec)
            if not norm:
                continue
            semantic = _dot(query_vec, vec) / (qnorm * norm)
            lexical = _lexical_score(text, chunks[idx])
            score = (0.8 * semantic) + (0.2 * lexical)
            if score > self.min_score or lexical >= 0.5:
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
