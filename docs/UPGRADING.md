# Upgrading and rollback

Upgrade one instance from a clean checkout of the exact reviewed target commit. The manager builds and validates the replacement before stopping the running service.

## Preconditions

- Target commit has green CI/security and CodeQL gates.
- Host Python remains in the supported 3.10–3.14 range.
- Configuration and release notes were reviewed.
- A dedicated test instance passed the live Telegram smoke matrix.
- Current `status` and `runtime` output were recorded.
- The currently deployed application is healthy and available for rollback.

## Upgrade

For the normal server checkout, update `master` and deploy the complete
application in place:

```bash
cd ~/robot
git switch master
git pull --ff-only
git status --short
sudo ./setup.sh update production --source "$PWD"
```

`git status --short` must print nothing. The `update` command is an alias for
the transactional `upgrade` operation and is also available as **Update /
upgrade deployment** in the interactive maintenance menu. It replaces the
entire installed application tree, including `miniapp/`, its interface
catalogs, styles, and images. It then restarts the instance and verifies both
Robot readiness and the configured Mini App HTTPS route. The route postflight
checks the shell and both contribution endpoints; an empty proxy 404 or an HTML
fallback cannot pass as a healthy API.

The repository owns `setup.sh` as an executable file. Run it directly; a fresh
checkout must not require `chmod`. Although the manager itself runs through
`sudo`, it inspects the operator-owned source checkout with Git's optional
locks disabled. The deployment must not refresh or change ownership of
`.git/index`.

Every packaged Mini App file is served `no-store`, and the shell loads its
JavaScript and stylesheet from `build/<fingerprint>/`, a prefix derived from
the bytes of the complete packaged client. A launch therefore downloads the
exact JavaScript, CSS, locale catalogs, and branding the running server ships,
and a WebView that ignores `no-store` and keeps the previous commit's modules
in its cache never has them asked for again: the new shell names addresses no
earlier deployment used. The menu button's fixed URL carries the same
fingerprint as a query so a menu launch is a new address as well. Sessions
live in server memory, so the upgrade restart also signs out any Mini App view
that was already open — launching `/bible` or `/search` again always runs the
deployed commit. The postflight fetches the shell and then the module it
names, so a proxy that stopped forwarding `build/*` fails the upgrade instead
of shipping a shell whose scripts answer with an empty 404.

For a separately reviewed checkout pinned to an exact commit:

```bash
git clone https://github.com/getbible/robot.git robot-upgrade
cd robot-upgrade
git checkout --detach <reviewed-target-sha>
git status --short
sudo ./setup.sh upgrade production --source .
```

The previously installed manager compares its upgrade logic with the reviewed
target. If the target checkout manager is newer, it hands the operation to that
exact checkout before configuration migration or route generation begins. This
prevents an already-running older process from applying an obsolete Caddy
route schema while deploying newer application code. Current generated routes
forward the bounded `/api/v1/*` namespace so a new backend endpoint does not
require fragile per-endpoint proxy enumeration.

An update to the commit that is already deployed is a supported repair path.
It does not rotate the application trees. It reapplies backwards-compatible
configuration migrations, manager and unit files, service limits, and all
setup-managed Caddy routes, restarts the instance, and runs the same postflight.
This makes a normal repeated `update` sufficient to repair stale generated
routes.

The manager:

1. confirms the current and target exact commits;
2. adds missing backwards-compatible configuration defaults, including disabled
   Mini App settings, without printing or replacing existing secrets;
3. clones the target into `app.next`;
4. builds a fresh virtual environment;
5. installs the target lock with `--require-hashes`;
6. runs `pip check`;
7. validates the existing instance environment with the target code;
8. installs and verifies the target manager/unit;
9. transactionally regenerates and reloads setup-managed Caddy routes when
   enabled;
