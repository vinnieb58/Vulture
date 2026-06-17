"""Tests for Finch preference-key normalization."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finch.preference_norm import normalize_preference_key


class TestNormalizePreferenceKey:
    def test_bagel_bagels(self):
        assert normalize_preference_key("bagel") == "bagel"
        assert normalize_preference_key("bagels") == "bagel"
        assert normalize_preference_key("  Bagels  ") == "bagel"

    def test_banana_bananas(self):
        assert normalize_preference_key("banana") == "banana"
        assert normalize_preference_key("bananas") == "banana"

    def test_berry_berries(self):
        assert normalize_preference_key("berry") == "berry"
        assert normalize_preference_key("berries") == "berry"

    def test_tomato_tomatoes(self):
        assert normalize_preference_key("tomato") == "tomato"
        assert normalize_preference_key("tomatoes") == "tomato"

    def test_multi_word_unchanged_plural(self):
        assert normalize_preference_key("coffee pods") == "coffee pods"
        assert normalize_preference_key("  Coffee Pods!  ") == "coffee pods"

    def test_punctuation_and_spacing(self):
        assert normalize_preference_key("bagels,") == "bagel"
        assert normalize_preference_key("  BAGEL  ") == "bagel"

    def test_eggs_not_over_singularized(self):
        assert normalize_preference_key("eggs") == "eggs"
