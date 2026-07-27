# Scripture interaction workflows

GetBible deliberately keeps the shortest command path native and moves every
browsing workflow into the contained Telegram Mini App.

| Command | Result |
|---|---|
| `/bible John 3:16` | Validate, retrieve, and post the verse immediately |
| `/bible John 3:16-18` | Validate, retrieve, and post the range immediately |
| `/bible` | Open the Mini App Bible browser |
| `/search grace` | Open the Mini App on contained results for `grace` |
| `/search` | Open the Mini App search form and filters |
| `/help` | Show native command help |
| Bot menu button | Open the Mini App home screen |

`/get` and `/getbible` remain aliases for `/bible`. When `MINI_APP_ENABLED` is
false, the existing Telegram-native picker remains available as a compatibility
fallback.

## Bible browser

The Mini App follows a readable selection sequence:

1. choose a translation;
2. choose a book;
3. choose a numbered chapter;
4. read the complete, wrapping text of every verse in that chapter;
5. tap the actual verse card to select or remove it;
6. add verses from other chapters, books, searches, or translations;
7. review, reorder, or clear the server-held basket;
8. press **Post selected verses** once.

Chapter buttons remain compact numbers. Verse selection never uses number-only
buttons: the reference and complete verse text occupy the same selectable card.
Selected cards have a visible pressed state and an accessible `aria-pressed`
state. The basket can contain isolated verses or contiguous verses; the server
normalizes adjacent selections into safe references at post time.

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
| Translation | Available repository translations |
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

The selected Bible translation is stored per Telegram user. Safe search
defaults may also be stored, but query text, excluded words, launch state,
session credentials, and verse text are not durable preferences.

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

See [Telegram Mini App deployment](MINI_APP.md) for the HTTPS boundary,
BotFather configuration, reverse-proxy examples, and production verification.