10. stops only the selected instance;
11. atomically replaces `app` and retains `app.previous`;
12. starts the service and waits for readiness;
13. verifies the configured Mini App shell and both contribution API routes on
    the local listener and public HTTPS URL, requiring Robot's JSON `401`
    response rather than accepting an HTML fallback;
14. automatically restores the prior Caddy files and application if
    readiness fails.

Source, virtual environment, and lock are always moved as one application tree. The service never combines code from one commit with a lock from another.

After success:

```bash
sudo getbible-robot doctor production
sudo getbible-robot logs production 200
```

Complete the private and group smoke tests in [Testing](TESTING.md).

## Manual rollback

The manager retains one immediately previous application:

```bash
sudo getbible-robot rollback production
```

It displays both commit SHAs, requires explicit confirmation, swaps the complete application trees, starts the selected service, and waits for readiness. If the rollback target itself fails readiness, the original application is restored.

When the Mini App is enabled, rollback verifies both its local listener and
public HTTPS path before accepting the target.

Rollback does not silently replace the environment file. If a future release
requires a configuration change, follow that release's documented upgrade
instructions before retrying.

The ninety-day Mini App session change is deliberately rollback-safe. Fresh
explicit configurations write `MINI_APP_SESSION_TTL_SECONDS=7776000`; older
physical hour-scale values in the former `60`–`86399` range are left
unchanged and normalized to ninety days only by the new runtime. When an
older host or Docker environment omits the key, the manager and versioned
Compose files pass the old-safe raw default `900`, which the new runtime also
normalizes. The immediately previous application or container image can
therefore still read the same configuration after rollback.

## Configuration migrations

1. Compare `.env.template` with `/etc/getbible-robot/<instance>.env` without printing secret values.
2. Use `sudo getbible-robot config <instance>`.
3. Add new keys deliberately.
4. Let the manager validate the complete file.
5. Restart and verify readiness.
6. Retain the prior secret file only according to the approved secret-retention policy.

`INSTANCE_NAME`, `LOG_FILE`, `HEALTH_PORT`, `MINI_APP_ENABLED`,
`MINI_APP_PUBLIC_URL`, `MINI_APP_LISTEN`, and `MINI_APP_PORT` remain
manager-owned. Mini App and webhook ports must be unique and must not match.

## Dependency updates

Maintainers edit `requirements.in` or `requirements-dev.in`, regenerate both locks with `scripts/refresh-locks.sh`, review the complete generated diff, and run the full Python 3.10–3.14 matrix. Production installs only the exact reviewed `requirements.txt` with hashes.

The direct runtime inputs are `python-telegram-bot`, `requests`, `tornado`, and
`python-dotenv` plus two compatibility pins; there is no Scripture engine among
them. The generated lock selects the exact tested artifacts. Production does
not resolve a newer package during service start.

### Upgrading to the public Search API

This release removes the Librarian (`getbible`) package from the robot. The
Mini App searches `https://search.getbible.net/v2` directly from the browser,
`/bible` references and the authoritative text behind Post resolve through
`https://query.getbible.net/v2`, and the Telegram-native `/search` used when no
Mini App is configured is answered by the robot's own client to the Search
API. Nothing needs migrating by hand. Expect the following and see
[Search](SEARCH.md) for the design.

- **Removed settings are ignored with a warning.** `SEARCH_CORPUS_LIMIT`,
  `SEARCH_SHARED_CORPUS_LIMIT`, `SEARCH_DEADLINE_SECONDS`,
  `SEARCH_INDEX_BUILD_SECONDS`, `PREWARM_DEFAULT_TRANSLATION`,
  `REFERENCE_CACHE_LIMIT`, `BOOKS_CACHE_LIMIT`, `CHAPTER_CACHE_LIMIT`,
  `TRANSLATION_CACHE_LIMIT`, `CACHE_MAX_BYTES`,
  `CACHE_MAINTENANCE_INTERVAL_SECONDS`, and
  `MINI_APP_MAX_SEARCHES_PER_SESSION` are no longer read. An instance file
  that still contains them starts normally and logs one warning per variable
  at startup. The manager does not rewrite them, so the immediately previous
  release can still read the same file after a rollback; remove them with
  `sudo getbible-robot config <instance>` once rollback is no longer needed.
