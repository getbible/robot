# Telegram delivery: polling and webhooks

GetBible Robot supports exactly one Telegram delivery mode per instance:

- **polling**: the robot asks Telegram for updates. It needs outbound HTTPS only
  and is the simplest mode for testing or a private server;
- **webhook**: Telegram sends each update as an HTTPS `POST` to a public URL.
  The robot listens only on loopback behind a reverse proxy.

Telegram does not provide a WebSocket stream for Bot API updates. Long polling
and webhooks are mutually exclusive for one bot token. Starting polling removes
an existing webhook; starting webhook mode registers the configured webhook.

Telegram Mini App HTTPS serving is independent of this choice. A polling
instance may—and normally will—serve the Mini App through a separate loopback
port and public HTTPS route. Do not forward the Mini App prefix to the Telegram
webhook port. See [Mini App deployment](MINI_APP.md).

## What BotFather does

Use `@BotFather` to create the bot and obtain or revoke its token:

1. open a private chat with `@BotFather`;
2. run `/newbot` and choose the display name and unique username;
3. copy the token into the hidden `setup.sh` prompt;
4. configure `/setprivacy` if group behavior requires it;
5. optionally use `/setuserpic` for the profile image.

Do not paste the token into a shell command, issue, chat transcript, or Git
file. The robot uses the Bot API—not BotFather—to synchronize its command menu,
display name, description, and short description at startup.

## Install in polling mode

Choose `polling` in the installation questionnaire. No inbound firewall rule,
certificate, DNS record, or public listener is required:

```bash
sudo ./setup.sh install
sudo getbible-robot doctor production
```

Only one active process may poll with a particular token. If Telegram reports a
polling conflict, the robot logs one critical explanation and exits with status
75. The systemd unit deliberately does not restart that status, preventing two
local or remote pollers from continually fighting each other.

## Install in webhook mode

Prepare:

- a DNS name pointing to the server;
- a valid public TLS certificate;
- inbound TCP on public port 443 (Telegram also supports 80, 88, and 8443);
- a reverse proxy that forwards only the private webhook path to the selected
  loopback port.

During `sudo ./setup.sh install`, choose `webhook` and provide a URL such as:

```text
https://bot.example.com/telegram/production
```

The manager generates a high-entropy webhook secret. Telegram includes it in
the `X-Telegram-Bot-Api-Secret-Token` request header, and the application
rejects requests that do not match.

The loopback listener remains private:

```text
Public: https://bot.example.com/telegram/production
Local:  http://127.0.0.1:9001/telegram/production
```

Do not open port 9001 in the host or cloud firewall.

## Optional webhook proxy

Polling is the standard production choice for this deployment and needs no
webhook proxy. If an operator deliberately selects webhook delivery, use Caddy
on a dedicated webhook hostname and replace the hostname, path, and port with
the values printed by setup:

```caddyfile
telegram-bot.example.com {
    reverse_proxy /telegram/production 127.0.0.1:9001
}
```

The setup-managed Mini App Caddy routes are independent. Do not edit their
generated file, forward a webhook to the Mini App port, or weaken either
listener's loopback binding.

## Change delivery after installation

The manager performs a validated, rollback-capable switch:

```bash
sudo getbible-robot delivery production
```

For webhook mode, it asks for the public HTTPS URL, local loopback port,
optional fixed public IP, and proxy readiness. For polling mode, the Telegram
library removes the webhook before polling starts. If the selected active mode
does not become ready, the manager restores the previous environment and
restarts the prior mode.

Verify the local and Telegram state:

```bash
sudo getbible-robot status production
sudo getbible-robot doctor production
sudo getbible-robot logs production 100
```

`doctor` calls Telegram's `getWebhookInfo` without printing the token. In
polling mode the registered webhook URL must be empty. In webhook mode it must
exactly match the configured public URL.

## Edit help and profile content

The installer creates restricted per-instance welcome and help files:

```text
/etc/getbible-robot/production.welcome.txt
/etc/getbible-robot/production.help.txt
```

Edit them through the manager, which makes a backup, restores secure ownership,
validates the result, and offers to restart:

```bash
sudo EDITOR=nano getbible-robot content production welcome
sudo EDITOR=nano getbible-robot content production help
```

Edit the Telegram profile metadata with:

```bash
sudo EDITOR=nano getbible-robot config production
```

Change `BOT_NAME`, `BOT_DESCRIPTION`, or `BOT_SHORT_DESCRIPTION`, then accept
the restart. The command menu and profile text synchronize through the Bot API
during startup.

## Common webhook failures

- **Webhook URL mismatch**: rerun `delivery`; do not call `setWebhook` manually
  with a different URL.
- **Connection refused**: confirm the instance is active and the reverse proxy
  targets the printed loopback port and exact path.
- **TLS error**: use a valid full-chain certificate trusted by Telegram.
- **404**: preserve the path when proxying; do not strip
  `/telegram/<instance>`.
- **Updates stop after switching to polling**: run `doctor`; polling must show
  no registered webhook.
- **Pending updates grow**: inspect `getWebhookInfo`, proxy logs, the robot log,
  certificate validity, DNS, firewall rules, and response latency.

Keep webhook request handling fast. The event loop receives the update and
offloads synchronous Scripture/search work into bounded executors; search has
its own smaller concurrency pool so expensive corpus work cannot occupy every
direct-reference worker.
