# Search

How `/search` works, what the browser decides, what the robot decides, and what
the public Search API decides.

## Doctrine

Search is a service, not a library. Neither the robot nor the browser downloads
a translation, parses it, builds an index, or caches a corpus. A search is one
bounded HTTPS `GET` to `https://search.getbible.net/v2`, which classifies every
run of text by its writing system and applies that system's rules to the corpus
and to the query alike. The caller collects a query and a set of narrowing
filters, sends both, and renders what comes back.

Two callers exist:

- **The Mini App** searches from the browser. The robot is not on the path:
  the request leaves the Telegram WebView for `search.getbible.net` directly,
  the same way chapters come from `api.getbible.net` and explicit references
  from `query.getbible.net`. The robot serves no search endpoint and holds no
  search state.
- **Telegram-native `/search`**, used only when no Mini App is configured, is
  fulfilled by the robot's own thin client to the same API on its own
  executor, semaphore, deadline, and circuit.

Explicit references (`/bible John 3:16`, the Mini App's reference entry, and
the authoritative text behind Post) do not touch the Search API; they resolve
through the Query API at `https://query.getbible.net/v2`.

## The request

```text
GET https://search.getbible.net/v2/{translation}?q=grace&words=all&limit=25&offset=0
```

| Parameter | Values | Default | Bound |
|---|---|---|---|
| `q` | query text | required | 1–500 characters |
| `words` | `all`, `any`, `phrase` | `all` | |
| `match` | `whole_word`, `substring` | `whole_word` | |
| `case_sensitive` | `true`, `false` | `false` | |
| `scope` | `bible`, `old_testament`, `new_testament`, `deuterocanon` | `bible` | |
| `diacritics` | `fold`, `exact` | `fold` | |
| `sort` | `canonical`, `relevance` | `canonical` | |
| `limit` | matches per page | `100` | 1–100 |
| `offset` | index of the first match returned | `0` | 0–10000 |
| `book` | book number or name; repeatable | none | at most 83 combined |
| `exclude` | term to exclude; repeatable | none | at most 32, each 1–100 characters |
| `proximity` | maximum distance between terms; requires `words=all` | none | 0–100 |

`{translation}` is always a catalogue code. The API answers an unrecognised
segment with a `301` to its best guess; the browser sends `redirect: "error"`
and the robot's client refuses redirects, so a redirect is a failure rather
than a silent change of translation.

Filters narrow a search. They do not tell the engine how to read a script: the
service classifies each run of text by Unicode property, so a query that mixes
writing systems keeps each run's own rules, and a translation added to GetBible
later is searchable with no change in this repository.

| Family | Scripts | Searchable unit |
|---|---|---|
| Alphabetic | Latin, Cyrillic, Greek, Armenian, Georgian, Coptic, Cherokee | Words between spaces; accents fold |
| Continuous | Han, Hiragana, Katakana, Hangul, Thai, Lao, Khmer, Myanmar, Tibetan | Overlapping character n-grams with positions |
| Abjad | Hebrew, Arabic, Syriac, Thaana, Samaritan | Words between spaces; pointing folds; a stem behind an attached particle is reachable |
| Brahmic | Devanagari, Bengali, Tamil, Telugu, Kannada, Malayalam, Sinhala | Words between spaces; combining marks are **kept**, because they carry vowels |

In a continuous script `whole_word` and `substring` resolve identically,
because nothing there delimits a word. A reader cannot get that choice wrong.

`fold` removes accents, optional vowel pointing and precomposed letters that
Unicode decomposition alone cannot reach, which is what lets `Duc Chua Troi`
reach `Ðức Chúa Trời`. `exact` turns folding off and distinguishes pointed from
unpointed text.

### One vocabulary, end to end

`fold` and `exact` are the Search API's own words, and they are the only ones
this project uses. The Mini App radio group carries those values, the request
carries those values, the preference store persists those values, and the
Telegram filter dashboard prints those values. Nothing is translated onto
anything else at any boundary.

