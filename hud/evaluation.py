#!/usr/bin/env python3
"""Small, dependency-free accuracy metrics for transcription fixtures."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?", re.I)


def normalize_words(text: str) -> List[str]:
    """Return comparable lowercase words while ignoring punctuation/case."""
    return [word.lower() for word in WORD_RE.findall(text or "")]


@dataclass(frozen=True)
class WordErrorStats:
    """Levenshtein word-error counts and their reference-relative rate."""

    reference_words: int
    hypothesis_words: int
    substitutions: int
    deletions: int
    insertions: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def wer(self) -> Optional[float]:
        if self.reference_words:
            return self.errors / self.reference_words
        # WER is undefined for a silence-only reference when the hypothesis
        # contains words; insertion counts remain the meaningful signal.
        return 0.0 if not self.hypothesis_words else None

    def as_dict(self) -> dict:
        return {
            "reference_words": self.reference_words,
            "hypothesis_words": self.hypothesis_words,
            "substitutions": self.substitutions,
            "deletions": self.deletions,
            "insertions": self.insertions,
            "errors": self.errors,
            "wer": round(self.wer, 6) if self.wer is not None else None,
        }


def word_error_stats(reference: str, hypothesis: str) -> WordErrorStats:
    """Compute deterministic word-level Levenshtein alignment statistics.

    Ties prefer a match, then substitution, deletion, and insertion. The
    ordering keeps fixture results stable when several alignments cost the
    same amount.
    """
    ref = normalize_words(reference)
    hyp = normalize_words(hypothesis)
    rows = len(ref) + 1
    cols = len(hyp) + 1
    # Each cell is (cost, substitutions, deletions, insertions, priority).
    table: List[List[Tuple[int, int, int, int, int]]] = [
        [(0, 0, 0, 0, 0) for _ in range(cols)] for _ in range(rows)]
    for i in range(1, rows):
        table[i][0] = (i, 0, i, 0, 2)
    for j in range(1, cols):
        table[0][j] = (j, 0, 0, j, 3)

    for i in range(1, rows):
        for j in range(1, cols):
            if ref[i - 1] == hyp[j - 1]:
                match = table[i - 1][j - 1]
                table[i][j] = (match[0], match[1], match[2], match[3], 0)
                continue
            substitution = table[i - 1][j - 1]
            deletion = table[i - 1][j]
            insertion = table[i][j - 1]
            candidates = [
                (substitution[0] + 1, substitution[1] + 1,
                 substitution[2], substitution[3], 1),
                (deletion[0] + 1, deletion[1], deletion[2] + 1,
                 deletion[3], 2),
                (insertion[0] + 1, insertion[1], insertion[2],
                 insertion[3] + 1, 3),
            ]
            table[i][j] = min(candidates, key=lambda value: (value[0], value[4]))

    result = table[-1][-1]
    return WordErrorStats(
        reference_words=len(ref),
        hypothesis_words=len(hyp),
        substitutions=result[1],
        deletions=result[2],
        insertions=result[3],
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Score a transcript against a reference")
    parser.add_argument("reference", type=Path)
    parser.add_argument("hypothesis", type=Path)
    args = parser.parse_args(argv)
    stats = word_error_stats(
        args.reference.read_text(encoding="utf-8"),
        args.hypothesis.read_text(encoding="utf-8"),
    )
    print(json.dumps(stats.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
