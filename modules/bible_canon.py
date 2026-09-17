"""The 66-book Protestant canon shared by every coordinate validator.

Bookmark coordinates are translation independent and carry no Scripture text;
the only structural facts a validator needs are the book count and each
book's chapter count. They are kept here, in one place, for the contribution
store, the Bookmarks API client and the review tooling alike.
"""

from __future__ import annotations

BOOK_CHAPTER_COUNTS: tuple[int, ...] = (
    50, 40, 27, 36, 34, 24, 21, 4, 31, 24, 22, 25, 29, 36, 10, 13, 10,
    42, 150, 31, 12, 8, 66, 52, 5, 48, 12, 14, 3, 9, 1, 4, 7, 3, 3, 3, 2,
    14, 4, 28, 16, 24, 21, 28, 16, 16, 13, 6, 6, 4, 4, 5, 3, 6, 4, 3, 1,
    13, 5, 5, 3, 5, 1, 1, 1, 22,
)
BOOK_COUNT = len(BOOK_CHAPTER_COUNTS)
MAX_VERSE_NUMBER = 2_000


def is_canonical_coordinate(book: object, chapter: object, verse: object) -> bool:
    """Return True when the triple is a coordinate inside the canon."""
    if not isinstance(book, int) or not isinstance(chapter, int) or not isinstance(verse, int):
        return False
    if isinstance(book, bool) or isinstance(chapter, bool) or isinstance(verse, bool):
        return False
    book_number, chapter_number, verse_number = book, chapter, verse
    return (
        1 <= book_number <= BOOK_COUNT
        and 1 <= chapter_number <= BOOK_CHAPTER_COUNTS[book_number - 1]
        and 1 <= verse_number <= MAX_VERSE_NUMBER
    )


__all__ = [
    "BOOK_CHAPTER_COUNTS",
    "BOOK_COUNT",
    "MAX_VERSE_NUMBER",
    "is_canonical_coordinate",
]
