# Security policy

## Supported code

Security fixes are applied to the `master` branch. Deployments should use an exact reviewed commit and the checked-in hashed dependency lock.

## Reporting a vulnerability

Please use GitHub's private security-advisory reporting for `getbible/robot`, or contact `getbible@vdm.io`. Do not open a public issue containing bot tokens, private chat data, exploit payloads against a live service, or infrastructure details.

Include the affected commit, reproduction steps using a local test bot where possible, impact, and any proposed mitigation.

## Secrets

A Telegram token must never be committed or passed on a command line. The setup manager stores each token in `/etc/getbible-robot/<instance>.env` as `root:root` mode `0600`. If a token is exposed, revoke it immediately through `@BotFather`, replace it, and inspect deployment, logs, process history, and Git history.

## Data handling

Metadata audit mode does not persist user messages, references, search terms,
names, usernames, profiles, or chat IDs. The bounded per-instance preference
database stores only Telegram user ID, selected translation code, and update
time so a user's default survives restarts. Content audit mode is an explicit
operator choice that additionally stores normalized search terms and final
references in the restricted per-instance JSONL log; it still excludes tokens,
identities, verse bodies, and repository payloads. Deployers are responsible
for access, retention, backup, and deletion policy for the preference database
and for any content-audit log. Telegram and the configured GetBible API remain
independent external services.
