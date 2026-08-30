// Protestant canon order used by the bundled catalogue, repository importer,
// Query API review coordinates, and contributor publication pipeline.
export const BOOK_CHAPTER_COUNTS = Object.freeze([
  50, 40, 27, 36, 34, 24, 21, 4, 31, 24, 22, 25, 29, 36, 10, 13, 10,
  42, 150, 31, 12, 8, 66, 52, 5, 48, 12, 14, 3, 9, 1, 4, 7, 3, 3, 3, 2,
  14, 4, 28, 16, 24, 21, 28, 16, 16, 13, 6, 6, 4, 4, 5, 3, 6, 4, 3, 1,
  13, 5, 5, 3, 5, 1, 1, 1, 22,
]);

export function isCanonicalVerseCoordinate(value) {
  return Boolean(
    value &&
    Number.isInteger(value.book) &&
    value.book >= 1 &&
    value.book <= BOOK_CHAPTER_COUNTS.length &&
    Number.isInteger(value.chapter) &&
    value.chapter >= 1 &&
    value.chapter <= BOOK_CHAPTER_COUNTS[value.book - 1] &&
    Number.isInteger(value.verse) &&
    value.verse >= 1 &&
    value.verse <= 2_000,
  );
}
