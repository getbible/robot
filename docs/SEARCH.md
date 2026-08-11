# Search

How `/search` works, what the robot decides, and what Librarian decides.

## Doctrine

The robot does not decide how a writing system should be read. It collects a
query and a set of narrowing filters, hands both to Librarian, and renders what
comes back. Librarian classifies every run of text by its writing system and
applies that system's rules to the corpus and to the query alike.

This is the single most important thing to understand about the current
implementation, because the previous one did the opposite.

## What changed, and why it mattered

Under Librarian 1.x the robot inspected the query itself. If it saw a character
from a continuous script — Han, kana, Hangul, Thai — it rewrote the search from
whole-word to substring before sending it. That produced two defects:

| Query | 1.x behaviour | 2.x behaviour |
|---|---|---|
| `神` under default filters | returned nothing | returns the verses |
| `Jesus 耶稣` | whole query became substring, so `all` matched inside `shall` | each run keeps its own rules |
| `אור` against pointed Hebrew | missed | reaches `וְהָאוֹר` by its stem |
| `λογος` unaccented | missed | reaches `λόγος` |

The rewrite also mutated the filter stored in the user's own session, so a
Chinese search silently changed the reader's saved match mode.

That branch is gone. `requires_substring_matching()` returns `False` for every
query in Librarian 2 and is not imported anywhere in this repository.

## How a unit is found

| Family | Scripts | Searchable unit |
|---|---|---|
| Alphabetic | Latin, Cyrillic, Greek, Armenian, Georgian, Coptic, Cherokee | Words between spaces; accents fold |
| Continuous | Han, Hiragana, Katakana, Hangul, Thai, Lao, Khmer, Myanmar, Tibetan | Overlapping character n-grams with positions |
| Abjad | Hebrew, Arabic, Syriac, Thaana, Samaritan | Words between spaces; pointing folds; a stem behind an attached particle is reachable |
| Brahmic | Devanagari, Bengali, Tamil, Telugu, Kannada, Malayalam, Sinhala | Words between spaces; combining marks are **kept**, because they carry vowels |

Classification is by Unicode property, not by a language tag and not by a
maintained range table. A translation added to the GetBible API later is
classified from its text with no change in this repository.

## Filters

The robot exposes Librarian's criteria as user-facing filters. They narrow a
search; they do not tell the engine how to read a script.

| Filter | Values | Default |
|---|---|---|
| Words | `all`, `any`, `phrase` | `all` |
| Match | `whole_word`, `substring` | `whole_word` |
| Scope | `bible`, `old_testament`, `new_testament`, `deuterocanon` | `bible` |
| Case | on, off | off |
| Diacritics | `fold`, `exact` | `fold` |
| Sort | `canonical`, `relevance` | `canonical` |
| Books, Exclude, Proximity | see [Interactive workflows](INTERACTIONS.md) | empty |

In a continuous script `whole_word` and `substring` resolve identically, because
nothing there delimits a word. A reader cannot get that choice wrong.

`fold` removes accents, optional vowel pointing and precomposed letters that
Unicode decomposition alone cannot reach, which is what lets `Duc Chua Troi`
reach `Ðức Chúa Trời`. `exact` turns folding off and distinguishes pointed from
unpointed text.

### One vocabulary, end to end

`fold` and `exact` are Librarian's own words, and they are the only ones this
project uses. The Mini App radio group carries those values, the JSON API
accepts those values, the preference store persists those values, and the
Telegram filter dashboard prints those values. Nothing is translated onto
anything else at any boundary.

The 1.x spellings `insensitive` and `sensitive` are not accepted. Mapping them
would have meant every layer carrying two names for one thing, and a reader's
setting passing through a translation table on each hop — the kind of shim that
survives long after anyone remembers why it exists.

The cost is bounded and paid once. A profile stored before the upgrade holds the
1.x spelling, so its filters fall back to defaults on first read; because a
stored profile degrades field by field, the reader keeps their translation and
their place. A Mini App page still open from before the upgrade sends the old
value and is refused until it reloads, which it does on next open.

## Highlighting

Rendered results mark the matched terms. The renderer reads each term under the
rules of its own writing system, using Librarian's own folding and script
classification rather than a local approximation:

- A continuous script has no word boundary, so no boundary is tested. Testing
  for one leaves every Han, kana, Hangul, Thai, Lao, Khmer and Tibetan match
  unmarked — precisely the languages this engine exists to serve.
- An abjad stem sits behind an attached particle, so only its trailing edge is a
  real boundary. `אור` is marked where it occurs inside `והאור`.
- Brahmic and continuous marks are never folded, because they carry vowels.

## Capacity

Search runs on its own executor, semaphore, timeout and circuit breaker,
separate from reference delivery, so corpus work cannot consume every direct
reference permit.

### What one instance can serve

Measured against a KJV-shaped corpus — 30,888 verses, 11,908 distinct terms,
four cores:

