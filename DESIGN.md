# notionfs Design

Local-first Notion sync. Edit Notion pages as markdown files, sync when ready.

```
Local Filesystem                    Sync Engine                     Notion API
─────────────────                   ───────────                     ──────────
workspace/
  .notionfs/
    state.db ◄──────────────────── SyncState (SQLite) ────────────► api_client.py
  .git/ ◄──────────────────────── change detection
  Meeting Notes.md ◄─────────────► SyncEngine ─────────────────────► Notion
  Projects/
    _index.md
    Alpha.md
```

## Workspace Layout

```
my-workspace/
├── .notionfs/
│   ├── config.toml      # root_id, notion_url
│   └── state.db         # SQLite sync state (WAL mode)
├── .git/                # initialized on clone
├── Meeting Notes.md     # leaf page
└── Projects/            # page with children
    ├── _index.md        # parent page content
    └── Alpha.md         # child page
```

Global token lives in `~/.notionfs/config.toml` or `NOTION_TOKEN` env var.

Pages with children become directories. The parent's content goes in `_index.md`.
Database entries get YAML frontmatter for properties; regular pages have none.

## Sync State

SQLite tracks the mapping between local files and Notion pages:

```
path             notion_id                             remote_hash   remote_mtime          status
─────────────────────────────────────────────────────────────────────────────────────────────────
Projects/_index.md  a1b2c3d4-...                       sha256:...    2024-01-15T10:30:00   clean
Projects/Alpha.md   e5f6g7h8-...                       sha256:...    2024-01-15T11:00:00   conflict
```

Status values: `clean`, `local_modified`, `remote_modified`, `conflict`, `deleted_local`, `deleted_remote`.

## Sync Algorithm

**Pull** fetches the page tree, compares `last_edited_time` against stored `remote_mtime`:
- Remote changed, local clean → overwrite local file
- Remote changed, local also changed → mark conflict
- Remote deleted, local clean → delete local file
- Remote deleted, local modified → mark `deleted_remote` conflict

**Push** detects local changes (via git or hash comparison), then for each modified file:
- Local changed, remote clean → push to Notion via block diff
- Local changed, remote also changed → mark conflict (unless `--force`)
- Local deleted, remote clean → archive the Notion page
- Local deleted, remote modified → mark `deleted_local` conflict

Conflicts require explicit resolution: `--keep-local`, `--keep-remote`, or `--keep-both`.

## Git Integration

Clone initializes a git repo for efficient change detection. Configures local identity
to avoid dependence on global git config:

```
git init
git config user.name "notionfs"
git config user.email "notionfs@local"
```

Push and status use `git status --porcelain` to find modified files—O(changed) instead
of O(all files). After pull, changes auto-commit with message like `"notionfs pull"`.

If `.git` doesn't exist (removed, or workspace predates git integration), falls back to
comparing file hashes against `remote_hash` in state.db.

## CLI

```bash
notionfs clone <url> [--path ./workspace]   # init workspace from Notion page
notionfs pull [--force]                      # fetch remote changes
notionfs push [--force]                      # push local changes
notionfs sync                                # pull then push
notionfs status                              # show pending changes
notionfs resolve <path> --keep-local|--keep-remote|--keep-both
notionfs watch [--interval 30]               # continuous sync daemon
```

## Rate Limiting

Reactive only—no proactive throttling. On 429, respect `Retry-After` header with
exponential backoff. Notion's rate limits are per-integration, not per-request-type,
so a global approach works fine.

Block appends chunked to 100 blocks per request (API limit). Rich text segments
chunked at 2000 chars (API limit), split on word boundaries.

## Block Diffing

Push doesn't replace all blocks—it diffs. LCS-based algorithm matches blocks by
content hash, then:
- Matched blocks: update in place (preserves block ID, comments, etc.)
- Unmatched remote blocks: delete
- Unmatched local blocks: append

This minimizes API calls and preserves Notion-side state like comments and
resolved suggestions that aren't represented in markdown.
