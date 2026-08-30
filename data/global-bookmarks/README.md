# Global bookmark catalogue sources

`topics.json` is the canonical, English topic-metadata source. Topic ids are
stable slugs and become localization constants named `bookmark_topics.<id>`.
The generator adds the English values to `ENGLISH_BOOKMARK_TOPIC_MESSAGES`.
Translated catalogues may omit a newly accepted key; the Mini App then uses
its existing English fallback until a reviewed translation is added later.
Do not copy the English text into untranslated language catalogues.

`tag-verse.csv` is the canonical topic-to-verse association source. References
use the translation-independent numeric form `book chapter:verse`.

Regenerate and verify the committed browser files from the repository root:

```bash
node scripts/generate_global_bookmarks.mjs
node scripts/generate_global_bookmarks.mjs --check
```

## Accepted contribution bundle

The moderation service exports only resolved canonical changes. Its JSON is
privacy-safe: it contains no Telegram identity, timestamp, note, or submitted
verse text. The exact version 1 structure is:

```json
{
  "schema_version": 1,
  "topics": [
    {
      "id": "prayer-and-fasting",
      "name": "Prayer and Fasting",
      "color": "#93c5fd",
      "aliases": []
    }
  ],
  "associations": {
    "add": [
      { "topic_id": "prayer-and-fasting", "book": 40, "chapter": 6, "verse": 16 }
    ],
    "remove": []
  }
}
```

`topics` contains new canonical definitions or existing definitions whose
reviewed aliases are being extended. Existing ids, English names, and colors
cannot be silently changed by an import. Associations may refer to an existing
repository topic without repeating its metadata in `topics`.

The id and resulting `bookmark_topics.<id>` key are permanent. If maintainers
later correct an established English name manually, they must retain that id
and add the previous English wording as an alias so stored mappings and future
translations remain valid.

Version 1 contribution bundles cannot delete a repository topic. Once a topic
has appeared in any live catalogue revision, moderation treats its ID, English
definition, and existence as permanent; `topic_delete` may only cancel a topic
before its first live publication. Supporting true deletion later requires a
new versioned schema with an explicit, provenance-aware topic tombstone so an
older unmerged contribution branch cannot resurrect the topic.

Validate without changing the worktree, then import:

```bash
node scripts/import_contribution_bundle.mjs --check /path/to/bundle.json
node scripts/import_contribution_bundle.mjs /path/to/bundle.json
```

The importer rejects unknown fields (including accidental private attribution),
unsafe or non-Latin topic text, unstable slugs, invalid colors, conflicting
operations, duplicate or invalid coordinates, empty topics, and catalogue
limits. It updates both sources and generated modules. Re-importing the same
bundle is a byte-for-byte no-op and does not advance `catalog_version`.
