# Direct contribution publication

The contributor submits once. The existing private contribution database keeps
applications, topic mappings, events, decisions, notifications and accepted
ledger revisions. Final maintainer acceptance saves that work first, then
attempts publication. A missing credential or failed HTTP request never undoes
acceptance. There is no new contributor consent or client storage format.

## Operator commands

For a native instance named `production`:

```bash
sudo getbible-robot contributions production
sudo getbible-robot contributions production tokens
sudo getbible-robot commit production
sudo getbible-robot contributions production status
```

The menu's **Accept approved changes and commit to the builder** action performs
both operations. **Commit previously accepted contributions / retry**, or the
`commit` command, never asks for those content decisions again. `tokens` prompts
only for missing credentials, without echo; press Enter to skip either. To
replace/remove a configured credential or choose another model, use the existing
`getbible-robot config production` editor. No service restart is needed for these
publisher-only native settings. Public API detection still runs in the bot.

For a container:

```bash
docker exec -it getbible-robot-production /app/setup.sh contributions production
docker exec getbible-robot-production /app/setup.sh contributions production commit
docker exec getbible-robot-production /app/setup.sh contributions production status
```

Set credentials in the private Compose environment and recreate the single
container, or edit the selected instance's environment file in multi-instance
mode. The same publisher works in both modes without Git, SSH, a builder
checkout or a separate publisher account. An optional privacy-safe bundle
export is still available; it is not required for API publication.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CONTRIBUTION_GITHUB_TOKEN` | empty | Fine-grained PAT or installation token with Contents read/write on only `getbible/v1_bookmark_builder` |
| `CONTRIBUTION_OPENAI_API_KEY` | empty | OpenAI API key; its presence enables new-topic translation |
| `CONTRIBUTION_TRANSLATION_MODEL` | `gpt-5.6-sol` | Responses API model supporting strict JSON-schema output |

Missing GitHub token: acceptance remains queued and no network publication or
translation is attempted. Add the token and run `commit` later. The publishing
identity must be permitted by repository rules to update the default branch.
Pull-request permission is not needed. Do not weaken branch rules silently;
configure the intended identity explicitly. A workflow-generated `GITHUB_TOKEN`
is not appropriate for the Robot's credential: use a PAT or installation token
so that the resulting push starts the builder's existing workflow.

Missing OpenAI key: commit the English topic and verse links, preserving all
locale files. Configured OpenAI key: all requested translations must validate
before the same commit is published. An invalid, exhausted or unavailable key
is an error, not permission to silently downgrade to English. Correct the key
and retry, or deliberately remove it to select English-only publication.

The model default follows OpenAI's documented flagship at implementation time,
not a claim that it is the best translator of every biblical term in every
language. It is configurable; deployment model access and language quality
must be evaluated for the project's catalogue. Provider documentation:
[models](https://developers.openai.com/api/docs/models),
[structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[GitHub references](https://docs.github.com/en/rest/git/refs).

## One atomic source commit

`scripts/contribution_publish.py` orchestrates the operation.
`modules/bookmark_sources.py` implements and validates the builder's data
contract; `modules/contribution_publication.py` owns transport, receipts and
translation. No downloaded Python, shell command or model-generated program is
executed. Only these source paths can change:

- `data/topics.json`: accepted English definitions and stable IDs;
- `data/links/<topic-id>.json`: sorted, deduplicated verse coordinates;
- `data/locales/<locale>.json`: new-topic translated names only.

The publisher discovers the builder's default branch, requiring `main` or
`master`, reads all source files at a fixed head, verifies blob hashes, and
validates the complete cross-file catalogue before writing. It creates a tree
based on that head and a single commit containing every changed file. Advancing
the existing branch is a non-force fast-forward. There is no contribution
branch, pull request or merge. A concurrent branch change causes a fresh read
and reapplication; unrelated colleague edits and existing translations survive.
No-op work does not generate a duplicate commit. Generated `v1/` files are never
written by Robot: the existing builder push workflow owns API generation.

Only accepted events are eligible; pending/deferred/rejected content is not
included. On first use, the existing cumulative accepted ledger imports work
from older deployments, including topics accepted locally but never submitted
upstream. Thereafter per-event receipts, rather than repeated cumulative
imports, prevent an old contribution from overwriting a later colleague edit.
An older deferred event accepted later is not skipped. Established topic IDs,
English names and colours are not replaced by this bundle protocol.

## Translation

Only topics absent from the fetched builder source are translated. The target
list is discovered from existing `data/locales/*.json` files. English remains
in `data/topics.json`; the builder generates English locale output. Adding
verses to an existing topic makes no OpenAI request, even if it lacks some
translations. Adding an OpenAI key later does not backfill previously published
English-only topics; that is a separate catalogue-editing operation.

The Responses API request contains the accepted English name, aliases, target
locale codes/language names, and a small public terminology sample. It carries
no contributor identity or private reviewer notes. The fixed prompt asks for
concise, natural Bible-topic names, without doctrinal additions or commentary,
and treats all supplied labels as data, not instructions. Strict JSON-schema
output requires exactly one string per requested locale. Application code
validates completion, exact keys, Unicode normalization and 1–120 character
labels; the model cannot choose filenames or issue repository operations.

Results are cached privately by topic content, language, model and prompt
version. Network retries and branch conflicts reuse successful translations.
Schema validity does not guarantee linguistic accuracy; trusted maintainers
can correct translations directly in the builder without future verse-only
contributions overwriting their work. No paid model call is made during tests.

## Upgrade, recovery and privacy

The native update manager adds missing optional settings and offers the two
missing credentials. Blank input, EOF, or a non-interactive update continues
without them; supplied settings and unrelated configuration are preserved.
The update itself does not publish anything. Configuration rollback remains
part of the existing deployment transaction. Existing legacy Git settings are
left in private files but ignored by the new normal publishing path.

The live schema-v5 database migrates in place with the existing idempotent
v6 migration. A synthetic fixture produced by the live revision
`6ec7081bcb4683011151437298891226539e3aa2` checks every original table/column/row
before and after two opens of the upgraded store. It then verifies that only
accepted data, not pending work, reaches the publisher.

Publication state is separate, adjacent to the configured contribution file:
`contributions.sqlite3.publication.sqlite3` for the default filename. It holds
private event receipts, one frozen pending publication, the last commit and
translation cache; a private lock prevents simultaneous publisher processes.
**Back up and restore both databases together**, using SQLite's backup API or
stopping writers. Never delete the publication journal to retry a failure:
that would make old accepted work eligible for initial import again.
The private per-instance state directory and these files survive upgrades.

A candidate commit ID is stored before the remote branch update. After a lost
HTTP acknowledgement, a retry checks whether that commit is already an ancestor
of the current head and records success without a duplicate. Transient HTTP
failures have bounded retries. A persistent failure remains visible in status;
run `commit` again after correcting it. There is no unattended retry daemon.

Publication credentials are server-side only. Native root-owned credentials
cross to the existing database-owning service account over stdin, not command
arguments or inherited environment. Container credentials stay scoped to their
own instance. Logs and remote commits contain no Telegram identity, private
moderation notes, keys or provider response bodies.

A successful source commit is not yet proof that the API build succeeded. The
existing Bookmarks API watcher marks accepted contributions live and queues
private contributor notices only when the public catalogue contains the change.
A failed build leaves the source commit and accepted records intact; fix the
reported builder error and rerun its workflow. Robot never force-resets history.
