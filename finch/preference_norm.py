"""Shared preference-key normalization for Finch alias save and lookup."""

from __future__ import annotations

import re

_PUNCTUATION_RE = re.compile(r"[^\w\s-]")
_WHITESPACE_RE = re.compile(r"\s+")


def _singularize_word(word: str) -> str:
    """Conservative singular form for simple grocery plurals."""
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("oes"):
        return word[:-2]
    if word.endswith(("xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("ss") or word.endswith("us"):
        return word
    if word.endswith("s") and word[-2] != "g":
        return word[:-1]
    return word


def normalize_preference_key(text: str) -> str:
    """Normalize case, spacing, punctuation, and simple plural/singular variants."""
    cleaned = text.strip().lower()
    cleaned = _PUNCTUATION_RE.sub(" ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    if not cleaned:
        return cleaned
    words = cleaned.split()
    if len(words) == 1:
        return _singularize_word(words[0])
    return cleaned
