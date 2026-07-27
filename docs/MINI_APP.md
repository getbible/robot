# Telegram Mini App deployment

The GetBible Mini App is part of the same isolated robot instance. It gives
search, filtering, Bible navigation, multi-selection, and review a contained
mobile interface while direct commands such as `/bible John 3:16` retain their
fast native path.

The Mini App is independent of Telegram update delivery. A production instance
may use polling and still serve the Mini App, or it may use a separate webhook
listener. The three loopback services have different purposes and must not
share ports:

| Listener | Default | Public exposure |
|---|---:|---|
| Health/readiness/metrics | `127.0.0.1:8081` | Never expose directly |
| Telegram webhook | `127.0.0.1:9001` | Exact private webhook path only |
| Telegram Mini App | `127.0.0.1:9201` | Mini App URL prefix through HTTPS |

## Security boundary

A Telegram Mini App is a web application, so its public HTTPS shell can be
requested by an ordinary browser. It is not technically possible to make that
HTML URL reachable by Telegram WebViews while making it unreachable by every
other browser: Mini App traffic comes from users' devices, not a stable
Telegram server IP range.

GetBible therefore makes unauthenticated browser access inert:

- the bot token remains server-side and is never placed in HTML, JavaScript,
  URLs, logs, or browser storage;
- every data or action API requires fresh, signature-verified Telegram
  `initData`;
- the server validates the signed user identity and authentication timestamp;
- a short-lived, user-bound launch token ties the browser session to the bot
  workflow and originating chat context; a generic Main Mini App launch is
  deliberately restricted to the authenticated user's private bot chat;
- authenticated sessions have a short absolute lifetime that API activity
  cannot extend indefinitely;
- if Telegram recreates a WebView and loses its browser session record, a fresh
  signed `initData` value may rebind only to the still-active session carrying
  the same opaque launch token, user, chat, and chat instance; the absolute
  session lifetime is not extended;
- expired, replayed, missing, mismatched, or malformed authorization fails
  closed before Scripture lookup or posting;
- submitted verse text is never authoritative—the server resolves selected
  identifiers again before posting;
- state, selection, and final output-message bounds are enforced server-side;
- final posts are completely resolved and rendered before their first Telegram
  send, and known partial sends are rolled back best-effort;
- Telegram theme values are presentation hints, never authorization.

Do not add an IP allowlist for Telegram clients, trust `Referer` or
`User-Agent`, expose the bot token to the browser, or treat an obscure URL as
authentication.

## Configure a new instance

During `sudo ./setup.sh install`, answer yes to the Mini App question and
provide a public URL such as:

```text
https://bot.example.com/getbible/production
```

Create the public DNS `A` and/or `AAAA` record before running setup, and direct
public TCP ports `80` and `443` to this host. The manager verifies that the
hostname resolves publicly and, when Caddy is absent, installs Caddy through
its official signed APT repository on Debian/Ubuntu or official COPR repository
on DNF hosts. It assigns a unique loopback port beginning at `9201` and
configures Caddy automatic HTTPS. Polling remains the recommended Telegram
delivery mode and does not affect the Mini App HTTPS listener.

Do not create a public cloud-firewall rule for `9201` or any other assigned
Mini App port. The Robot binds that listener to `127.0.0.1`; Caddy is the only
public entry point and proxies the configured HTTPS hostname/path to loopback.

Setup refuses to continue when DNS is absent/private, Caddy is inactive while
another process owns port `80` or `443`, an existing Caddy configuration
conflicts, or the final public certificate/route/content probe fails. It never
uses a downloaded shell installer or a curl-pipe package installation.

## Manage an installed instance

Use the transactional manager instead of editing listener settings directly:

```bash
sudo getbible-robot miniapp production
sudo getbible-robot status production
sudo getbible-robot doctor production
```

`miniapp` backs up the environment and Caddy files, validates DNS, the public
HTTPS URL, loopback port, complete generated Caddy configuration, service
reload, local shell, public certificate, route, and response content. Any
failure restores the environment and both Caddy files byte-for-byte, reloads
the prior Caddy configuration, and restores the prior robot service state.
Disabling removes the public route while retaining its URL and reserved port
for a later safe re-enable.

## Setup-managed Caddy

The supported production path uses the host's `caddy.service`. The manager
adds one marked import to `/etc/caddy/Caddyfile` and writes deterministic,
non-secret routes to:

```text
/etc/caddy/getbible-robot.caddy
```

Do not edit the marked import or generated route file. Existing unrelated
Caddyfile content is preserved. Every candidate is checked with `caddy
validate` before a zero-downtime reload. Duplicate and path-overlapping routes
are rejected, and multiple Robot instances receive separate reserved loopback
ports. Caddy is retained on uninstall because it may serve other Robot
instances or unrelated sites; only the selected instance's route is removed.

The application continues to emit its own restrictive security and cache
headers. Caddy terminates public TLS and forwards the complete path prefix
without weakening the loopback or application-authentication boundary.

## Telegram configuration

At startup the robot can register the configured Mini App URL in bot-owned Bot
API controls. BotFather-only product settings remain an operator step.
Configure the bot's Main Mini App in `@BotFather` when profile or group-context
launches are required, using exactly the same reviewed HTTPS URL.
`setup.sh` cannot configure or verify that BotFather-only setting. The robot
uses the Main Mini App `startapp` deep link and does not require a separately
named Mini App.

Do not create a second bot or token for the Mini App. It uses the matching
instance token only on the server to validate Telegram signatures and post the
final server-resolved selection.

## Configuration reference

