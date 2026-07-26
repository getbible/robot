# Interactive Bible and search workflows

The robot has two intentionally different command paths:

- an explicit command remains fast and compatible with the legacy bot;
- an empty command opens a Telegram-native selection panel.

The panel uses inline keyboards, pagination, and selective replies. It does not require a separately hosted Telegram Mini App or expose another web application.

## Bible reference behavior

### Explicit reference

```text
/bible John 3:16
/bible John 3:16-19;1 John 3:10-17
/bible Genesis 1:1-5 aov
```

The reference is validated and posted immediately. The configured `TRANSLATION`—`kjv` by default—is used when no trailing translation abbreviation is supplied. `/get` and `/getbible` are aliases and follow the same behavior.

### Empty command

```text
/bible
```

The robot never substitutes a hidden default verse. It opens this short sequence:

1. translation, with the configured default already selected and a one-click **Skip — use KJV** action;
2. Old Testament, New Testament, or other books actually available in that translation;
3. book;
4. chapter;
5. first verse;
6. last verse, where choosing the same number selects one verse;
7. review and **Post Scripture**.

Translation and book lists come from the configured GetBible v2 repository. The selected full-book navigation document must match the SHA-1 published in that translation's books index before chapter or verse buttons are constructed.

## Scripture search behavior

### Search with defaults

```text
/search grace
/search faith hope
```

The command text is passed to Librarian 1.2 with its safe defaults:

- all words;
- whole-word matching;
- case insensitive;
- whole Bible;
- diacritic sensitive;
- canonical order;
- no book restriction, exclusions, or proximity;
- configured default translation.

The robot displays the returned verses as selectable rows. It does not post any Scripture until the user selects one or more rows and presses **Post selected**.

### Configurable search

```text
/search
```

The filter dashboard exposes the Librarian 1.2 search contract:

| Control | Values |
|---|---|
| Translation | Every translation in the configured repository catalog |
| Words | All, any, or phrase |
| Match | Whole word or substring |
| Scope | Bible, Old Testament, New Testament, or deuterocanon |
| Case | Sensitive or insensitive |
| Diacritics | Sensitive or insensitive |
| Sort | Canonical or relevance |
| Books | Optional multi-select from the chosen translation |
| Exclude | Optional words supplied through a selective reply |
| Proximity | None or 0–100 intervening words from the offered bounded values |

Choosing proximity automatically selects the Librarian-compatible `all` word mode. Changing translation clears a previous book restriction because book availability is translation-specific. **Reset defaults** returns the dashboard to the configured default translation and Librarian defaults.

After selecting **Search words**, the user replies to the bot's selective prompt. This reply mechanism works in private chats and groups without treating unrelated group messages as search input.

## Result selection

Search matches are displayed five at a time. Each row contains:

- an unchecked or checked marker;
- the canonical verse reference;
- a short plain-text preview.

Navigation buttons page through at most `SEARCH_RESULT_LIMIT` returned matches. The exact Librarian total is displayed when it is larger than the returned set. Selection is preserved while paging.

The robot groups selected verses by book and chapter, compresses contiguous verse numbers into ranges, revalidates the resulting reference against `MAX_INPUT_LENGTH`, `MAX_REFERENCES`, and `MAX_TOTAL_VERSES`, and retrieves the final Scripture through Librarian. Search preview data is never treated as the authoritative post payload.

**New search** retains the current filters. **Filters** returns to the dashboard. **Cancel** closes the session.

Rendered verses are compact: the linked reference header is followed by one
line per verse, and adjacent verses use a single newline rather than blank
paragraphs.

## Session and callback safety

- Callback data contains only an opaque random session token, an action, and a bounded numeric or translation identifier.
- A session is usable only by the user and chat that created it.
- `INTERACTION_TTL_SECONDS` expires idle sessions.
- `INTERACTION_SESSION_LIMIT` provides LRU-bounded process memory under arbitrary user and chat churn.
- Restarting the process intentionally invalidates every open panel; the user can run the command again.
- Repository calls still pass through the shared queue, fixed worker pool, timeout, circuit breaker, and user/chat rate limits.
- Every accepted command, callback, and session-owned prompt reply consumes the inbound user/chat rate budget, including local pagination and selection changes.
- Pure local paging and checkmark changes do not consume a repository worker.
- Repeated rate-limit rejections produce at most one Telegram warning per cooldown period.

The process stores only short-lived selection state. It does not persist query text, selected verses, user profiles, or chat history.

## Rollout roadmap

### Implemented on the feature branch

- Librarian input policy moved to `getbible>=1.2,<2` with an exact `1.2.0` hashed lock.
- Explicit `/bible <reference>` compatibility retained.
- Empty `/bible` guided picker added.
- Default and configurable Librarian search added.
- Paginated multi-select results with explicit posting added.
- Group-safe selective replies added.
- Session ownership, TTL/LRU bounds, callback bounds, catalog validation, checksums, and metrics added.
- Deterministic security, service, catalog, interaction, and command regressions added.
- Per-instance audit events record workflow source, translation, filter modes, result/selection counts, and posting outcome. Exact search terms and final references are present only when `AUDIT_LOG_MODE=content`.

### Required before production merge

1. Complete the repository CI and CodeQL gates on Python 3.10, 3.11, and 3.12.
2. Use a dedicated test bot to run the private-chat and group smoke matrix in [Testing](TESTING.md).
3. Verify real BotFather privacy-mode behavior for selective replies in at least one group.
4. Exercise a right-to-left translation and a translation with non-66-book coverage.
5. Confirm the largest supported full translation remains below
   `GETBIBLE_MAX_RESPONSE_BYTES`, while search output remains independently
   bounded by `SEARCH_MAX_RESPONSE_BYTES`.
6. Rehearse rollback to the previous reviewed robot commit.

### Follow-up after stable rollout

- Collect aggregate-only counts for opened, expired, cancelled, searched, and posted interactions.
- Tune page sizes or wording from observed Telegram usability without changing callback trust boundaries.
- Add translations for bot-owned interface text if multilingual UI becomes a product requirement.
- Consider a separately reviewed Telegram Mini App only if native keyboard pagination proves insufficient; it is not required for the current workflow.
