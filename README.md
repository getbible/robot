# GetBible Robot

GetBible Robot is a hardened Telegram interface for Scripture reading, search, selection, copying, and posting. The Mini App uses GetBible API V2 directly for public Scripture data and keeps temporary selection state in the browser. Robot remains the authenticated Telegram control plane and the sole adapter for Librarian full-text search.

## Architecture at a glance

```text
Telegram Mini App
  ├─ translations / books / chapters / hashes → https://api.getbible.net/v2
  ├─ explicit and grouped references          → https://query.getbible.net/v2
  ├─ temporary ordered selection              → browser memory
  └─ auth / preferences / search / final Post → Robot
                                                └─ search only → Librarian
```

Only full-text search and search pagination use Librarian.

A normal reader action does not pass through Robot. Selecting, unselecting, reordering, clearing, highlighting, counters, and copying are browser-owned and issue no Robot request. Final Post is the only selection synchronization boundary; Robot validates authoritative Scripture before Telegram delivery.

See [Architecture](docs/ARCHITECTURE.md), [Browser data](docs/BROWSER_DATA.md), [Mini App](docs/MINI_APP.md), and [Interactions](docs/INTERACTIONS.md).

## Commands

```text
/bible 1 John 3:16
/bible John 3:16-19;1 John 3:10-17
/bible Gen 1:1-5 codex
/bible Ps 1:1-5 aov
/bible
/search grace
/search
/help
```

`/get` and `/getbible` are aliases of `/bible`.

- An explicit `/bible <reference>` keeps the native fast path and posts immediately.
- Bare `/bible` opens the Mini App reader at the saved translation/book/chapter/verse.
- `/search <query>` opens Librarian-backed results in the Mini App.
- Bare `/search` opens the search form.
- Intermediate browsing never floods the chat.

## Mini App behavior

The Mini App has Home, Bible, Search, and Selected surfaces.

- Translation metadata, localized books, chapter maps, chapter text, and hashes come directly from Main API.
- Explicit references use Query API.
- Public data is cached in bounded IndexedDB with exact-scope hash revalidation and in-memory fallback.
- Reader and search verses normalize to one descriptor.
- Coordinate identity is translation, book number, chapter, and verse.
- A verse selected in Search is selected in Reader and vice versa.
- A second click unselects immediately.
- Selected verse number/body styling, ARIA state, range boundaries, counters, and copy output derive from `BrowserSelectionStore`.
- Failed Post preserves the complete ordered browser selection.
- Successful Post clears it.
- A bounded, coordinate-only reading history remembers opened chapters and
  selected verses for the active browser session. Each entry keeps its
  translation and can be reopened or removed individually; the complete
  history can also be cleared.

Browser display text and UI identifiers are not final posting authority.

## Security boundaries

The public Mini App shell is not an authentication boundary.

Robot protects actions with:

- fresh Telegram-signed `initData`;
- owner-bound, one-time launch tokens;
- bounded opaque sessions with a three-hour default absolute lifetime;
- user/chat/topic binding;
- bounded request bodies and output;
- per-user, per-chat, and trusted-client rate limits;
- idempotent final posting;
- escaped Telegram HTML and UTF-16-aware chunking;
- correlation IDs and user-safe errors.

The bot token remains server-side. It is never placed in HTML, JavaScript, URLs, browser storage, public API traffic, or logs.

Public API transport:

- uses only `https://api.getbible.net/v2/` and `https://query.getbible.net/v2/`;
- omits credentials and cookies;
- sends no Telegram data;
- rejects redirects;
- uses `no-referrer`;
- enforces timeout, size, schema, and coordinate bounds.

## Cache integrity

The browser cache stores public, identity-free data only under a versioned namespace. It has bounded record count, bounded total bytes, bounded per-record bytes, least-recently-used eviction, and request coalescing.

Every cached scope stores its published SHA-1 and is revalidated at least weekly. Parent hash changes invalidate descendants. Chapter replacement requires stable pre/post hashes, exact-byte SHA-1 verification, bounded schema validation, and atomic replacement. Failed validation never overwrites a valid record.

## Search isolation

Search and pagination alone use Librarian. They have separate bounded execution, timeout, cache, and circuit behavior so expensive corpus work cannot consume every direct-reference permit.

Search failure does not affect reader navigation. Main API or Query API failure does not invalidate Telegram authentication.