The older spellings `insensitive` and `sensitive` are not accepted. Mapping
them would have meant every layer carrying two names for one thing, and a
reader's setting passing through a translation table on each hop — the kind of
shim that survives long after anyone remembers why it exists. A profile that
still holds an old spelling falls back to defaults field by field, so the
reader keeps their translation and their place.

## The response

A `200` is `application/json` with three members:

- `matches` — the authoritative order. Each entry names `reference`,
  `book_nr`, `chapter`, and `verse`; a full-text match also carries `score`,
  `occurrences`, and the `terms` that matched.
- `results` — chapter-grouped verse text keyed
  `<abbreviation>_<book_nr>_<chapter>`, each with the translation metadata,
  `book_name`, and a `verses` list of `{chapter, verse, name, text}`. The verse
  behind a match is the entry of `results[key].verses` whose `verse` equals the
  match's.
- `query` — `text`, `kind`, `translation`, `engine_version`, `total`, and
  `returned`; a full-text answer adds `offset`, `limit`, `has_more`, `sha`,
  the normalised `criteria`, and `cache`, `analysis`, and `cost` diagnostics.

`total` is exact, so a page can say how many matches exist rather than
guessing from what it holds. `kind` is `"reference"` when the query text was
itself a Bible reference: the service resolves it and returns those verses
with no `terms`, and a reference answer has no further pages.

Both callers treat the same fields the same way. Reader chapters and search
results normalise to one verse descriptor, and a search result carries the same
direct selection identity as the verse opened in the reader —
`gbd_<translation>_<book>_<chapter>_<verse>` — so there is no second token to
reconcile and Post accepts either.

### Pagination

A page is advanced by `offset + returned` while `has_more` is true. `sha`
identifies the corpus the page was cut from; if a later page reports a
different `sha`, the translation changed underneath the search and the pages
would describe two corpora, so the search restarts from offset zero instead of
combining them.

### Errors

Every failure is `application/problem+json` with `type`, `title`, `status`,
`code`, `detail`, and `instance`, plus `retry_after` and a `Retry-After`
header when the service asks the caller to wait.

| Status | Codes | Retry |
|---:|---|---|
| `400` | `missing_search`, `invalid_search`, `invalid_body`, `unknown_parameter`, `repeated_parameter`, `request_limit` | no; the request is wrong |
| `404` | `translation_not_found`, `unknown_version` | no |
| `429` | `rate_limited` | after `Retry-After` |
| `503` | `busy`, `search_timeout`, `repository_unavailable`, `readiness_failed` | after `Retry-After` |

### Caching

Public `GET` responses carry `Cache-Control` and an `ETag`. The browser's HTTP
cache honours them; nothing from a search enters IndexedDB, and the robot
keeps no search-result cache at all. Results live in page memory or in a
TTL-bounded Telegram interaction session and vanish with it.

## The Mini App

`miniapp/lib/getbible-transport.js` holds four fixed origins — Main, Query,
Search, and Bookmarks — each validated against its expected host. A search is a `GET`
with `Accept: application/json`, credentials omitted, `redirect: "error"`,
`referrerPolicy: "no-referrer"`, the same stall and total deadlines as a
chapter download, and the same bounded retry: `429`, `503`, and other `5xx`
answers are retryable, `400` and `404` are raised on the first attempt. A
non-OK response has its bounded problem document parsed into an error that
carries the code, the status, whether it is retryable, and the announced
`Retry-After` seconds.

`miniapp/lib/getbible-api.js` builds the query string from the reader's filters
(`proximity` only with `words=all`; `book` and `exclude` repeated) and
`miniapp/lib/getbible-model.js` normalises the answer into verse descriptors
in `matches` order. `miniapp/app.js` pages 25 matches at a time: the next page
asks for `offset = results.length`, a changed `sha` restarts from zero, a
reference-kind answer shows its verses with no **Load more**, the summary shows
the exact total, and an error shows whether it is worth retrying and how long
to wait. No session limit is involved; the robot never sees the request.

