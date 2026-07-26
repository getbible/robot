# Interactive Bible and search workflows

The robot has two intentionally different command paths:

- an explicit command remains fast and compatible with the legacy bot;
- an empty command opens a Telegram-native selection panel.

The Bible picker and search dashboard use inline-keyboard pagination. Both use
Telegram Bot API 10.2 ephemeral commands and messages in groups and supergroups,
so the requesting user and bot are the only participants that see translation,
book, chapter, verse, filter, result, selection, navigation, progress, and
notice interactions. No separately hosted Telegram Mini App or other web
application is required.

## Bible reference behavior

### Explicit reference

```text
/bible John 3:16
/bible John 3:16-19;1 John 3:10-17
/bible Genesis 1:1-5 aov
```

The reference is validated and posted immediately. The Telegram user's saved
translation is used when no trailing translation abbreviation is supplied.
`TRANSLATION`—`kjv` by default—is the fallback until that user chooses another
translation. `/get` and `/getbible` are aliases and follow the same behavior.

In a group or supergroup, the registered `/bible` command is ephemeral. The
command stays private to its sender while the resulting Scripture is posted
normally to the originating chat and forum topic. A manually typed legacy alias
that arrives as an ordinary message is deleted only after every Scripture chunk
has been delivered successfully.

### Empty command

```text
/bible
```

The robot never substitutes a hidden default verse. It opens this sequence:

1. translation, with the user's saved or configured default already selected and a one-click **Continue with …** action;
2. Old Testament, New Testament, or other books actually available in that translation;
3. book;
4. chapter;
5. first verse;
6. **Add verse N only**, or a later verse to create a range;
7. basket review, with **Add another from this chapter**, **Add another book/chapter**, and an individual remove action for every entry;
8. **Post selection**.

The basket accepts a single verse, one range, several non-contiguous verses,
several ranges, or combinations across chapters and books. Overlapping and
adjacent selections are merged into compact canonical references only when the
final request is built. The basket remains bounded by `MAX_REFERENCES`,
`MAX_VERSES_PER_REFERENCE`, and `MAX_TOTAL_VERSES`.

Translation and book lists come from the configured GetBible v2 repository.
Choosing a translation in either `/bible` or `/search` saves that choice as the
user's default for later commands and service restarts. The selected full-book
navigation document must match the SHA-1 published in that translation's books
index before chapter or verse buttons are constructed.

In a group or supergroup, this entire sequence remains inside one per-user
ephemeral panel. Page changes edit that panel rather than sending ordinary chat
messages. **Post Scripture** is the only action that publishes a normal group
message. If final retrieval fails, the private review controls are restored for
retrying; errors never fall back to a public message.

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
- saved user translation, falling back to the configured application default.

For Mandarin, Japanese, Korean, and other detected scripts that do not use
whitespace word boundaries, an otherwise-default whole-word search is
automatically changed to substring matching. This avoids treating every
adjacent ideograph as part of one indivisible word. An explicitly configured
search remains visible in the dashboard.

The robot displays every returned verse in full inside its own full-width
selectable button. The words reported by Librarian are visibly marked as
`【matched text】` because Telegram button labels do not support partial HTML
formatting. In a group or supergroup, the result panel is ephemeral and
addressed to the requesting user; other members cannot see it. Scripture is not
posted until the user selects one or more verse blocks and presses **Post
selected**.

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

Choosing proximity automatically selects the Librarian-compatible `all` word
mode. Changing translation clears a previous book restriction because book
availability is translation-specific and saves the new translation as that
user's default. **Reset defaults** restores Librarian defaults while retaining
the user's saved translation.

After selecting **Search words**, the user replies to the bot's prompt. The
prompt and response are ephemeral in a group or supergroup. The session remains
scoped to the originating chat and user, so another member's text cannot enter
the workflow.

## Result selection

The panel text contains only the query, translation, scope, result count, page,
and selection count. Every match is displayed once: one full-width button
contains its canonical reference and complete verse, with an unchecked or
checked marker. Matching words are enclosed in `【】` according to the active
case, diacritic, and whole-word/substring settings. No verse is shortened.
A page contains at most 30 result blocks. **Previous** and **Next** edit the
same per-user panel, and selections persist across pages.

The **All**, **Old**, **New**, and **Other** controls rerun the current query in
the selected Scripture scope without requiring another text reply. Choosing one
of these broad scopes clears a previous per-book restriction so the selected
section is not accidentally hidden by an incompatible book filter. A successful
scope change starts a new result generation and clears the old selection; a
failed rerun preserves the prior results and filters.

