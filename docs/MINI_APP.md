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
  workflow and originating chat context;
- expired, replayed, missing, mismatched, or malformed authorization fails
  closed before Scripture lookup or posting;
- submitted verse text is never authoritative—the server resolves selected
  identifiers again before posting;
- state and selection bounds are enforced server-side;
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

The manager assigns a unique loopback port beginning at `9201`. Configure the
HTTPS route before allowing setup to start the instance. Polling remains a
valid and recommended Telegram delivery mode; it does not remove the need for
the Mini App HTTPS route.

## Configure an existing instance

Use the transactional manager instead of editing listener settings directly:

```bash
sudo getbible-robot miniapp production
sudo getbible-robot status production
sudo getbible-robot doctor production
```

`miniapp` backs up the environment, validates the public HTTPS URL and
loopback port, prevents a webhook/Mini App port collision, restarts only the
selected instance, and restores the previous configuration if readiness
fails. Disabling the Mini App retains its URL and port for a later safe
re-enable.

## Reverse-proxy examples

The examples preserve the complete public path prefix. Replace the host, path,
and assigned port with the values printed by setup.

### Caddy

```caddyfile
bot.example.com {
    @getbible path /getbible/production*
    reverse_proxy @getbible 127.0.0.1:9201
}
```

### Nginx

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;

    client_max_body_size 64k;

    location = /getbible/production {
        return 308 /getbible/production/;
    }

    location ^~ /getbible/production/ {
        proxy_pass http://127.0.0.1:9201;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Request-ID $request_id;
        proxy_connect_timeout 3s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
    }
}
```

### Traefik

Route the prefix without `StripPrefix`:

```text
Host(`bot.example.com`) && PathPrefix(`/getbible/production`)
```

Forward it to:

```text
http://127.0.0.1:9201
```

When Traefik runs in a container, container loopback is not host loopback. On
Linux, run only the Traefik proxy with host networking so its
`127.0.0.1:9201` target is the host's loopback listener. Use Traefik's file
provider when practical; it does not require mounting the Docker control
socket. A bridge-network `host-gateway` address cannot reach a service bound
only to host loopback.

Do not solve that mismatch by changing `MINI_APP_LISTEN` to `0.0.0.0`.
A fully containerized Robot is a separate deployment profile: place Traefik
and the Robot on a private internal network, publish ports only from Traefik,
use container secrets and a read-only/rootless Robot, and explicitly validate
the non-loopback container bind. The supplied manager intentionally implements
the narrower host-systemd/loopback boundary; it does not silently weaken that
boundary when Docker is detected.

The application emits its own restrictive security headers. A reverse proxy
must not weaken or overwrite its content-security policy, frame policy,
referrer policy, MIME-sniffing protection, or cache controls. Apply edge rate
limits and a small request-body limit, but keep application authentication and
server-side bounds enabled.

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
| `MINI_APP_ENABLED` | `false` | Enable only after HTTPS routing is ready |
| `MINI_APP_PUBLIC_URL` | empty | Absolute HTTPS URL; no credentials, query, or fragment |
| `MINI_APP_LISTEN` | `127.0.0.1` | Manager-owned; never bind publicly |
| `MINI_APP_PORT` | `9201` | Unique per instance and different from the webhook port |
| `MINI_APP_INIT_DATA_MAX_AGE_SECONDS` | `300` | `30`–`900`; maximum Telegram authentication age |
| `MINI_APP_LAUNCH_TTL_SECONDS` | `300` | `30`–`900`; lifetime of the user-bound launch token |
| `MINI_APP_SESSION_TTL_SECONDS` | `900` | Idle server session lifetime |
| `MINI_APP_SESSION_LIMIT` | `2000` | Maximum bounded active Mini App sessions |
| `MINI_APP_MAX_SELECTIONS` | `100` | Maximum selected verse items before final normalization |

Keep the authentication and launch windows short. Lengthening them increases
the useful replay window and is not a remedy for incorrect clocks. Maintain
accurate host time with a trusted time-synchronization service.

## Verification

After starting or upgrading:

```bash
sudo getbible-robot status production
sudo getbible-robot doctor production
sudo ss -ltnp | grep ':9201'
sudo getbible-robot logs production 200
```

Verify that the Mini App port appears only on `127.0.0.1`. Then test through a
private conversation with the bot:

1. open the Mini App from `/search grace`;
2. confirm Telegram light and dark themes both remain legible;
3. filter and page without creating chat messages;
4. select several complete verse cards, review them, and post once;
5. confirm the server posts only resolved Scripture, in the originating chat;
6. retry an expired launch and confirm it fails closed and asks for a fresh
   launch;
7. open the public URL in an ordinary browser and confirm no data or action API
   is available.

For group rollout, repeat through the configured Main Mini App/deep-link path
and confirm the selection interface remains private while only the final
Scripture is posted to the intended chat and topic.

## Common failures

- **Ordinary browser shows the shell:** expected; confirm protected APIs remain
  inaccessible. The shell is not the security boundary.
- **Authorization rejected:** launch again from the bot, verify host time, and
  confirm the instance token matches the bot that opened the app.
- **404 for scripts or API calls:** preserve the entire configured prefix in
  the reverse proxy.
- **502 or connection refused:** confirm the service is active and the proxy
  targets the assigned loopback port.
- **Mini App works privately but not from a group:** verify the Main Mini App
  URL and direct-link setting in BotFather.
- **`doctor` reports no listener:** check configuration validation, port
  collision, service logs, and `systemctl status`.
- **Theme looks wrong:** use Telegram theme parameters and CSS variables; do
  not hard-code a theme based on device preference alone.
