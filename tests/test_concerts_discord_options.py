"""Tests for typed Discord option handling and command registration."""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from concerts.discord_commands import (
    AREA_CHOICES,
    FILTER_OPTION_NAMES,
    assert_supported_area_choices,
)
from engine.concerts.command_router import dispatch_concert
from engine.concerts.query_parser import (
    FilterValidationError,
    criteria_from_args,
    merge_filters_from_args,
)
from engine.concerts.search import SearchResult
from engine.concerts.stats import SearchStats


class TestCriteriaFromArgs:
    def test_typed_options_only(self):
        criteria = criteria_from_args(
            {
                "artist": "Three Days Grace",
                "area": "houston",
                "days": 180,
            }
        )
        assert criteria.artist_query == "Three Days Grace"
        assert criteria.area == "houston"
        assert criteria.days_forward == 180

    def test_freeform_query_fallback(self):
        criteria = criteria_from_args(
            {"query": 'genre:"rock" area:"louisiana" days:365'}
        )
        assert criteria.genre == "rock"
        assert criteria.area == "louisiana"
        assert criteria.days_forward == 365

    def test_typed_overrides_freeform_conflict(self):
        criteria = criteria_from_args(
            {
                "query": 'artist:"Old Name" area:"dallas" days:90',
                "artist": "Shinedown",
                "area": "houston",
                "days": 365,
            }
        )
        assert criteria.artist_query == "Shinedown"
        assert criteria.area == "houston"
        assert criteria.days_forward == 365

    def test_explicit_city_state_radius(self):
        criteria = criteria_from_args(
            {
                "artist": "Disturbed",
                "city": "Houston",
                "state": "tx",
                "radius": 75,
                "days": 180,
            }
        )
        assert criteria.city == "Houston"
        assert criteria.state == "TX"
        assert criteria.radius_miles == 75

    def test_force_allows_noisy_nationwide_genre(self):
        criteria = criteria_from_args(
            {
                "genre": "rock",
                "area": "nationwide",
                "force": True,
                "days": 365,
            }
        )
        assert criteria.genre == "rock"
        assert criteria.area == "nationwide"

    def test_empty_input_rejected(self):
        with pytest.raises(FilterValidationError, match="typed options or a freeform"):
            criteria_from_args({})

    def test_nationwide_genre_without_force_rejected(self):
        with pytest.raises(FilterValidationError, match="too noisy"):
            criteria_from_args({"genre": "rock", "area": "nationwide"})


class TestSearchAndWatchTypedDispatch:
    @patch("engine.concerts.command_router.search_concerts")
    def test_search_accepts_typed_args(self, mock_search):
        mock_search.return_value = SearchResult(events=[], provider_notes=[], queries_run=0, stats=SearchStats())
        result = dispatch_concert(
            "search",
            {"artist": "Breaking Benjamin", "area": "texas", "days": 365},
        )
        assert result.success
        mock_search.assert_called_once()
        criteria = mock_search.call_args[0][0]
        assert criteria.artist_query == "Breaking Benjamin"
        assert criteria.area == "texas"

    @patch("engine.concerts.command_router.search_concerts")
    def test_watch_accepts_typed_args(self, mock_search):
        mock_search.return_value = SearchResult(events=[], provider_notes=[], queries_run=0, stats=SearchStats())
        result = dispatch_concert(
            "watch",
            {"artist": "Disturbed", "area": "nationwide", "days": 365},
        )
        assert result.success
        criteria = mock_search.call_args[0][0]
        assert criteria.artist_query == "Disturbed"
        assert criteria.area == "nationwide"


class TestDiscordCommandRegistration:
    def test_area_choices_match_supported_presets(self):
        assert_supported_area_choices()

    def test_area_choices_cover_all_presets(self):
        values = {choice.value for choice in AREA_CHOICES}
        assert "houston" in values
        assert "east texas" in values
        assert "nationwide" in values
        assert len(values) == 8

    def test_search_and_watch_expose_filter_options(self):
        import concerts.discord_commands as module

        source = inspect.getsource(module.register_concert_commands)
        for command in ("concert_search", "concert_watch", "concert_pause", "concert_unwatch"):
            assert f"async def {command}" in source
        for option in FILTER_OPTION_NAMES:
            assert option in source
        assert "@app_commands.choices(area=AREA_CHOICES)" in source or "area_choices" in source

    def test_merge_preserves_unoverridden_freeform_fields(self):
        merged = merge_filters_from_args(
            {
                "query": 'genre:"rock" area:"dallas" days:180',
                "artist": "Shinedown",
            }
        )
        assert merged.artist == "Shinedown"
        assert merged.genre == "rock"
        assert merged.area == "dallas"
        assert merged.days == 180