The Content Security Policy — the `<meta>` element in `miniapp/index.html`
and the header set by the Tornado static handler — allows exactly
`'self' https://api.getbible.net https://query.getbible.net https://search.getbible.net https://bookmarks.getbible.net`
for connections. A proxy that rewrites the header must keep all four.

## Telegram-native `/search`

When no Mini App is configured, `/search` collects the query and filters in a
Telegram panel and the robot's `GetBibleSearchClient` sends the same request
from the host. It is bounded on every side:

| Bound | Setting |
|---|---|
| Base origin | `GETBIBLE_SEARCH_BASE_URL` (HTTPS only) |
| Per-request deadline | `SEARCH_TIMEOUT` |
| Response body | `SEARCH_MAX_RESPONSE_BYTES` |
| Requests in flight | `MAX_CONCURRENT_SEARCHES`, a dedicated executor and semaphore |
| Matches per page | `SEARCH_RESULT_LIMIT`, clamped to the API's 100 |
| Failures | a search circuit separate from the reference circuit |

The client validates the query and filters locally before sending. A `400`
becomes a validation error shown to the reader; `404 translation_not_found`
becomes an unknown-translation reply; `429`, `5xx`, and transport failures
count against the search circuit and are reported as temporary. Nothing here
can occupy a direct-reference permit: the executor, semaphore, and circuit are
the search's own, so a slow Search API delays searches and nothing else.

`/metrics` publishes `getbible_robot_search_circuit_open` for that circuit.
Mini App searches never pass through the robot and are not counted anywhere in
it.

## Highlighting

Rendered results mark the matched terms. The service reports which terms
matched; marking where they occur is done locally, in the browser and in the
robot alike, by mirroring the service's analysis rather than approximating it.
The robot's `modules/search_text.py` classifies a run by script, casefolds,
folds marks, and splits graphemes; the browser's model modules share one
`termHighlights` implementation between reader and search rendering.

- A continuous script has no word boundary, so no boundary is tested. Testing
  for one leaves every Han, kana, Hangul, Thai, Lao, Khmer and Tibetan match
  unmarked — precisely the languages this engine exists to serve.
- An abjad stem sits behind an attached particle, so only its trailing edge is
  a real boundary. `אור` is marked where it occurs inside `והאור`.
- Brahmic and continuous marks are never folded, because they carry vowels.

A reference-kind answer has no terms and receives no highlights.

## Capacity

The robot holds no corpus, no index, and no disk cache, and it runs no
prewarm. Its resident size does not grow with the translations searched,
readiness does not wait for anything to be built, and there is nothing to warm
after a restart.

A Telegram-native search costs one outbound HTTPS request.
`MAX_CONCURRENT_SEARCHES` bounds how many are in flight so that a slow
upstream cannot hold worker threads open; it does not size CPU work, because
there is none. Throughput and latency are the Search API's, which publishes its
own limits and answers `429` or `503 busy` when they are reached. The robot
honours `Retry-After` rather than retrying through it.

## Result volume

`SEARCH_RESULT_LIMIT` bounds what one Telegram page retains; the response also
carries the exact total, and the Mini App shows it. `engine_version` in the
response reports the service's matching semantics and moves whenever they
change, independent of any translation `sha`; when result counts step up under
a stable translation, compare that field before treating it as a regression.
The robot no longer publishes an engine version of its own.

## Related documents

- [Configuration](CONFIGURATION.md) — every search bound and its range.
- [Interactive workflows](INTERACTIONS.md) — the filter dashboard and result flow.
- [Architecture](ARCHITECTURE.md) — where search sits in the two data planes.
- [Browser data](BROWSER_DATA.md) — the four public origins and what the browser keeps.
- [Mini App deployment](MINI_APP.md) — the CSP allowlist and its two enforcement layers.