| Variable | Default | Production rule |
|---|---:|---|
| `MINI_APP_ENABLED` | `false` | Managed through `getbible-robot miniapp` |
| `MINI_APP_PUBLIC_URL` | empty | Absolute HTTPS URL; no credentials, query, or fragment |
| `MINI_APP_LISTEN` | `127.0.0.1` | Manager-owned; never bind publicly |
| `MINI_APP_PORT` | `9201` | Manager-owned, reserved per configured instance, and different from health/webhook ports |
| `MINI_APP_INIT_DATA_MAX_AGE_SECONDS` | `300` | `30`–`900`; maximum Telegram authentication age |
| `MINI_APP_LAUNCH_TTL_SECONDS` | `300` | `30`–`900`; lifetime of the user-bound launch token |
| `MINI_APP_SESSION_TTL_SECONDS` | `900` | Absolute server session lifetime |
| `MINI_APP_SESSION_LIMIT` | `2000` | Maximum bounded active Mini App sessions |
| `MINI_APP_MAX_SELECTIONS` | `100` | Maximum selected verse items before final normalization |

Keep the authentication and launch windows short. Lengthening them increases
the useful replay window and is not a remedy for incorrect clocks. Maintain
accurate host time with a trusted time-synchronization service.

## Branding and look and feel

The Mini App's presentation is contained in `miniapp/`; changing its branding
does not require changes to Telegram authentication, Scripture lookup, or
posting code.

| Element | Source | Notes |
|---|---|---|
| Upright Bible | `miniapp/assets/getbible-upright.png` | Used by the opening gate, protected/expired gate, top bar, and home hero |
| Browser icon | `miniapp/assets/favicon.png` | PNG favicon declared in `miniapp/index.html` |
| Hero background | `miniapp/assets/ocean-light-hero.webp` | Optimized WebP referenced by `miniapp/styles.css` |
| Wordmark and tagline | `miniapp/index.html` | Keep `getBible.Life` and “The words of eternal life” as real text |
| Colors and themes | `miniapp/styles.css` | Light tokens are in `:root`; dark overrides are in `:root[data-theme="dark"]` |
| Component sizing and layout | `miniapp/styles.css` | Brand, gate, hero, navigation, cards, and responsive rules live here |

To replace the Bible icon, use a transparent PNG with a clean, uncropped
boundary and preserve the `getbible-upright.png` filename. The current source
has a portrait aspect ratio; CSS uses contained sizing at each placement, so do
not add baked-in padding or crop the artwork. If the filename changes, update
all four references in `miniapp/index.html` and the branding assertion in
`miniapp/tests/static.test.mjs`.

Theme colors should be changed through the custom properties at the top of
`miniapp/styles.css`. Preserve the `--tg-theme-*` fallbacks so Telegram light
and dark themes remain authoritative for page, surface, text, button, and
separator colors. Use the `--brand*` variables for GetBible-specific accents
instead of hard-coding the same color across individual components.

After any presentation change, run:

```bash
cd miniapp
npm test
```

Then verify the opening gate, home hero, top bar, protected/expired state,
search keyboard behavior, and light/dark themes on a narrow phone viewport.
Deploy the reviewed source through the normal instance upgrade command; do not
edit files inside `/opt/getbible-robot/<instance>/app` by hand.

## Verification

After starting or upgrading:

```bash
sudo getbible-robot status production
sudo getbible-robot doctor production
sudo ss -ltnp | grep ':9201'
sudo systemctl status caddy.service --no-pager
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo getbible-robot logs production 200
```

Verify that the Mini App port appears only on `127.0.0.1`. Then test through a
private conversation with the bot:

1. open the Mini App from `/search grace`;
2. confirm Telegram light and dark themes both remain legible;
3. filter and page without creating chat messages;
4. select several complete verse cards, review them, and post once;
5. confirm the server posts only resolved Scripture, in the originating chat;
6. submit a search with the phone keyboard's Search key and confirm the
   keyboard closes before the results appear;
7. close and reopen the same launch before its absolute session timeout and
   confirm the active selection is safely recovered;
8. after posting in a group, confirm both the "Only visible to GetBibleBot"
   command and the "Only visible to you" launch response are removed;
9. retry a genuinely expired launch and confirm it fails closed, explains that
   `/bible` or `/search` must be sent again, and offers a close action instead
   of a reload loop;
10. open the public URL in an ordinary browser and confirm no data or action API
   is available.

For group rollout, repeat through the configured Main Mini App/deep-link path
and confirm the selection interface remains private while only the final
Scripture is posted to the intended chat and topic.

## Common failures

- **Ordinary browser shows the shell:** expected; confirm protected APIs remain
  inaccessible. The shell is not the security boundary.
- **Authorization rejected:** close the expired Mini App and send `/bible` or
  `/search` again. If a new launch is also rejected, verify host time and
  confirm the instance token matches the bot that opened the app.
- **DNS preflight fails:** create/fix the public `A`/`AAAA` record before
  enabling the Mini App.
- **Caddy validation or reload fails:** resolve the reported conflict in the
  existing Caddyfile; the manager has already restored the previous files.
- **502 or connection refused:** run `doctor` and confirm both the Robot and
  Caddy services are active; do not bind the Robot publicly.
- **Mini App works privately but not from a group:** verify the Main Mini App
  URL and direct-link setting in BotFather.
- **`doctor` reports no listener:** check configuration validation, port
  collision, service logs, and `systemctl status`.
- **Theme looks wrong:** use Telegram theme parameters and CSS variables; do
  not hard-code a theme based on device preference alone.