Librarian derives the matching strategy from the query text, so the robot ships
no per-language branch and no match-mode detector. Chinese, Japanese, Korean,
Thai, Lao, Khmer, Myanmar and Tibetan queries reach the index under the default
filters, unaccented Greek reaches accented text, and an unpointed Hebrew or
Arabic stem reaches the word behind its attached particle. See
[Search](docs/SEARCH.md).

## Runtime and deployment

Supported runtime:

- Python 3.10, 3.11, 3.12, 3.13, or 3.14;
- Linux Docker/OCI for portable deployment;
- Linux with `systemd` for host-native deployment;
- a Telegram bot token;
- outbound HTTPS to Telegram and GetBible API;
- public HTTPS when the Mini App is enabled.

The host deployment keeps health, webhook, and Mini App listeners separate and loopback/private behind Caddy. Docker contains no Caddy or systemd and leaves TLS/ingress to the platform.

Published images are available from GitHub Container Registry:

```bash
docker pull ghcr.io/getbible/robot:2.1.0
```

Use exact reviewed image/version tags for production and rollback.

## Docker quick start

```bash
./setup.sh docker-init
${EDITOR:-vi} .env
./setup.sh docker-validate
./setup.sh docker-deploy
./setup.sh docker-doctor
```

The default Compose deployment runs one bot in one bounded, non-root, read-only container. Multi-bot mode is explicit and requires unique ports and isolated state.

See [Docker deployment](docs/DOCKER.md).

## Host-native installation

```bash
git clone https://github.com/getbible/robot.git
cd robot
git checkout --detach <reviewed-commit-sha>
sudo ./setup.sh install
```

The manager creates an isolated service identity, exact hashed environment, root-only token/configuration, bounded cache/state/log paths, health listener, Mini App listener, and hardened systemd unit.

Use the manager for operations:

```bash
sudo getbible-robot status <instance>
sudo getbible-robot doctor <instance>
sudo getbible-robot miniapp <instance>
sudo getbible-robot update <instance>
```

Do not edit generated Caddy/systemd configuration directly.

## Dependency policy

Human-maintained intent lives in `requirements.in` and `requirements-dev.in`. Production and CI install exact hashed locks from `requirements.txt` and `requirements-dev.txt`.

Robot supports compatible Librarian 2.x releases beginning with 2.0.0:

```text
getbible>=2.0.0,<3
```

The reviewed runtime lock currently selects a specific released version. Production never resolves an unreviewed latest dependency during startup.

See [Dependency policy](docs/DEPENDENCIES.md).

## Development and verification

```bash
git clone https://github.com/getbible/robot.git
cd robot
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
(cd miniapp && npm ci --ignore-scripts && npx playwright install chromium)
bash scripts/run-checks.sh
```

Focused iteration:

```bash
venv/bin/python -m unittest discover -s tests -v
(cd miniapp && npm run check)
(cd miniapp && npm run test:browser)
```

The permanent release gate requires:

- Python 3.10, 3.11, 3.12, 3.13, and 3.14;
- production container build and smoke test;
- Ruff, strict mypy, and branch coverage;
- browser unit and real Chromium tests;
- public API routing and CSP parity;
- cache hash/invalidation/bounds tests;
- browser selection add/remove/reorder/clear and visual highlight tests;
- browser reading-history record/reopen/remove/clear and persistence tests;
- no pre-Post Robot selection mutation;
- authoritative idempotent Post tests;
- Bandit, dependency audit, secret scan, systemd verification, and CodeQL.

See [Testing](docs/TESTING.md) and [Release gate](docs/RELEASE_GATE.md).

## Production acceptance

After deploying one exact green commit, verify:

1. bare `/bible` opens the reader;
2. cold and warm chapter loads work;
3. explicit references resolve through Query API;
4. selecting highlights verse number and body;
5. second-click unselect works;
6. Search and Reader selections interoperate;
7. navigation preserves selected styling;
8. Copy does not Post or clear selection;
9. failed Post preserves selection;
10. successful Post delivers authoritative Scripture and clears selection;
11. reading history reopens the exact verse and translation;
12. individual and complete history clearing work;
13. private command and launcher cleanup still works.

Record the deployed commit SHA and permanent CI/CodeQL run links with release evidence.

## License

See the repository license and the copyright metadata returned for each Scripture translation. Robot's software license does not relicense Scripture translations or override publisher terms.