- **`SEARCH_TIMEOUT` is now a per-request deadline.** Fresh installs carry
  `30` seconds. The manager and the versioned Compose files leave a legacy
  `150` in place, because the previous release refuses any value below its
  index-build allowance and rollback must stay one step; lower it
  deliberately after the rollback window. `SEARCH_RESULT_LIMIT` keeps its
  range and is clamped to the Search API's 100 at request time.
- **Two new settings.** `GETBIBLE_QUERY_BASE_URL`
  (`https://query.getbible.net`) and `GETBIBLE_SEARCH_BASE_URL`
  (`https://search.getbible.net`) direct the robot's own calls and accept
  HTTPS origins only. The manager adds both on upgrade; the defaults apply
  where they are absent.
- **Outbound HTTPS from the host now also reaches `query.getbible.net` and
  `search.getbible.net`.** Update egress firewalls and proxies before the
  upgrade; a blocked origin fails `/bible` or the Telegram-native `/search`
  with a temporary-unavailable reply and opens the matching circuit.
- **The Mini App's Content Security Policy gains `https://search.getbible.net`**
  in both the HTML shell and the Tornado header. An external proxy that
  rewrites the header must include it, or every search is blocked by the
  browser.
- **Startup is faster.** There is no prewarm and no index; readiness no
  longer waits for a translation to be downloaded and built, and the
  process's memory no longer grows with the translations searched. The
  instance cache directory is no longer written; its contents can be removed
  once rollback is no longer needed.
- **A Mini App page open across the upgrade must reload.** The robot's
  `/api/v1/search` routes are gone, so a stale page's search answers `404`
  until it reopens. Sessions live in memory, so the upgrade restart already
  requires a fresh launch.
- **No cache to clear.** Neither the robot nor the browser keeps a durable
  search-result cache; the browser honours the Search API's `Cache-Control`.

Rollback reinstalls the previous release's own lock, Librarian included, and
reads the same environment file. This release does not change the preference
vocabulary, so rollback is not lossy for preferences.

### Contribution store schema

The contribution store remains schema v5. A production database left at
`user_version=6` by the withdrawn Telegram `web_app_data` push transport is
downgraded automatically the next time Robot opens it: the two dormant push
staging tables are dropped and `user_version` returns to 5. Contributor,
event, and catalogue state are untouched, and no operator action is required.

## Docker upgrade and rollback

Docker deployments upgrade by changing the explicit `ROBOT_IMAGE` value in the
private Compose environment and letting the setup manager pull, validate, and
recreate the workload:

```bash
cd ~/robot
cp -- .env ".env.before-$(date +%Y%m%dT%H%M%S)"
${EDITOR:-vi} .env
./setup.sh docker-validate
./setup.sh docker-update
./setup.sh docker-doctor
```

For example:

```dotenv
ROBOT_IMAGE=ghcr.io/getbible/robot:2.1.0
```

Use an exact semantic version or `sha-<full-commit>` tag for production. Do not
use `edge`; it is the CI-approved `master` channel for pre-production testing.
The persistent named volume is retained when the container is recreated.

Rollback is the same controlled operation: restore the prior `ROBOT_IMAGE`
value, validate, and run `docker-update` again. Do not start the old and new
containers concurrently with the same Telegram token.

## Deployment record

Record:

- instance;
- date and operator;
- previous and target commit;
- target `requirements.txt` SHA-256;
- Python version;
- unit checksum;
- configuration schema changes without secret values;
- CI and CodeQL URLs;
- `doctor` result;
- live smoke-test result;
- rollback result when rehearsed.