The result set remains bounded by `SEARCH_RESULT_LIMIT` and
`SEARCH_MAX_RESPONSE_BYTES`. The panel header remains within Telegram's message
limit, while the complete verse text is carried by the corresponding button
label. The exact Librarian total is displayed when it is larger than the
returned set.

The robot groups selected verses by book and chapter, compresses contiguous
verse numbers into ranges, revalidates the resulting reference against
`MAX_INPUT_LENGTH`, `MAX_REFERENCES`, and `MAX_TOTAL_VERSES`, and retrieves the
final Scripture through Librarian. Displayed search-result text is never treated
as the authoritative post payload.

**New search** retains the current filters. **Filters** returns to the dashboard. **Cancel** closes the session.

Rendered verses are compact: the linked reference header is followed by one
line per verse, and adjacent verses use a single newline rather than blank
paragraphs.

## Chat cleanup contract

In private chats, the workflow is visible only to that user. In groups and
supergroups, `/bible` and `/search` are registered as ephemeral commands and all
guided Bible/search messages are addressed only to the requesting user. A
manually typed command or legacy alias from a client that does not send it
ephemerally can still appear temporarily, but no picker, result, prompt,
progress message, or recoverable error is ever sent as an ordinary group
message. Only after every final Scripture message has been delivered
successfully does the robot remove the recorded workflow conversation:

- the user's initiating `/bible`, `/get`, `/getbible`, or `/search` command;
- the bot's picker, dashboard, or result panel;
- bot-created selective-reply prompts and acknowledgements;
- the owner's replies to those prompts.

The final Scripture message IDs are never added to the cleanup ledger, so the
Scripture remains in the chat. The ledger is bounded, scoped to the originating
chat/session, and contains only exact message IDs created or accepted by that
workflow; unrelated group messages cannot be deleted. An explicit **Cancel**
also removes the recorded workflow conversation without posting Scripture.

If Scripture lookup or delivery fails, the conversation is preserved so the
user can recover or retry. If Telegram refuses one or more deletions because of
group permissions, message age, or a transient API failure, the robot logs the
best-effort cleanup failure and still treats the already delivered Scripture as
successful.

Telegram ephemeral messages are available only in groups and supergroups, may
disappear after time or an app restart, and are not guaranteed to reach an
offline client. The robot therefore keeps its authoritative session state on
the server and never falls back to public result messages. Literal Telegram
channels do not support per-user ephemeral panels; the current command bot does
not subscribe to `channel_post` updates.

For reliable group delivery after a cold catalog lookup or long-running search,
keep the bot as a group administrator. Telegram permits any bot to respond
ephemerally within the short incoming-action window, while an administrator may
send the per-user response later. The existing **Delete messages** permission
remains required to remove manually visible commands and legacy alias messages.

## Session and callback safety

- Callback data contains only an opaque random session token, an action, and a bounded numeric or translation identifier.
- A session is usable only by the user and chat that created it.
- `INTERACTION_TTL_SECONDS` expires idle sessions.
- `INTERACTION_SESSION_LIMIT` provides LRU-bounded process memory under arbitrary user and chat churn.
- Restarting the process intentionally invalidates every open panel; the user can run the command again.
- Repository calls still pass through the shared queue, fixed worker pool,
  timeout, and circuit breaker.
- New commands and free-text prompt replies consume the inbound user/chat rate
  budget. Owner-scoped menu callbacks are serialized per session and do not
  consume command tokens, so normal translation/book/chapter/result navigation
  cannot rate-limit itself.
- Scrolling is native Telegram behavior, and checkmark changes do not consume a
  repository worker.
- Repeated rate-limit rejections produce at most one Telegram warning per cooldown period.
- Workflow cleanup records at most 256 exact message IDs per session and runs
  only after successful Scripture delivery or explicit cancellation.

The process stores short-lived query and selection state only in memory. The
per-instance preference database persists only a Telegram user ID, one
translation code, and an update timestamp; it does not store names, usernames,
query text, selected verses, or chat history.

## Rollout checklist

### Implemented

- Librarian input policy moved to `getbible>=1.2,<2` with an exact `1.2.0` hashed lock.
- Explicit `/bible <reference>` compatibility retained.
- Empty `/bible` guided picker added.
- Default and configurable Librarian search added.
- Paginated multi-select results with explicit posting added.
- Group-safe selective replies added.
- Session ownership, TTL/LRU bounds, callback bounds, catalog validation, checksums, and metrics added.
- Deterministic security, service, catalog, interaction, and command regressions added.
- Per-instance audit events record workflow source, translation, filter modes, result/selection counts, and posting outcome. Exact search terms and final references are present only when `AUDIT_LOG_MODE=content`.

### Required before production rollout

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
