# Scripture interaction workflows

GetBible deliberately keeps the shortest command path native and moves every
browsing workflow into the contained Telegram Mini App.

| Command | Result |
|---|---|
| `/bible John 3:16` | Validate, retrieve, and post the verse immediately |
| `/bible John 3:16-18` | Validate, retrieve, and post the range immediately |
| `/bible` | Resume the Mini App Bible reader at the last viewed verse |
| `/search grace` | Open the Mini App on contained results for `grace` |
| `/search` | Open the Mini App search form and filters |
| `/help` | Show native command help |
| Bot menu button | Open the Mini App home screen |

`/get` and `/getbible` remain aliases for `/bible`. When `MINI_APP_ENABLED` is
false, the existing Telegram-native picker remains available as a compatibility
fallback.

## Bible reader

The existing **Bible** tab is both a compact chapter reader and the verse
selection surface. It does not introduce a second reader route or a fifth
bottom-navigation item. The normal sequence is:

1. choose the global translation from the header chip when needed;
2. choose a book from the passage picker;
3. choose a numbered chapter from the passage picker;
4. read the chapter as continuous, edge-friendly Scripture text;
5. tap a verse number or row to select or remove it;
6. add verses from other chapters, books, searches, or translations;
7. review, reorder, or clear the server-held basket;
8. press **Post selected verses** once.

The compact chapter heading contains previous/next controls and the current
passage. It hides while scrolling down and returns while scrolling up, leaving
the maximum practical space for Scripture. The fixed bottom navigation behaves
the same way, but leaves a small tap handle available while collapsed. Its
background and device safe area extend to the physical bottom edge.

Pressing the current passage opens a narrow side sheet over the Scripture
instead of replacing the reader. When a chapter is already open, the sheet
starts at that book's numbered chapter grid; **Back** reveals the book grid.
The user can dismiss it with **Close**, Telegram Back, Escape, or the shaded
backdrop. Choosing a chapter closes the sheet and loads it immediately.

Book buttons use the selected translation's authoritative names from the
GetBible API. The visible labels are collision-free compact forms derived only
for presentation; each button retains the complete API name for its accessible
name and desktop title. Old Testament, New Testament, and additional books are
separated without assuming that every translation contains exactly 66 books.
Chapter grids contain numbers only and scroll independently through long books
such as Psalms.

Chapter buttons remain compact numbers. Reader verses do not become separate
raised cards; adjacent selected verses visually join while retaining a visible
pressed state and accessible `aria-pressed` state. The basket can contain
isolated verses or contiguous verses; the server normalizes adjacent selections
into safe references at post time.

The client receives an opaque selection identifier for each displayed verse.
It never supplies authoritative verse text for posting. On **Post**, the server
rebuilds and retrieves the Scripture references, applies the existing renderer
and limits, and sends the final message through the bot.

## Search and filters

`/search <words>` places the initial query only in short-lived server launch
state; the query is not exposed in the launch URL. `/search` opens an empty
search form. Results are complete verse cards and use the same tap-to-select
behavior as Bible browsing.

The interface exposes the supported Librarian search controls:

| Control | Values |
|---|---|
| Words | All, any, or phrase |
| Match | Whole word or substring |
| Scope | Bible, Old Testament, New Testament, or deuterocanon |
| Case | Sensitive or insensitive |
| Diacritics | Sensitive or insensitive |
| Sort | Canonical or relevance |
| Books | Optional translation-specific restriction |
| Exclusions | Optional excluded words |
| Proximity | Optional bounded word distance |

Results use contained paging or incremental loading and never create a stream
of Telegram messages. Selections persist across result pages and Bible
navigation. Submitting through either the visible button or the mobile
keyboard's Search key blurs the query input before results render so Telegram
can dismiss the on-screen keyboard. Only the final server-resolved post enters
the target chat.

## Translation and interface preferences

