"""
Reusable Discord message pagination/chunking for Vulture and Crow bots.

Discord caps plain-text messages at 2000 characters. This module splits long
command output into multiple pages without breaking hunt/list blocks when
possible, and adds clear page indicators.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol, Sequence

# Discord hard limit; leave headroom for page footers and formatting.
DISCORD_MESSAGE_MAX = 2000
SAFE_MESSAGE_LIMIT = 1900
_PAGE_FOOTER_RESERVE = 40  # room for "\n\n_(Page 99/99)_"


class _PaginatedResult(Protocol):
    success: bool
    message: str
    data: dict | None


def page_footer(page: int, total: int, *, continued: bool = False) -> str:
    """Footer appended to paginated messages."""
    if total <= 1:
        return ""
    prefix = "Continued… " if continued and page < total else ""
    return f"\n\n_{prefix}(Page {page}/{total})_"


def _hard_split(text: str, limit: int) -> list[str]:
    """Split text at character boundaries when it cannot fit as one block."""
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _split_oversized_block(block: str, limit: int) -> list[str]:
    """Split a single oversized block on newlines first, then hard-split."""
    if len(block) <= limit:
        return [block]

    pieces: list[str] = []
    for line in block.split("\n"):
        if len(line) <= limit:
            pieces.append(line)
        else:
            pieces.extend(_hard_split(line, limit))

    merged: list[str] = []
    current = ""
    for piece in pieces:
        candidate = piece if not current else f"{current}\n{piece}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                merged.append(current)
            current = piece
    if current:
        merged.append(current)
    return merged or [block[:limit]]


def paginate_blocks(
    header: str,
    blocks: Sequence[str],
    *,
    separator: str = "\n\n",
    limit: int = SAFE_MESSAGE_LIMIT,
) -> list[str]:
    """
    Pack blocks into Discord-safe pages.

    The header appears only on page 1. Blocks are never split across pages
    unless a single block exceeds the limit.
    """
    normalized_blocks: list[str] = []
    for block in blocks:
        if not block:
            continue
        if len(block) > limit:
            normalized_blocks.extend(_split_oversized_block(block, limit))
        else:
            normalized_blocks.append(block)

    if not normalized_blocks:
        return paginate_text(header, limit=limit) if header else [""]

    def assemble(page_blocks: list[str], *, include_header: bool) -> str:
        body = separator.join(page_blocks)
        if include_header and header:
            return f"{header}{separator}{body}" if body else header
        return body

    # Greedy pack; reserve footer space when we expect multiple pages.
    draft_pages: list[tuple[list[str], bool]] = []
    current: list[str] = []

    for block in normalized_blocks:
        trial = current + [block]
        include_header = not draft_pages and not current
        reserve = _PAGE_FOOTER_RESERVE if (draft_pages or len(trial) > 1) else 0
        if len(assemble(trial, include_header=include_header)) + reserve <= limit:
            current = trial
        else:
            if current:
                draft_pages.append((current, not draft_pages))
                current = [block]
            else:
                draft_pages.append(([block], not draft_pages))
                current = []

    if current:
        draft_pages.append((current, not draft_pages))

    total = len(draft_pages)
    pages: list[str] = []
    for idx, (page_blocks, include_header) in enumerate(draft_pages, start=1):
        text = assemble(page_blocks, include_header=include_header)
        footer = page_footer(idx, total, continued=idx < total)
        if len(text) + len(footer) > limit:
            # Drop header on overflow pages or hard-split as last resort.
            text = assemble(page_blocks, include_header=False)
            if len(text) + len(footer) > limit:
                text = text[: max(0, limit - len(footer))]
        pages.append(text + footer)
    return pages


def paginate_text(text: str, *, limit: int = SAFE_MESSAGE_LIMIT) -> list[str]:
    """Split arbitrary text on paragraph boundaries, with page footers."""
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    sections = text.split("\n\n")
    if len(sections) == 1:
        return _add_page_footers(_hard_split(text, limit - _PAGE_FOOTER_RESERVE))

    return paginate_blocks("", sections, separator="\n\n", limit=limit)


def paginate_hunt_list_result(
    result: _PaginatedResult,
    *,
    limit: int = SAFE_MESSAGE_LIMIT,
) -> list[str]:
    """
    Paginate hunt list output from command_router.cmd_list.

    Splits on hunt summary blocks (double-newline separated) so each hunt
    stays intact unless a single summary exceeds the limit.
    """
    message = result.message or ""
    hunts = (result.data or {}).get("hunts") if result.data else None
    if not result.success or not hunts:
        return paginate_text(message, limit=limit)

    if "\n\n" not in message:
        return paginate_text(message, limit=limit)

    header, body = message.split("\n\n", 1)
    blocks = body.split("\n\n") if body else []
    return paginate_blocks(header, blocks, limit=limit)


def _add_page_footers(chunks: list[str]) -> list[str]:
    total = len(chunks)
    if total <= 1:
        return chunks
    return [
        chunk + page_footer(i, total, continued=i < total)
        for i, chunk in enumerate(chunks, start=1)
    ]


class _FollowupSender(Protocol):
    def send(self, content: str, *, ephemeral: bool) -> Awaitable[object]: ...


class _InteractionLike(Protocol):
    response: object
    followup: _FollowupSender

    async def response_send(self, content: str, *, ephemeral: bool) -> object: ...


async def send_paginated_messages(
    send_first: Callable[..., Awaitable[object]],
    send_next: Callable[..., Awaitable[object]],
    pages: Sequence[str],
    *,
    ephemeral: bool = True,
) -> None:
    """Send pre-built pages via the appropriate Discord send callables."""
    if not pages:
        pages = [""]
    await send_first(pages[0], ephemeral=ephemeral)
    for page in pages[1:]:
        await send_next(page, ephemeral=ephemeral)


async def send_paginated_followup(
    interaction: _InteractionLike,
    pages: Sequence[str],
    *,
    ephemeral: bool = True,
) -> None:
    """
    Send pages after interaction.response.defer() (or any completed response).

    Assumes the interaction has already been acknowledged.
    """
    await send_paginated_messages(
        interaction.followup.send,
        interaction.followup.send,
        pages,
        ephemeral=ephemeral,
    )


async def send_paginated_response(
    interaction: _InteractionLike,
    pages: Sequence[str],
    *,
    ephemeral: bool = True,
    deferred: bool = False,
) -> None:
    """
    Send paginated output, handling deferred vs not-yet-responded interactions.

    When deferred=True (or interaction.response.is_done()), all pages go via
    followup. Otherwise the first page uses response.send_message and the rest
    use followup.
    """
    if not pages:
        pages = [""]

    response_done = deferred
    is_done = getattr(interaction.response, "is_done", None)
    if callable(is_done):
        response_done = response_done or is_done()

    if response_done:
        await send_paginated_followup(interaction, pages, ephemeral=ephemeral)
        return

    await send_paginated_messages(
        interaction.response.send_message,
        interaction.followup.send,
        pages,
        ephemeral=ephemeral,
    )


def paginate_command_message(
    result: _PaginatedResult,
    *,
    command: str | None = None,
    limit: int = SAFE_MESSAGE_LIMIT,
) -> list[str]:
    """Route to hunt-list pagination or generic text pagination."""
    if command == "list":
        return paginate_hunt_list_result(result, limit=limit)
    return paginate_text(result.message or "", limit=limit)
