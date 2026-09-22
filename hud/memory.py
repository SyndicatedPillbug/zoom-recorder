#!/usr/bin/env python3
"""Cheap, deterministic structured memory for a live meeting.

This deliberately does not call a provider. It extracts only high-signal
patterns from committed transcript text, attaches the source event as
evidence, and leaves interpretation to the answer prompt. That keeps memory
updates fast and makes every item auditable after the call.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

DECISION_RE = re.compile(
    r"\b(?:we\s+(?:decided|agreed)|the\s+decision\s+is|let['’]s|we['’]ll)\b",
    re.I,
)
COMMITMENT_RE = re.compile(
    r"\b(?:i['’]ll|i\s+will|we['’]ll|we\s+will|need\s+to|next\s+step|action\s+item)\b",
    re.I,
)
NUMBER_RE = re.compile(
    r"(?:\$\s?\d[\d,.]*|\b\d+(?:\.\d+)?\s?(?:%|percent|ms|seconds?|minutes?|hours?|days?|weeks?|months?|years?)\b|"
    r"\b(?:Q[1-4]|20\d{2})\b)",
    re.I,
)


def extract_memory(text: str, speaker: str = "", ts: float = 0.0,
                   event_id: int = 0) -> List[Dict[str, Any]]:
    """Extract zero or more small facts, preserving the exact utterance."""
    utterance = " ".join((text or "").split())
    if len(utterance.split()) < 4:
        return []
    out: List[Dict[str, Any]] = []
    if DECISION_RE.search(utterance):
        out.append({"kind": "decision", "text": utterance,
                    "evidence": utterance, "speaker": speaker,
                    "ts": ts, "event_id": event_id})
    if COMMITMENT_RE.search(utterance):
        out.append({"kind": "commitment", "text": utterance,
                    "evidence": utterance, "speaker": speaker,
                    "ts": ts, "event_id": event_id})
    if NUMBER_RE.search(utterance):
        out.append({"kind": "fact_with_number", "text": utterance,
                    "evidence": utterance, "speaker": speaker,
                    "ts": ts, "event_id": event_id})
    return out


def format_memory(items: List[Dict[str, Any]], limit: int = 12) -> str:
    """Format the compact memory for a prompt without losing provenance."""
    lines = []
    for item in items[-max(1, limit):]:
        stamp = item.get("ts")
        label = str(item.get("kind") or "memory")
        evidence = str(item.get("evidence") or item.get("text") or "")
        lines.append("- [{}] {} — evidence: {}".format(label, evidence, evidence))
    return "\n".join(lines)
