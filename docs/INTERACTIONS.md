# Scripture interaction workflows

GetBible keeps immediate Telegram commands native and moves browsing, reading, searching, selecting, copying, and review into the Mini App.

| Command | Result |
| --- | --- |
| `/bible John 3:16` | Native direct-reference post |
| `/bible John 3:16-18` | Native direct-range post |
| `/bible` | Open the Mini App reader at the saved location |
| `/search grace` | Open the Mini App with Librarian search results |
| `/search` | Open the Mini App search form |
| `/help` | Native command help |
| Bot menu | Open the Mini App home screen |

`/get` and `/getbible` remain aliases for `/bible`. The Telegram-native picker remains a compatibility fallback when the Mini App is disabled.

## Reader workflow

1. Open **Bible**.
2. Choose a translation from the shared header selector.
3. Choose a book and chapter.
4. Read the complete chapter loaded directly from `api.getbible.net/v2`.
5. Tap a verse to select it in browser memory.
6. Use the anchored menu above that verse to assign its complete verse to a
   colored bookmark topic, or dismiss it without bookmarking.
7. Tap the verse again to unselect it; an existing bookmark remains independent.
8. Continue navigating; selected verses remain visibly selected during the active WebView session.
9. Open **Selected** to reorder, remove, copy, or Post.

The browser does not call Robot for catalogs, books, chapters, chapter text, selecting, unselecting, reordering, clearing, or copying.

Every successful chapter open and successful verse selection is also recorded
in bounded, user-scoped browser-local reading history. A repeated chapter or
exact verse moves to the top instead of creating another row. **History** is a permanent
fifth bottom-navigation action and opens a normal page, so the
footer remains available for navigation. The page uses the same left-aligned
heading, right-aligned clear action, and bordered empty state as **Selected**;
its top bar keeps only the centered getBible icon. Each row shows its reference,
current translation, and verse text, reopens the exact coordinate in that
currently selected translation, and can be removed individually. **Clear all**
resets the complete history without contacting Robot
or Telegram storage. The record survives later WebView sessions on that browser
but intentionally does not synchronize between devices.

## Home and top-bar workflow

Home presents **Search Scripture** and **Read the Bible** as its two primary
actions. Beneath them, current Selected and History summaries appear when
populated, and Bookmarks is always available to manage topics and recovery. The
permanent footer remains Home, Search, Bible, History, and
Selected; Bookmarks is not added as a sixth footer action.

Search and Bible show the translation control. Home, History, Selected, and
Bookmarks keep only the centered getBible icon, so those page headings and
actions have the same visual rhythm.

## Visual selection contract

A selected reader verse must expose all of the following immediately:

- `aria-pressed="true"`;
- selected row styling;
- selected verse-number styling;
- contiguous selection start/end styling where applicable;
- updated Selected count and navigation badge.

A second click reverses all selected state immediately. The UI derives this state exclusively from `BrowserSelectionStore`; it does not wait for a network round trip.

## Search workflow

Search is the sole Mini App Scripture-discovery operation that uses Robot/Librarian.

1. Submit a query and optional filters.
2. Robot/Librarian returns bounded normalized result verses.
3. The browser registers those verses with the same selection store used by reader chapters.
4. Selecting a search result highlights the same coordinate when opened in Reader.
5. Selecting that coordinate in Reader updates its Search representation.

Opaque search tokens and deterministic reader IDs are not selection identity. Identity is translation, book number, chapter, and verse.

Filters narrow a search; they never tell Librarian how to read a script. The
words, match, scope, case, diacritics, sort, books, exclusion and proximity
controls are documented with their values and defaults in [Search](SEARCH.md),
which also covers why a Chinese or Thai query needs no special handling and why
the diacritics filter now folds by default.

## Explicit reference workflow

Mini App explicit references use `query.getbible.net/v2` directly. Query results provide reader coordinates and grouped-reference results without routing through Robot or Librarian.

A Query API failure affects explicit reference resolution only. It does not invalidate authentication, cached chapters, local selection, or Search state.

## Selected workflow

The Selected screen is a view of the browser-owned ordered selection.

Available actions:

- open a selected verse in Reader;
- move it earlier or later;
- remove it;
- clear all selections;
- copy formatted Scripture;
- Post the final ordered selection.

Reorder, remove, clear, and copy are local. They issue no Robot request.

## Bookmark workflow

Bookmarks are complete canonical verses. Their identity is book, chapter, and
verse across translations, so assigning an already bookmarked coordinate from
another translation updates the one existing bookmark and moves it to the
chosen topic.

1. Select a Bible verse to open the anchored topic menu above it.
2. Choose one colored topic to add or move the bookmark, or remove its existing
   bookmark.
