/**
 * Returns a presentation-only copy of a topic list in localized alphabetical
 * order. The stable identifier tie-breaker keeps rendering deterministic when
 * two translated labels collate equally; callers never mutate persisted topic
 * order, which is significant to Telegram's compact topic indexes.
 */
export function sortBookmarkTopics(
  topics,
  nameForTopic = (topic) => topic?.name ?? "",
  locale = "en",
) {
  if (!Array.isArray(topics) || typeof nameForTopic !== "function") {
    throw new TypeError("Bookmark topics are invalid.");
  }
  const collator = new Intl.Collator(locale, {
    numeric: true,
    sensitivity: "base",
    usage: "sort",
  });
  return [...topics].sort((left, right) => {
    const byName = collator.compare(
      String(nameForTopic(left)),
      String(nameForTopic(right)),
    );
    return byName || String(left?.id ?? "").localeCompare(
      String(right?.id ?? ""),
      "en",
    );
  });
}