The translation chip in the application header is the only translation
selector. It opens a dedicated selector containing the bounded translation
name, language, and abbreviation. It never changes the current tab. The Search
filter sheet contains only search filters, and the Bible passage picker contains
only book and chapter controls.

Selecting a translation updates the interface language and direction
immediately, clears any old-translation Scripture before it can be relabelled,
and reloads the current book, chapter, and visible verse when the Bible reader
is open. Translation and the closest valid reader location are resolved and
committed together, so an immediate close cannot leave a mixed or erased resume
state. An in-flight response for an older translation is ignored. Search and
Scripture reads are side-effect-free; only the serialized preferences endpoint
may change the saved translation or reader location.

The selected Bible translation and the last reader location are stored per
Telegram user. A reader location is only four bounded identifiers:
translation, book, chapter, and verse. Safe search defaults may also be stored,
but query text, excluded words, chapter contents, launch state, session
credentials, and verse text are not durable preferences.

Opening bare `/bible` resumes that saved location. Opening a chapter from a
search result focuses the matching verse and offers a return to the preserved
search results. Search results can still be selected directly without opening
the reader.

Each translation exposes its authoritative `lang` metadata. The Mini App
resolves the interface catalog in this order:

1. exact normalized locale, such as `pt-br`;
2. base language, such as `pt`;
3. English.

The document language and accessible labels change with the resolved catalog.
A missing or incomplete catalog always falls back key-by-key to English, so a
translation can never leave navigation blank. Adding interface languages is a
content-only extension to `miniapp/lib/i18n.js`; it does not change the
authorization boundary. The selected Scripture text always comes from the
chosen translation regardless of interface-catalog availability.

## Private and group launches

In a private chat, an inline Web App button carries a one-time opaque launch
token. In a group or forum topic, the robot sends the requesting user an
ephemeral Main Mini App link. The server binds that token to the Telegram user,
target chat, topic, and initial route. The URL contains no chat identifier,
reference, search query, or verse text.

Browsing, filtering, and basket changes remain private. The final post is sent
by the bot to the launch-bound chat and topic. A generic menu-button launch has
no group target and posts to the user's private conversation with the bot.

## Failure and cleanup behavior

- When Telegram recreates a WebView, fresh signed `initData` can recover the
  still-active session only for the same launch token, user, chat, and chat
  instance. Its absolute expiry is unchanged.
- A malformed, genuinely expired, replayed, or user-mismatched launch fails
  closed with an explicit close-and-relaunch instruction.
- Tapping a recorded expired launch performs one best-effort cleanup of its
  Telegram rows before rejection. Reopening the bot command creates a fresh
  launch.
- Direct `/bible <reference>` commands are deleted only after successful
  delivery when command deletion is enabled.
- The initiating ephemeral command and the bot's ephemeral Mini App response
  are both best-effort cleanup items. Immediate source-command cleanup is
  retried independently after a successful final post; final Scripture
  messages are never part of that cleanup ledger.
- The complete basket is resolved and rendered under one global output-message
  limit before the first Telegram send.
- Known messages from an incomplete send are deleted best-effort. Because a
  timeout can leave an unknown Telegram outcome, the exact basket attempt is
  then locked; relaunch or change the basket instead of blindly retrying it.
- Idempotency is bound to the exact ordered basket. A new key cannot bypass a
  pending, completed, or failed attempt for the same basket.
- Process restart intentionally expires in-memory launches, Mini App sessions,
  search results, and baskets.

## Security and bounds

Every protected API request is tied to signature-verified Telegram `initData`,
the exact authenticated user, an absolute-lifetime server session, same-origin
policy, rate limits, and server-side content/selection bounds. The bot token
never reaches the browser.

Complete reader chapters come from the GetBible Main API through one
hash-verified, bounded server cache. Librarian remains responsible for
reference parsing, search, direct command retrieval, and authoritative final
posting.

See [Telegram Mini App deployment](MINI_APP.md) for the HTTPS boundary,
BotFather configuration, reverse-proxy examples, and production verification.
