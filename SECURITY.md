# Security policy

## Supported code

Security fixes are applied to the `master` branch. Deployments should use an exact reviewed commit and the checked-in hashed dependency lock.

## Reporting a vulnerability

Please use GitHub's private security-advisory reporting for `getbible/robot`, or contact `getbible@vdm.io`. Do not open a public issue containing bot tokens, private chat data, exploit payloads against a live service, or infrastructure details.

Include the affected commit, reproduction steps using a local test bot where possible, impact, and any proposed mitigation.

## Secrets

A Telegram token must never be committed. Store it in `/etc/getbible-robot.env` or an equivalent secret manager with permissions restricted to the service account. If a token is exposed, revoke it immediately through `@BotFather`, replace it, and inspect deployment and Git history.

## Data handling

The robot does not persist user messages, references, favorites, or profiles. Structured logs contain aggregate operational events and correlation IDs, not message content. Telegram and the configured GetBible API remain independent external services with their own data practices.
