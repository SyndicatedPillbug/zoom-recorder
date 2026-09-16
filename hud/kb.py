#!/usr/bin/env python3
"""Background knowledge base: chunk ``.md`` files and retrieve relevant context.

Embeddings are computed locally with ``sentence-transformers`` so the knowledge
base itself never leaves the machine -- only the few retrieved snippets that are
relevant to the conversation get folded into the answer prompt. The imports are
lazy: if the embedding stack is not installed (it needs a separate venv on
Python 3.9), the HUD simply runs without KB grounding.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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


def _fingerprint(files: List[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        try:
            stat = path.stat()
        except OSError:
            continue
        h.update(str(path).encode("utf-8"))
        h.update(str(int(stat.st_mtime)).encode())
        h.update(str(stat.st_size).encode())
    return h.hexdigest()


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
    def __init__(self, dirs: List[str], model_name: str = "all-MiniLM-L6-v2",
                 cache_dir: Optional[str] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.dirs = [d for d in dirs if d]
        self.model_name = model_name
        self.cache_dir = Path(os.path.expanduser(cache_dir)) if cache_dir else DEFAULT_CACHE
        self.log = log or (lambda _m: None)
        self._model = None
        self._chunks: List[Dict[str, str]] = []
        self._vectors = None
        self._ready = False
        self._error = ""

    # -- availability ------------------------------------------------------
    def available(self) -> bool:
        return self._load_deps()

    def _load_deps(self) -> bool:
        if self._model is not None:
            return True
        try:
            import sentence_transformers  # noqa: F401
            import numpy  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            self._error = "sentence-transformers not installed ({})".format(exc)
            self.log("KB disabled: {}".format(self._error))
            return False
        return True

    # -- build -------------------------------------------------------------
    def build(self, force: bool = False) -> bool:
        if not self.dirs:
            return False
        if not self._load_deps():
            return False
        files = list(_iter_markdown_files(self.dirs))
        if not files:
            self.log("KB: no markdown files found in {}".format(", ".join(self.dirs)))
            return False
        fingerprint = _fingerprint(files)
        cache_file = self.cache_dir / "index.npz"
        meta_file = self.cache_dir / "index.json"
        if not force and cache_file.is_file() and meta_file.is_file():
            if self._load_cache(cache_file, meta_file, fingerprint):
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
            import numpy as np
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(self.model_name)
            vectors = model.encode(
                [c["text"] for c in chunks],
                normalize_embeddings=True, batch_size=32, show_progress_bar=False,
            )
            vectors = np.asarray(vectors, dtype="float32")
        except Exception as exc:  # noqa: BLE001
            self.log("KB: failed to embed ({}); continuing without KB".format(exc))
            return False

        self._model = model
        self._chunks = chunks
        self._vectors = vectors
        self._ready = True
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(cache_file, vectors=vectors)
            meta_file.write_text(json.dumps(
                {"fingerprint": fingerprint, "model": self.model_name,
                 "chunks": chunks}), encoding="utf-8")
        except OSError as exc:
            self.log("KB: could not write cache ({})".format(exc))
        self.log("KB: indexed {} chunk(s) from {} file(s)".format(len(chunks), len(files)))
        return True

    def _load_cache(self, cache_file: Path, meta_file: Path, fingerprint: str) -> bool:
        try:
            import numpy as np
            from sentence_transformers import SentenceTransformer
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("fingerprint") != fingerprint or meta.get("model") != self.model_name:
                return False
            vectors = np.load(cache_file)["vectors"]
            self._model = SentenceTransformer(self.model_name)
            self._chunks = meta.get("chunks") or []
            self._vectors = vectors
            self.log("KB: loaded cached index ({} chunks)".format(len(self._chunks)))
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- query -------------------------------------------------------------
    def query(self, text: str, top_k: int = 5) -> List[KBSnippet]:
        if not self._ready or not text.strip():
            return []
        try:
            import numpy as np
            vec = self._model.encode([text], normalize_embeddings=True)  # type: ignore[union-attr]
            vec = np.asarray(vec, dtype="float32")
            scores = (self._vectors @ vec.T).reshape(-1)  # type: ignore[operator]
            order = scores.argsort()[::-1][: max(1, top_k)]
            out: List[KBSnippet] = []
            for idx in order:
                if scores[idx] <= 0.1:
                    continue
                chunk = self._chunks[int(idx)]
                out.append(KBSnippet(
                    source=Path(chunk["source"]).name,
                    heading=chunk.get("heading", ""),
                    text=chunk["text"],
                    score=float(scores[idx]),
                ))
            return out
        except Exception as exc:  # noqa: BLE001
            self.log("KB query failed: {}".format(exc))
            return []
