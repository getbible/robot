"""Safe Telegram HTML rendering and verse-boundary message chunking."""

from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from getbible import RequestLimitError

from .errors import ScriptureUnavailable

TELEGRAM_TEXT_LIMIT = 4096
DEFAULT_CHUNK_LIMIT = 3900
DEFAULT_MAX_CHUNKS = 8


def render_scripture(
    result: dict[str, Any],
    web_base_url: str,
    *,
    chunk_limit: int = DEFAULT_CHUNK_LIMIT,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[str]:
    """Render repository data without allowing HTML, URL, or output amplification."""
    if not isinstance(result, dict) or not result:
        raise ScriptureUnavailable("The Scripture repository returned no verses.")
    if not 256 <= chunk_limit <= TELEGRAM_TEXT_LIMIT:
        raise ValueError("chunk_limit must be between 256 and Telegram's text limit.")
    if not isinstance(max_chunks, int) or isinstance(max_chunks, bool) or not 1 <= max_chunks <= 32:
        raise ValueError("max_chunks must be an integer between 1 and 32.")

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
            budget = chunk_limit - max(
                _telegram_length(first_prefix),
                _telegram_length(continuation_prefix),
            )
            pieces = _split_for_html(text, budget)
            for index, piece in enumerate(pieces):
                prefix = first_prefix if index == 0 else continuation_prefix
                blocks.append(prefix + piece)

    return _pack_blocks(blocks, chunk_limit, max_chunks)


def pack_html_blocks(
    blocks: Iterable[str],
    *,
    chunk_limit: int = DEFAULT_CHUNK_LIMIT,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
    separator: str = "\n\n",
) -> list[str]:
    """Pack complete pre-escaped HTML blocks without truncating their content."""
    if not 256 <= chunk_limit <= TELEGRAM_TEXT_LIMIT:
        raise ValueError("chunk_limit must be between 256 and Telegram's text limit.")
    if not isinstance(max_chunks, int) or isinstance(max_chunks, bool) or not 1 <= max_chunks <= 32:
        raise ValueError("max_chunks must be an integer between 1 and 32.")
    if separator not in {"\n", "\n\n"}:
        raise ValueError("separator must be one or two newlines.")
    rendered = list(blocks)
    if not rendered or any(not isinstance(block, str) or not block for block in rendered):
        raise ScriptureUnavailable("Search rendering received an invalid result block.")
    return _pack_blocks(
        rendered,
        chunk_limit,
        max_chunks,
        separator=separator,
    )


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


def _telegram_length(value: str) -> int:
    """Return Telegram's UTF-16 code-unit length rather than Python code points."""
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as error:
        raise ScriptureUnavailable("Scripture text contains invalid Unicode data.") from error


def _split_for_html(text: str, maximum: int) -> list[str]:
    """Split raw text while accounting for HTML expansion and UTF-16 units."""
    if maximum < 32:
        raise ValueError("maximum is too small.")
    pieces: list[str] = []
    current: list[str] = []
    current_length = 0
    for character in text:
        escaped = html.escape(character)
        escaped_length = _telegram_length(escaped)
        if current and current_length + escaped_length > maximum:
            pieces.append("".join(current))
            current = []
            current_length = 0
        current.append(escaped)
        current_length += escaped_length
    if current:
        pieces.append("".join(current))
    return pieces


def _pack_blocks(
    blocks: list[str],
    maximum: int,
    max_chunks: int,
    *,
    separator: str = "\n",
) -> list[str]:
    chunks: list[str] = []
    current = ""

    def append_chunk(value: str) -> None:
        chunks.append(value)
        if len(chunks) > max_chunks:
            raise RequestLimitError(
                f"A response cannot exceed {max_chunks} Telegram messages."
            )

    for block in blocks:
        if _telegram_length(block) > maximum:
            raise ScriptureUnavailable("A rendered Scripture block exceeded Telegram's limit.")
        candidate = block if not current else f"{current}{separator}{block}"
        if _telegram_length(candidate) <= maximum:
            current = candidate
            continue
        append_chunk(current)
        current = block
    if current:
        append_chunk(current)
    if not chunks or any(_telegram_length(chunk) > maximum for chunk in chunks):
        raise ScriptureUnavailable("Scripture rendering failed safely.")
    return chunks
