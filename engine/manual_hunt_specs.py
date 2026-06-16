"""
Predefined manual hunt specifications for idempotent DB seeding.

Each spec maps directly to engine.hunt_service.create_hunt() / edit_hunt() kwargs.
"""

from __future__ import annotations

from typing import Any

from engine.source_selection import resolve_source_sites

MACBOOK_A2338_SCREEN_NAME = "macbook_a2338_screen"

MACBOOK_A2338_SCREEN_SPEC: dict[str, Any] = {
    "name": MACBOOK_A2338_SCREEN_NAME,
    "category": "electronics",
    "search_terms": [
        "MacBook Pro A2338",
        "MacBook Pro M1 screen",
        "MacBook Pro M1 display",
        "MacBook Pro 13 M1",
        "MacBook Pro 2020 M1",
        "MacBook Pro A2338 display assembly",
        "MacBook Pro M1 for parts",
    ],
    "source_sites": resolve_source_sites("electronics"),
    "include_keywords": [
        "A2338",
        "MacBook Pro A2338",
        "MacBook Pro M1 screen",
        "MacBook Pro M1 display",
        "MacBook Pro 13 M1",
        "MacBook Pro 2020 M1",
        "MacBook Pro A2338 lid",
        "MacBook Pro A2338 LCD",
        "MacBook Pro A2338 display assembly",
        "MacBook Pro M1 for parts",
        "MacBook Pro A2338 broken",
        "MacBook Pro M1 broken",
    ],
    "exclude_keywords": [
        "case",
        "cover",
        "protector",
        "shell",
        "sticker",
        "keyboard cover",
        "sleeve",
        "charger",
        "adapter",
    ],
    "max_price": 250,
    "location": "houston",
    "radius": 50,
    "notes": (
        "Display: MacBook Pro A2338 M1 Screen (13-inch, 2020). "
        "Priority: medium. Area: Houston/Katy/Richmond/Rosenberg. "
        "Donor laptops allowed (for parts / won't power on). "
        "Pricing: great $100–150, good $150–225, maybe $225–275, skip $300+."
    ),
    "adapter_options": {
        "limit": 15,
        "display_name": "MacBook Pro A2338 M1 Screen",
        "priority": "medium",
        "area_notes": "Houston/Katy/Richmond/Rosenberg",
        "pricing_guidance": {
            "great": [100, 150],
            "good": [150, 225],
            "maybe": [225, 275],
            "skip_above": 300,
        },
        "allow_donor_laptops": True,
    },
}