| | |
|---|---|
| Corpus parse | 67 ms |
| Index build | 2.9 s, about 27 MB resident |
| Search, ordinary query | 20–35 ms |
| Search, very common word (26,915 matches) | 106 ms |
| Sustained throughput | **~45 searches/second per process** |

Searches share one already-parsed corpus and one already-built index; nothing is
re-read or re-analysed per request. What they do not share is the interpreter.
Matching is CPU-bound Python and holds the GIL, so throughput is flat in the
number of workers while latency grows with it:

| Concurrent workers | Throughput | Mean latency |
|---:|---:|---:|
| 1 | 44.6/s | 22 ms |
| 2 | 42.1/s | 48 ms |
| 4 | 42.6/s | 94 ms |
| 8 | 35.4/s | 226 ms |
| 16 | 29.3/s | 546 ms |

`MAX_CONCURRENT_SEARCHES` therefore defaults to 4 — enough that one expensive
query cannot stall every other reader, and not so many that everyone waits
behind a saturated interpreter. Setting it to 100 would not serve 100 readers
faster; it would serve them at roughly the same total rate with latencies near
the search deadline.

**Scale out with processes, not threads.** Each additional instance brings its
own interpreter and its own ~45/s, and the deployment already supports running
several. The cost is one resident corpus set per process, so size
`SEARCH_SHARED_CORPUS_LIMIT` per instance rather than assuming one figure covers
the host.

The per-process ceiling is Librarian's matching loop. Raising it means letting
that loop run without the GIL, which is a change in the library rather than
here.

### Index construction

An index is built once per **translation and policy** — the pair of case
sensitivity and diacritics folding — and is then shared by every later request
using that pair. Because the filter dashboard exposes both toggles, one
translation can hold up to four indexes, and they are not evicted individually.

Librarian bounds a build with `SEARCH_INDEX_BUILD_SECONDS` rather than charging
it to a request's `SEARCH_DEADLINE_SECONDS`. A build serves every later search,
so abandoning it because one caller's clock ran out made that caller fail and
left the next caller to repeat the same work.

Concurrent first requests now wait on one build rather than each starting their
own, and corpora live in a process-wide registry keyed by repository,
translation and source SHA, so the parse-and-analyse cost is paid once per
translation version rather than once per client object.

### Two caches, sized for different things

`SEARCH_CORPUS_LIMIT` bounds one client's dictionary of corpus handles.
`SEARCH_SHARED_CORPUS_LIMIT` bounds the process-wide registry that holds the
corpora themselves. Only the second one decides whether work is repeated.

Reuse is the point of that registry. A translation that is already parsed and
analysed is answered from it, so a second search of the same translation — and a
reader moving back and forth between two — does no parsing and no indexing at
all. Sizing it down to the per-client limit would undo that: every switch would
re-read and re-analyse a corpus the process had already built. It therefore
defaults to eight, comfortably above the handle limit, and should be raised on
an instance serving many translations rather than lowered to save memory.

Lowering it does not really save memory; it converts a bounded, one-off cost
into an unbounded, repeated one.

### Prewarming

With `PREWARM_DEFAULT_TRANSLATION` enabled the robot builds the default
translation's corpus and index before Telegram accepts traffic, spending the
index budget rather than the interactive lookup timeout. A warm index means no
request ever pays for a build. A failed prewarm is logged and startup continues;
the first search then pays the build instead.

An index is keyed by its case and diacritics policy, so the prewarm passes the
same policy a default search uses. Warming under a different one is not a
partial win but a total loss: it builds an index no search reads, and the first
real search still pays the whole build inside the request path. Librarian's
hardened client still defaults that argument to the 1.x spelling, which resolves
to `exact`, so the robot passes it explicitly and a test asserts the two cannot
drift apart.

Searching a *non-default* translation still triggers a cold build. That request
is bounded by `LOOKUP_TIMEOUT` and may time out, but the build continues in its
worker and serves every later search of that translation.

## Result volume

Default searches return **more** than they did under 1.x, because continuous
scripts match at all now and folding is the default. `SEARCH_RESULT_LIMIT`
bounds what one page retains; the response also carries an exact total.

`SEARCH_ENGINE_VERSION` is `4`. It moves whenever matching semantics change,
independent of any translation SHA, and is published as
`getbible_robot_search_engine_version` on `/metrics` so an operator can tell an
upgrade apart from a regression when result counts move.

The robot keeps no durable search-result cache. Results live in TTL-bounded
interaction sessions that cannot outlive the process that upgraded the library,
so there is nothing to invalidate on an engine change.

## Related documents

- [Configuration](CONFIGURATION.md) — every search budget and its range.
- [Interactive workflows](INTERACTIONS.md) — the filter dashboard and result flow.
- [Architecture](ARCHITECTURE.md) — where search sits in the two data planes.
- [Dependencies](DEPENDENCIES.md) — the Librarian release policy.
