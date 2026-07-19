"""Safe Telegram HTML rendering and verse-boundary message chunking."""

from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from .errors import ScriptureUnavailable

TELEGRAM_TEXT_LIMIT = 4096
DEFAULT_CHUNK_LIMIT = 3900


def render_scripture(
    result: dict[str, Any],
    web_base_url: str,
    *,
    chunk_limit: int = DEFAULT_CHUNK_LIMIT,
) -> list[str]:
    """Render trusted repository data without allowing HTML or URL injection."""
    if not isinstance(result, dict) or not result:
        raise ScriptureUnavailable("The Scripture repository returned no verses.")
    if not 256 <= chunk_limit <= TELEGRAM_TEXT_LIMIT:
        raise ValueError("chunk_limit must be between 256 and Telegram's text limit.")

    blocks: list[str] = []
    for chapter in result.values():
        if not isinstance(chapter, dict):
            raise ScriptureUnavailable("The Scripture repository returned malformed data.")
        book_name = str(chapter.get("book_name", "")).strip()
        abbreviation = str(chapter.get("abbreviation", "")).strip().casefold()
        chapter_number = chapter.get("chapter")
        verses = chapter.get("verses")
        if (
            not book_name
            or not abbreviation
            or chapter_number in {None, ""}
            or not isinstance(verses, list)
            or not verses
        ):
            raise ScriptureUnavailable("The Scripture repository returned malformed data.")

        verse_reference = _verse_ranges(verses)
        url = (
            f"{web_base_url.rstrip('/')}/"
            f"{quote(abbreviation, safe='')}/"
            f"{quote(book_name, safe='')}/"
            f"{quote(str(chapter_number), safe='')}/"
            f"{quote(verse_reference, safe='')}"
        )
        label = f"{book_name} {chapter_number}:{verse_reference}"
        header = (
            f'<b><a href="{html.escape(url, quote=True)}">'
            f"{html.escape(label)}</a></b> "
            f"<code>{html.escape(abbreviation)}</code>"
        )
        blocks.append(header)

        for verse in verses:
            if not isinstance(verse, dict):
                raise ScriptureUnavailable(
                    "The Scripture repository returned malformed verse data."
                )
            number = verse.get("verse")
            text = str(verse.get("text", "")).strip()
            if number in {None, ""} or not text:
                raise ScriptureUnavailable(
                    "The Scripture repository returned malformed verse data."
                )
            first_prefix = f"<b>{html.escape(str(number))}.</b> "
            continuation_prefix = "↳ "
            budget = chunk_limit - max(len(first_prefix), len(continuation_prefix))
            pieces = _split_for_html(text, budget)
            for index, piece in enumerate(pieces):
                prefix = first_prefix if index == 0 else continuation_prefix
                blocks.append(prefix + piece)

    return _pack_blocks(blocks, chunk_limit)


def _verse_ranges(verses: Iterable[dict[str, Any]]) -> str:
    try:
        numbers = sorted({int(verse["verse"]) for verse in verses})
    except (KeyError, TypeError, ValueError) as error:
        raise ScriptureUnavailable(
            "The Scripture repository returned invalid verse numbers."
        ) from error
    if not numbers or numbers[0] < 1:
        raise ScriptureUnavailable("The Scripture repository returned invalid verse numbers.")

    ranges: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _split_for_html(text: str, maximum: int) -> list[str]:
    """Split raw text while accounting for entity expansion after escaping."""
    if maximum < 32:
        raise ValueError("maximum is too small.")
    pieces: list[str] = []
    current: list[str] = []
    current_length = 0
    for character in text:
        escaped = html.escape(character)
        if current and current_length + len(escaped) > maximum:
            pieces.append("".join(current))
            current = []
            current_length = 0
        current.append(escaped)
        current_length += len(escaped)
    if current:
        pieces.append("".join(current))
    return pieces


def _pack_blocks(blocks: list[str], maximum: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > maximum:
            raise ScriptureUnavailable("A rendered Scripture block exceeded Telegram's limit.")
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= maximum:
            current = candidate
            continue
        chunks.append(current)
        current = block
    if current:
        chunks.append(current)
    if not chunks or any(len(chunk) > maximum for chunk in chunks):
        raise ScriptureUnavailable("Scripture rendering failed safely.")
    return chunks
