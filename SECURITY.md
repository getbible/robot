# Security policy

## Supported code

Security fixes are applied to the `master` branch. Deployments should use an exact reviewed commit and the checked-in hashed dependency lock.

## Reporting a vulnerability

Please use GitHub's private security-advisory reporting for `getbible/robot`, or contact `getbible@vdm.io`. Do not open a public issue containing bot tokens, private chat data, exploit payloads against a live service, or infrastructure details.

Include the affected commit, reproduction steps using a local test bot where possible, impact, and any proposed mitigation.

## Secrets

A Telegram token must never be committed or passed on a command line. The setup manager stores each token in `/etc/getbible-robot/<instance>.env` as `root:root` mode `0600`. If a token is exposed, revoke it immediately through `@BotFather`, replace it, and inspect deployment, logs, process history, and Git history.

## Telegram Mini App boundary

The Mini App's HTML, styles, scripts, and established GetBible artwork are
public assets. They contain no bot token, API credential, chat identifier,
search query, Scripture payload, or authorization secret. Every data or action
API requires a fresh Telegram-signed `initData` payload, a short-lived
user-bound launch/session, exact same-origin requests, and server-side request
bounds. Launch and signed-data replays fail closed.

The browser never supplies authoritative verse text for posting. It selects
opaque server-held identifiers; the robot resolves and renders the resulting
references again on the server. The listener remains on loopback behind a
reviewed HTTPS reverse-proxy route. See
[Telegram Mini App deployment](docs/MINI_APP.md) for the complete production
contract.

## Data handling

Metadata audit mode does not persist user messages, references, search terms,
names, usernames, profiles, or chat IDs. The bounded per-instance preference
database stores only Telegram user ID, selected translation code, allow-listed
non-content search defaults, and update time so those choices survive restarts.
It does not retain queries, exclusions, basket contents, names, or profiles.
Content audit mode is an explicit operator choice that additionally stores
normalized search terms and final references in the restricted per-instance
JSONL log; it still excludes tokens, identities, verse bodies, and repository
payloads. Deployers are responsible for access, retention, backup, and deletion
policy for the preference database and for any content-audit log. Telegram and
the configured GetBible API remain independent external services.
