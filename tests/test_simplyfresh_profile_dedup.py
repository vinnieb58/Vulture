"""Tests for Simply Fresh profile card deduplication."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "simplyfresh_probe"))

from probe_common import ProfileConfig, deduplicate_profile_cards, profile_card_signature

_PARENT_CARD = (
    "Who are you ordering for?\n"
    "Vincent Bergeron\n"
    "Classroom: Toddler 1\n"
    "School: MEADOW MONTESSORI SCHOOL\n"
    "Select profile"
)

_CHILD_CARD = (
    "Vincent Bergeron\n"
    "Classroom: Toddler 1\n"
    "School: MEADOW MONTESSORI SCHOOL\n"
    "Select profile"
)


def test_nested_duplicate_profile_containers_collapse_to_one():
    raw = [{"text": _PARENT_CARD}, {"text": _CHILD_CARD}]
    deduped = deduplicate_profile_cards(raw)
    assert len(raw) == 2
    assert len(deduped) == 1
    assert profile_card_signature(_PARENT_CARD) == profile_card_signature(_CHILD_CARD)
    # Prefer inner (shorter) container that owns Select profile.
    assert deduped[0]["text"] == _CHILD_CARD


def test_deduplicated_profile_matches_vincent_filters():
    raw = [{"text": _PARENT_CARD}, {"text": _CHILD_CARD}]
    deduped = deduplicate_profile_cards(raw)
    config = ProfileConfig(
        profile_name="Vincent Bergeron",
        school="MEADOW MONTESSORI SCHOOL",
    )
    eligible = [
        c
        for c in deduped
        if config.profile_name.lower() in c["text"].lower()
        and config.school.lower() in c["text"].lower()
    ]
    assert len(eligible) == 1
