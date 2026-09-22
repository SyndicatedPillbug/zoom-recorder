#!/usr/bin/env python3
"""Small, dependency-free accuracy metrics for transcription fixtures."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


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


@dataclass(frozen=True)
class StablePrefixStats:
    """First-prefix stability timing from rolling interim event records."""

    observations: int
    first_stable_word: Optional[str]
    first_stable_word_index: Optional[int]
    first_stable_word_latency_seconds: Optional[float]

    def as_dict(self) -> dict:
        return {
            "observations": self.observations,
            "first_stable_word": self.first_stable_word,
            "first_stable_word_index": self.first_stable_word_index,
            "first_stable_word_latency_seconds": (
                round(self.first_stable_word_latency_seconds, 6)
                if self.first_stable_word_latency_seconds is not None else None),
        }


def stable_prefix_stats(events: Iterable[Dict[str, Any]]) -> StablePrefixStats:
    """Measure the first word repeated by consecutive non-empty hypotheses.

    ``captured_at`` is the rolling-window submission time and ``ts`` is the
    publication time. The resulting latency is therefore the time from the
    first hypothesis opportunity to the next hypothesis that confirms its
    prefix, not a claim about the word's exact acoustic end time.
    """
    previous: Optional[Tuple[List[str], Optional[float]]] = None
    observations = 0
    for event in events:
        words = normalize_words(str(event.get("text") or ""))
        if not words:
            continue
        observations += 1
        captured = event.get("captured_at")
        published = event.get("ts")
        try:
            captured_at = float(captured) if captured is not None else None
        except (TypeError, ValueError):
            captured_at = None
        try:
            published_at = float(published) if published is not None else None
        except (TypeError, ValueError):
            published_at = None
        if previous is not None:
            prior_words, prior_captured_at = previous
            common = 0
            for left, right in zip(prior_words, words):
                if left != right:
                    break
                common += 1
            if common:
                latency = None
                if prior_captured_at is not None and published_at is not None:
                    latency = max(0.0, published_at - prior_captured_at)
                return StablePrefixStats(
                    observations=observations,
                    first_stable_word=words[0],
                    first_stable_word_index=0,
                    first_stable_word_latency_seconds=latency,
                )
        previous = (words, captured_at)
    return StablePrefixStats(observations=observations,
                             first_stable_word=None,
                             first_stable_word_index=None,
                             first_stable_word_latency_seconds=None)


def first_stable_publication_stats(events: Iterable[Dict[str, Any]]) -> StablePrefixStats:
    """Measure the first stable word emitted by a live session event stream.

    The live writer stores stable interim commits as ``transcript`` events
    with ``finalized`` false. Their event-level ``latency`` is the measured
    inference-to-publication delay for that stable commit. This is distinct
    from the rolling-hypothesis metric above and is the metric available from
    persisted session logs.
    """
    observations = 0
    for event in events:
        if (event.get("type") != "transcript"
                or event.get("finalized", True)
                or not str(event.get("text") or "").strip()):
            continue
        observations += 1
        words = normalize_words(str(event.get("text") or ""))
        latency = event.get("latency")
        try:
            measured = float(latency) if latency is not None else None
        except (TypeError, ValueError):
            measured = None
        if measured is None:
            try:
                measured = max(0.0, float(event["ts"]) - float(event["captured_at"]))
            except (KeyError, TypeError, ValueError):
                measured = None
        return StablePrefixStats(
            observations=observations,
            first_stable_word=words[0] if words else None,
            first_stable_word_index=0 if words else None,
            first_stable_word_latency_seconds=measured,
        )
    return StablePrefixStats(observations=observations,
                             first_stable_word=None,
                             first_stable_word_index=None,
                             first_stable_word_latency_seconds=None)


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
