"""Unit tests for experiments/concerts/probe_common.py helpers."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_CONCERT_PROBE_COMMON_PATH = (
    Path(__file__).resolve().parent.parent / "experiments" / "concerts" / "probe_common.py"
)
_SPEC = importlib.util.spec_from_file_location("concert_probe_common", _CONCERT_PROBE_COMMON_PATH)
assert _SPEC and _SPEC.loader
concert_probe_common = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = concert_probe_common
_SPEC.loader.exec_module(concert_probe_common)

artifact_filename = concert_probe_common.artifact_filename
build_normalized_event = concert_probe_common.build_normalized_event
classify_genre_signal = concert_probe_common.classify_genre_signal
classify_seatgeek_taxonomies = concert_probe_common.classify_seatgeek_taxonomies
count_by_genre = concert_probe_common.count_by_genre
count_by_taxonomy_token = concert_probe_common.count_by_taxonomy_token
is_collision_resistant_artifact_name = concert_probe_common.is_collision_resistant_artifact_name
make_event_dedupe_key = concert_probe_common.make_event_dedupe_key
make_provider_dedupe_key = concert_probe_common.make_provider_dedupe_key
normalize_dedupe_text = concert_probe_common.normalize_dedupe_text
normalize_local_starts_at = concert_probe_common.normalize_local_starts_at
save_artifact = concert_probe_common.save_artifact
split_taxonomy_tokens = concert_probe_common.split_taxonomy_tokens
summarize_event_duplicates = concert_probe_common.summarize_event_duplicates
ARTIFACTS_ROOT = concert_probe_common.ARTIFACTS_ROOT


class TestNormalizeDedupeText:
    def test_collapses_punctuation_and_case(self):
        assert normalize_dedupe_text("  Black Label Society!! ") == "black label society"

    def test_empty(self):
        assert normalize_dedupe_text("") == ""


class TestNormalizeLocalStartsAt:
    def test_date_only(self):
        assert normalize_local_starts_at("2026-09-05") == "2026-09-05"

    def test_iso_datetime(self):
        assert normalize_local_starts_at("2026-09-05T20:00:00Z") == "2026-09-05 20:00"


class TestDedupeKeys:
    def test_provider_dedupe_key(self):
        assert make_provider_dedupe_key("ticketmaster", "vvG1JZ") == "ticketmaster|vvG1JZ"

    def test_event_dedupe_key_stable_for_same_show(self):
        key_a = make_event_dedupe_key(
            artist_or_title="Sevendust",
            venue="Boeing Center at Tech Port",
            starts_at="2026-09-11T20:00:00Z",
        )
        key_b = make_event_dedupe_key(
            artist_or_title="sevendust",
            venue="Boeing Center at Tech Port",
            starts_at="2026-09-11T20:00:00Z",
        )
        assert key_a == key_b
        assert key_a.startswith("event|")

    def test_event_dedupe_key_differs_for_different_provider_ids(self):
        event_a = build_normalized_event(
            source="ticketmaster",
            provider_event_id="id-1",
            artist_or_title="INOHA",
            venue="Paper Tiger",
            starts_at="2026-09-20T19:00:00Z",
        )
        event_b = build_normalized_event(
            source="ticketmaster",
            provider_event_id="id-2",
            artist_or_title="INOHA",
            venue="Paper Tiger",
            starts_at="2026-09-20T19:00:00Z",
        )
        assert event_a.dedupe_key != event_b.dedupe_key
        assert event_a.event_dedupe_key == event_b.event_dedupe_key


class TestDuplicateSummary:
    def test_summarize_event_duplicates(self):
        events = [
            build_normalized_event(
                source="ticketmaster",
                provider_event_id="a",
                artist_or_title="Scene Queen",
                venue="Paper Tiger",
                starts_at="2026-09-27T19:00:00Z",
                genre_or_classification="Rock",
            ),
            build_normalized_event(
                source="ticketmaster",
                provider_event_id="b",
                artist_or_title="Scene Queen",
                venue="Paper Tiger",
                starts_at="2026-09-27T19:00:00Z",
                genre_or_classification="Rock",
            ),
            build_normalized_event(
                source="ticketmaster",
                provider_event_id="c",
                artist_or_title="Other Act",
                venue="Paper Tiger",
                starts_at="2026-09-28T19:00:00Z",
            ),
        ]
        groups = summarize_event_duplicates(events)
        assert len(groups) == 1
        assert groups[0]["count"] == 2
        assert groups[0]["provider_event_ids"] == ["a", "b"]


class TestGenreCounts:
    def test_count_by_genre(self):
        events = [
            build_normalized_event(
                source="ticketmaster",
                provider_event_id="1",
                artist_or_title="A",
                genre_or_classification="Rock",
            ),
            build_normalized_event(
                source="ticketmaster",
                provider_event_id="2",
                artist_or_title="B",
                genre_or_classification="Pop",
            ),
            build_normalized_event(
                source="ticketmaster",
                provider_event_id="3",
                artist_or_title="C",
                genre_or_classification="Rock",
            ),
        ]
        assert count_by_genre(events) == {"Rock": 2, "Pop": 1}


class TestClassifyGenreSignal:
    def test_positive(self):
        assert classify_genre_signal("Rock") == "positive"
        assert classify_genre_signal("Hard Rock") == "positive"

    def test_negative(self):
        assert classify_genre_signal("Pop") == "negative"
        assert classify_genre_signal("Country") == "negative"
        assert classify_genre_signal("Other") == "negative"

    def test_neutral_unknown(self):
        assert classify_genre_signal("Bluegrass") == "neutral"


class TestArtifactFilename:
    def test_unique_suffix_pattern(self):
        name = artifact_filename("probe")
        assert name.startswith("probe_")
        assert name.endswith(".json")
        assert is_collision_resistant_artifact_name(name)
        assert re.search(r"^probe_\d{8}T\d{9}_\d+_[0-9a-f]{8}\.json$", name)

    def test_distinct_names(self):
        names = {artifact_filename("probe") for _ in range(20)}
        assert len(names) == 20

    def test_rejects_legacy_second_level_format(self):
        assert not is_collision_resistant_artifact_name("probe_20260701T204445Z.json")

    def test_save_artifact_uses_collision_resistant_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(concert_probe_common, "ARTIFACTS_ROOT", tmp_path)
        path = save_artifact("seatgeek", "probe", {"hello": "world"})
        assert is_collision_resistant_artifact_name(path.name)
        body = json.loads(path.read_text(encoding="utf-8"))
        assert body["artifact_filename_version"] == 2
        assert body["artifact_basename"] == path.name


class TestSeatGeekTaxonomies:
    def test_positive_concert_rock(self):
        assert classify_seatgeek_taxonomies("concert, rock") == "positive"

    def test_negative_sports(self):
        assert classify_seatgeek_taxonomies("sports, nfl") == "negative"

    def test_count_by_taxonomy_token(self):
        events = [
            build_normalized_event(
                source="seatgeek",
                provider_event_id="1",
                artist_or_title="A",
                genre_or_classification="concert, rock",
            ),
            build_normalized_event(
                source="seatgeek",
                provider_event_id="2",
                artist_or_title="B",
                genre_or_classification="sports, nfl",
            ),
        ]
        assert count_by_taxonomy_token(events)["concert"] == 1
        assert count_by_taxonomy_token(events)["sports"] == 1
        assert split_taxonomy_tokens("concert, rock") == ["concert", "rock"]
