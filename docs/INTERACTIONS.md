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
6. Tap it again to unselect it.
7. Continue navigating; selected verses remain visibly selected during the active WebView session.
8. Open **Selected** to reorder, remove, copy, or Post.

The browser does not call Robot for catalogs, books, chapters, chapter text, selecting, unselecting, reordering, clearing, or copying.

Every successful chapter open and successful verse selection is also appended
to bounded browser-session reading history. The history control appears in the
expanded bottom navigation only in the Bible view and opens a full-screen list.
The centered header remains fixed inside Telegram's protected fullscreen lane
while entries scroll independently. Each row shows its reference and
translation, reopens the exact coordinate when chosen, and can be removed
individually. **Clear all** resets the complete history without contacting Robot.

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

- The shared translation selector remains reachable in fullscreen and non-fullscreen layouts.
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
- successful Post delivers authoritative Scripture and clears selection.