3. From Home, choose **Manage** in the Bookmarks summary.
4. Search localized built-in topic names or custom topic names, open a topic to
   review its verses, or add, rename, recolor, and remove custom topics.
   Global definitions are server-owned and stay outside the personal topic
   editor; use their global controls to change visibility or verse exclusions.
5. Open a bookmarked verse in Bible or remove it from the topic detail view.

Above topic search, **Add all** and **Remove all** control the complete bundled
global-topic catalog. The adjacent information disclosure explains that these
are curated sets of verses matching each topic. The same add/remove choice
remains available inside each topic, and personal topics and bookmarks are
preserved when global topics are removed.

The browser writes each change to its scoped local copy immediately. On a
supported Telegram client it asynchronously reconciles the newest timestamped
bookmark/topic aggregate through `DeviceStorage` and `CloudStorage`. Compact
last-read coordinates use the same hybrid strategy. History, selections,
translation catalogs, and chapters remain browser-only.

Global-topic visibility, exclusions, and renamed-topic mapping use a separate
scoped local/`DeviceStorage` replica so Telegram Desktop can restore them after
discarding WebView storage. They never use `CloudStorage` and never enter the
personal bookmark aggregate or backup.

### Backup and restore

The Bookmarks page supports two portable recovery paths:

- **Download JSON** and **Import bookmarks** operate on a bounded, versioned
  file in the browser;
- **Back up to chat** sends that validated JSON document to the user's private
  bot chat with an owner-bound **Restore bookmarks** button.

The chat document remains the durable artifact. Pressing Restore on any
Telegram client must occur in that owner's private bot chat and creates a fresh,
short-lived, one-time Mini App launch. The page retrieves the document, shows
its counts and asks before merging. It persists the merged state before
acknowledging the restore reference, so a failed acknowledgement can be safely
retried from the chat message. Robot never stores the backup body in database
or session state and never writes it to structured logs.

## Post workflow

Post is the only selection synchronization boundary.

1. Freeze one ordered browser snapshot.
2. Submit the bounded final selection once.
3. Robot validates ownership, coordinates, order, and idempotency.
4. Robot obtains authoritative Scripture and renders the complete Telegram output.
5. Robot sends to the originating chat/topic.
6. Success clears the browser selection.
7. Failure preserves it unchanged for retry.

Browser display text, names, references, and UI IDs are never final posting authority.

## Copy workflow

Copy uses the browser snapshot and does not Post or mutate Robot state. It preserves Unicode, translation labels, reference grouping, and verse order. Copy success or failure leaves the selection unchanged.

## Translation changes

Changing translation immediately updates interface language/direction and reloads the same canonical reader location from Main API. Old chapter text is removed before the new request begins so translations cannot appear mixed.

Selections from different translations may coexist. Coordinate identity includes the translation code.

## Navigation and accessibility

- The translation selector remains reachable on Search and Bible in fullscreen
  and non-fullscreen layouts; icon-only routes do not expose it.
- The passage picker supports keyboard, Escape, backdrop, Close, and Telegram Back behavior.
- Selected state is communicated visually and through ARIA.
- Focus returns to a useful reader or error target after navigation and retry.
- Reduced-motion preference is respected.
- RTL layout preserves the same interaction semantics.

## Failure behavior

| Failure | Result |
| --- | --- |
| Main API unavailable | uncached reader request fails retryably; selection and authentication remain |
| Query API unavailable | explicit reference resolution fails retryably |
| Librarian unavailable | Search alone fails |
| Robot unavailable | protected Search/preferences/Post fail; local selection remains usable |
| Telegram Mini App storage unavailable | local bookmarks and last-read remain usable; cross-device sync is marked degraded |
| Chat restore fails before merge | current bookmarks remain; local JSON export/import remains available |
| Restore persistence or acknowledgement fails after merge | imported merge remains; chat document remains recoverable for retry |
| Post fails | ordered selection remains intact |
| Session expires | protected actions fail closed; identity-free cached Scripture remains public data |

## Acceptance criteria

A release must demonstrate in real Chromium and Telegram that:

- reader selection highlights immediately;
- verse number highlighting is visible;
- second-click unselect works;
- selected styling survives chapter navigation and source changes;
- Search and Reader are interchangeable by coordinate identity;
- no pre-Post Robot basket or Scripture mutation occurs;
- failed Post preserves selection;
- successful Post delivers authoritative Scripture and clears selection;
- history survives a new local WebView session but does not move to another device;
- bookmark topics and last-read reconcile between supported Telegram clients;
- private-chat backup/restore enforces ownership, confirmation,
  persistence-before-acknowledgement, and bounded JSON validation.
