"""Unit tests for Simply Fresh Kitchen meal classification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "simplyfresh_probe"))

from meal_classification import choose_non_vegetarian_option, classify_meal_option


def test_vegetarian_pasta():
    assert classify_meal_option("Vegetarian Pasta") == "vegetarian"


def test_cheese_pizza_uncertain():
    assert classify_meal_option("Cheese Pizza") == "uncertain"


def test_chicken_nuggets():
    assert classify_meal_option("Chicken Nuggets") == "non_vegetarian"


def test_beef_taco():
    assert classify_meal_option("Beef Taco") == "non_vegetarian"


def test_veggie_burger():
    assert classify_meal_option("Veggie Burger") == "vegetarian"


def test_two_choice_vegetarian_and_chicken():
    result = choose_non_vegetarian_option(["Vegetarian Pasta", "Chicken Nuggets"])
    assert result.selected == "Chicken Nuggets"
    assert result.reason == "two_choice_exclude_vegetarian"


def test_two_uncertain_skips():
    result = choose_non_vegetarian_option(["Cheese Pizza", "Mac and Cheese"])
    assert result.selected is None
    assert result.reason == "UNCERTAIN_MEAL_SKIPPED"
