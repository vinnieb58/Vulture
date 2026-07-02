"""Tests for concert query parser and validation."""

from __future__ import annotations

import pytest

from engine.concerts.query_parser import (
    FilterValidationError,
    filters_to_criteria,
    parse_and_validate,
    parse_filter_string,
)


class TestParseFilterString:
    def test_artist_city_days(self):
        f = parse_filter_string('artist:"Three Days Grace" city:"Houston" days:180')
        assert f.artist == "Three Days Grace"
        assert f.city == "Houston"
        assert f.days == 180

    def test_genre_area(self):
        f = parse_filter_string('genre:"rock" area:"houston" days:365')
        assert f.genre == "rock"
        assert f.area == "houston"
        assert f.days == 365

    def test_unquoted_values(self):
        f = parse_filter_string("days:90 radius:75")
        assert f.days == 90
        assert f.radius == 75


class TestValidation:
    def test_three_days_grace_houston(self):
        c = parse_and_validate('artist:"Three Days Grace" city:"Houston" days:180')
        assert c.artist_query == "Three Days Grace"
        assert c.city == "Houston"
        assert c.days_forward == 180

    def test_rock_houston_area(self):
        c = parse_and_validate('genre:"rock" area:"houston" days:365')
        assert c.genre == "rock"
        assert c.area == "houston"

    def test_artist_nationwide(self):
        c = parse_and_validate('artist:"Disturbed" area:"nationwide" days:365')
        assert c.artist_query == "Disturbed"
        assert c.area == "nationwide"

    def test_rock_louisiana(self):
        c = parse_and_validate('genre:"rock" area:"louisiana" days:365')
        assert c.area == "louisiana"

    def test_rock_east_texas(self):
        c = parse_and_validate('genre:"rock" area:"east texas" days:365')
        assert c.area == "east texas"

    def test_nationwide_genre_blocked(self):
        with pytest.raises(FilterValidationError, match="too noisy"):
            parse_and_validate('genre:"rock" area:"nationwide" days:365')

    def test_nationwide_genre_forced(self):
        c = parse_and_validate('genre:"rock" area:"nationwide" days:365 force:true')
        assert c.genre == "rock"

    def test_genre_without_geo_blocked(self):
        with pytest.raises(FilterValidationError, match="area"):
            filters_to_criteria(parse_filter_string('genre:"rock" days:180'))

    def test_missing_artist_and_genre(self):
        with pytest.raises(FilterValidationError):
            parse_and_validate("days:180")
