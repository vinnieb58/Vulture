"""
Meal option classification for Simply Fresh Kitchen probe dry-runs.
Pure logic — no Playwright dependency (unit-testable).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

MealClass = Literal["vegetarian", "non_vegetarian", "uncertain"]

VEGETARIAN_PATTERNS = [
    re.compile(r"vegetarian", re.I),
    re.compile(r"\bveggie\b", re.I),
    re.compile(r"veggie", re.I),
    re.compile(r"\bvegan\b", re.I),
    re.compile(r"plant[\s-]?based", re.I),
]

MEAT_PATTERNS = [
    re.compile(r"chicken", re.I),
    re.compile(r"beef", re.I),
    re.compile(r"turkey", re.I),
    re.compile(r"\bham\b", re.I),
    re.compile(r"pork", re.I),
    re.compile(r"pepperoni", re.I),
    re.compile(r"\bmeat\b", re.I),
    re.compile(r"burger", re.I),
    re.compile(r"\btaco\b", re.I),
    re.compile(r"pasta\s+with\s+meat", re.I),
    re.compile(r"nugget", re.I),
    re.compile(r"sausage", re.I),
    re.compile(r"bacon", re.I),
    re.compile(r"fish", re.I),
    re.compile(r"salmon", re.I),
]

# Cheese-only / generic items without veg or meat signals stay uncertain.
UNCERTAIN_FOOD_PATTERNS = [
    re.compile(r"cheese\s+pizza", re.I),
    re.compile(r"^pizza$", re.I),
    re.compile(r"mac\s+and\s+cheese", re.I),
]


@dataclass(frozen=True)
class MealChoiceResult:
    selected: Optional[str]
    reason: str
    classification: dict[str, MealClass]


def classify_meal_option(label: str) -> MealClass:
    text = " ".join(label.split()).strip()
    if not text:
        return "uncertain"

    # Simply Fresh vegetarian meals are often prefixed with "V-" (e.g. V-Grilled Tofu).
    if re.match(r"^V-", text):
        return "vegetarian"

    for pattern in VEGETARIAN_PATTERNS:
        if pattern.search(text):
            return "vegetarian"

    for pattern in UNCERTAIN_FOOD_PATTERNS:
        if pattern.search(text):
            return "uncertain"

    for pattern in MEAT_PATTERNS:
        if pattern.search(text):
            return "non_vegetarian"

    return "uncertain"


def choose_non_vegetarian_option(options: list[str]) -> MealChoiceResult:
    """Pick the best non-vegetarian option from visible meal labels."""
    if not options:
        return MealChoiceResult(None, "no_options", {})

    classifications = {opt: classify_meal_option(opt) for opt in options}
    non_veg = [o for o, c in classifications.items() if c == "non_vegetarian"]
    veg = [o for o, c in classifications.items() if c == "vegetarian"]

    if len(options) == 2 and len(veg) == 1 and len(non_veg) == 1:
        return MealChoiceResult(non_veg[0], "two_choice_exclude_vegetarian", classifications)

    if len(options) == 2 and len(veg) == 1:
        other = [o for o in options if o not in veg][0]
        other_class = classifications[other]
        if other_class == "non_vegetarian":
            return MealChoiceResult(other, "two_choice_other_is_meat", classifications)
        if other_class == "uncertain":
            return MealChoiceResult(None, "UNCERTAIN_MEAL_SKIPPED", classifications)

    if non_veg:
        # Prefer explicit meat labels over generic uncertain siblings.
        return MealChoiceResult(non_veg[0], "preferred_non_vegetarian", classifications)

    if len(options) == 2 and len(veg) == 2:
        return MealChoiceResult(None, "UNCERTAIN_MEAL_SKIPPED", classifications)

    return MealChoiceResult(None, "UNCERTAIN_MEAL_SKIPPED", classifications)
