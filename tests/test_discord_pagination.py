"""
Tests for Discord message pagination (hunt_list and generic command output).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataclasses import dataclass

import engine.database as db_module
from discord_messages import (
    SAFE_MESSAGE_LIMIT,
    paginate_blocks,
    paginate_command_message,
    paginate_hunt_list_result,
    paginate_text,
    send_paginated_followup,
    send_paginated_response,
)
from engine.hunt_repository import create_hunt, init_hunts_table
from models.hunt import Hunt


@dataclass
class _ListResult:
    success: bool
    message: str
    data: dict | None = None


def _fmt_summary(hunt: Hunt) -> str:
    price = f" | max ${hunt.max_price}" if hunt.max_price is not None else ""
    loc = f" | {hunt.location}" if hunt.location else ""
    terms = " ".join(hunt.search_terms)
    sites = ", ".join(hunt.source_sites) if hunt.source_sites else "—"
    return (
        f"**{hunt.name}** [{hunt.status}] — {sites} | \"{terms}\""
        f"{price}{loc}\n`{hunt.hunt_id}`"
    )


def _dispatch_list() -> _ListResult:
    """Build a list result matching command_router.cmd_list formatting."""
    import engine.hunt_repository as repo

    hunts = repo.list_hunts()
    if not hunts:
        return _ListResult(success=True, message="No hunts found.", data={"hunts": []})

    lines = [_fmt_summary(h) for h in hunts]
    return _ListResult(
        success=True,
        message=f"**Hunts — {len(hunts)} found**\n\n" + "\n\n".join(lines),
        data={"hunts": [{"name": h.name, "hunt_id": h.hunt_id} for h in hunts]},
    )


@pytest.fixture()
def temp_hunt_db(monkeypatch):
    tmp_dir = tempfile.TemporaryDirectory()
    db_path = Path(tmp_dir.name) / "test_vulture.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    init_hunts_table()
    yield db_path
    tmp_dir.cleanup()


def _make_hunt(name: str, *, status: str = "active", search_terms: list[str] | None = None) -> Hunt:
    return Hunt(
        name=name,
        status=status,
        source_sites=["craigslist"],
        search_terms=search_terms or [name],
    )


def _all_hunt_names_in_pages(pages: list[str]) -> set[str]:
    names: set[str] = set()
    for page in pages:
        for line in page.splitlines():
            if line.startswith("**") and "** [" in line:
                names.add(line.split("**")[1])
    return names


class TestPaginateHuntList:
    def test_no_hunts_single_page(self, temp_hunt_db):
        result = _dispatch_list()
        pages = paginate_hunt_list_result(result)
        assert len(pages) == 1
        assert "No hunts found" in pages[0]
        assert "Page" not in pages[0]

    def test_under_limit_single_page(self, temp_hunt_db):
        for i in range(3):
            create_hunt(_make_hunt(f"hunt_{i}"))
        result = _dispatch_list()
        pages = paginate_hunt_list_result(result)
        assert len(pages) == 1
        assert len(pages[0]) <= SAFE_MESSAGE_LIMIT
        assert _all_hunt_names_in_pages(pages) == {"hunt_0", "hunt_1", "hunt_2"}
        assert "Page" not in pages[0]

    def test_over_limit_includes_all_hunts_across_pages(self, temp_hunt_db, monkeypatch):
        monkeypatch.setattr("discord_messages.SAFE_MESSAGE_LIMIT", 500)
        for i in range(12):
            create_hunt(_make_hunt(f"listed_hunt_{i:02d}"))
        result = _dispatch_list()
        pages = paginate_hunt_list_result(result, limit=500)
        assert len(pages) > 1
        combined = "\n".join(pages)
        for i in range(12):
            assert f"listed_hunt_{i:02d}" in combined
        assert _all_hunt_names_in_pages(pages) == {f"listed_hunt_{i:02d}" for i in range(12)}
        assert "(Page 1/" in pages[0]
        assert "Continued" in pages[0] or "Continued" in pages[1]

    def test_very_long_hunt_name_still_present(self, temp_hunt_db, monkeypatch):
        monkeypatch.setattr("discord_messages.SAFE_MESSAGE_LIMIT", 300)
        long_name = "x" * 250
        long_terms = " ".join(["queryword"] * 40)
        create_hunt(
            Hunt(
                name=long_name,
                status="active",
                source_sites=["craigslist"],
                search_terms=[long_terms],
            )
        )
        create_hunt(_make_hunt("short_hunt"))
        result = _dispatch_list()
        pages = paginate_hunt_list_result(result, limit=300)
        combined = "\n".join(pages)
        assert long_name in combined
        assert "short_hunt" in combined
        for page in pages:
            assert len(page) <= 300

    def test_exactly_at_limit_no_page_footer(self, monkeypatch):
        monkeypatch.setattr("discord_messages.SAFE_MESSAGE_LIMIT", 120)
        header = "**Hunts — 1 found**"
        block = "a" * (120 - len(header) - 2)
        pages = paginate_blocks(header, [block], limit=120)
        assert len(pages) == 1
        assert len(pages[0]) <= 120
        assert "Page" not in pages[0]


class TestPaginateText:
    def test_empty_text(self):
        assert paginate_text("") == [""]

    def test_short_text_unchanged(self):
        text = "hello"
        assert paginate_text(text) == [text]


class TestPaginateCommandMessage:
    def test_list_command_uses_hunt_pagination(self, temp_hunt_db):
        create_hunt(_make_hunt("alpha"))
        result = _dispatch_list()
        pages = paginate_command_message(result, command="list")
        assert "alpha" in pages[0]


class TestSendPaginatedResponse:
    @pytest.mark.asyncio
    async def test_deferred_uses_followup_only(self):
        interaction = MagicMock()
        interaction.response.is_done.return_value = True
        interaction.followup.send = AsyncMock()
        pages = ["page one", "page two"]
        await send_paginated_followup(interaction, pages, ephemeral=True)
        assert interaction.followup.send.await_count == 2
        interaction.followup.send.assert_any_await("page one", ephemeral=True)
        interaction.followup.send.assert_any_await("page two", ephemeral=True)

    @pytest.mark.asyncio
    async def test_not_deferred_first_page_via_response(self):
        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        pages = ["first", "second"]
        await send_paginated_response(interaction, pages, ephemeral=True, deferred=False)
        interaction.response.send_message.assert_awaited_once_with("first", ephemeral=True)
        interaction.followup.send.assert_awaited_once_with("second", ephemeral=True)

    @pytest.mark.asyncio
    async def test_deferred_flag_skips_response(self):
        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        await send_paginated_response(
            interaction,
            ["only followup"],
            ephemeral=True,
            deferred=True,
        )
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with("only followup", ephemeral=True)
