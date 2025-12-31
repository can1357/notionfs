# notionfs v2: Local-First Sync

## Architecture

```
Local Filesystem                    Sync Engine                     Notion API
─────────────────                   ───────────                     ──────────
~/.notionfs/workspaces/
  <workspace>/
    .state.db ◄──────────────────── SyncState (SQLite) ───────────► api_client.py
    pages/
      Meeting Notes.md ◄──────────► FileWatcher ──────► SyncManager ──► Notion
      Projects/
        _index.md
        Project Alpha.md
        Project Beta.md
```

## What We Keep

| Module | Lines | Keep? | Notes |
|--------|-------|-------|-------|
| `converter/blocks_to_md.py` | 340 | ✅ Yes | Core value — Notion→Markdown |
| `converter/md_to_blocks.py` | 476 | ✅ Yes | Core value — Markdown→Notion |
| `converter/diff.py` | 405 | ✅ Yes | Block diffing for minimal API calls |
| `converter/properties.py` | 310 | ✅ Yes | Frontmatter↔Properties |
| `converter/frontmatter.py` | 51 | ✅ Yes | YAML parsing |
| `converter/comments.py` | 126 | ✅ Yes | Comment handling |
| `notion/api_client.py` | 608 | ✅ Yes | Add global rate limiter, otherwise solid |
| `notion/types.py` | 47 | ✅ Yes | Type definitions |
| `config.py` | 74 | ✅ Yes | Config handling |
| **Total kept** | **~2400** | | |

## What We Throw Away

| Module | Lines | Why |
|--------|-------|-----|
| `fs/operations.py` | 2078 | FUSE layer — the source of all pain |
| `fs/attrs.py` | 57 | FUSE-specific |
| `fs/inode.py` | 97 | Inode management — no longer needed |
| `fs/tempfiles.py` | 120 | Temp file detection for atomic writes |
| `cache/db.py` | 635 | Wrong schema — rewrite for sync state |
| `cache/poller.py` | 74 | Stub that does nothing |
| `cache/worker.py` | 122 | FUSE background worker |
| `sync/manager.py` | 629 | Too coupled to FUSE model |
| `cli.py` | 863 | Rewrite for new commands |
| **Total thrown** | **~4700** | |

## New Modules

### `sync/state.py` — Sync State Database

```python
@dataclass
class SyncEntry:
    path: Path                    # Local relative path (primary key)
    notion_id: str                # Notion page/database ID
    notion_url: str               # Full Notion URL
    notion_parent_id: str | None  # Parent for tree structure
    is_directory: bool            # Page with children or database
    local_hash: str | None        # SHA256 of local file content at last sync
    remote_hash: str | None       # SHA256 of remote content at last sync
    remote_mtime: datetime | None # Notion's last_edited_time at last sync
    sync_status: SyncStatus       # clean | local_modified | remote_modified | conflict | deleted_local | deleted_remote

class SyncState:
    """SQLite-backed sync state."""
    
    def __init__(self, db_path: Path): ...
    def get_entry(self, path: Path) -> SyncEntry | None: ...
    def get_entry_by_notion_id(self, notion_id: str) -> SyncEntry | None: ...
    def set_entry(self, entry: SyncEntry) -> None: ...
    def get_all_entries(self) -> list[SyncEntry]: ...
    def get_modified(self) -> list[SyncEntry]: ...
    def get_conflicts(self) -> list[SyncEntry]: ...
    def delete_entry(self, path: Path) -> None: ...
```

### `sync/engine.py` — Sync Logic

```python
class SyncEngine:
    def __init__(self, workspace: Path, notion: NotionAPIClient, state: SyncState): ...
    
    async def pull(self, force: bool = False) -> PullResult:
        """Fetch remote changes, write to local files."""
        # 1. Fetch page tree from Notion
        # 2. Compare remote_mtime with state
        # 3. For each changed page:
        #    - If local also modified → mark conflict
        #    - Else → fetch content, write file, update state
    
    async def push(self, force: bool = False) -> PushResult:
        """Push local changes to Notion."""
        # 1. Scan local files, compute hashes
        # 2. Compare with state.local_hash
        # 3. For each changed file:
        #    - If remote also modified → mark conflict (unless force)
        #    - Else → parse markdown, diff sync to Notion, update state
    
    async def sync(self) -> SyncResult:
        """Bidirectional sync: pull then push."""
        pull_result = await self.pull()
        push_result = await self.push()
        return SyncResult(pull_result, push_result)
    
    def status(self) -> StatusReport:
        """Show pending changes without syncing."""
    
    def resolve_conflict(self, path: Path, resolution: Resolution) -> None:
        """Resolve conflict: keep_local | keep_remote | keep_both."""
```

### `sync/watcher.py` — File Watcher Daemon

```python
class SyncWatcher:
    """Background daemon that watches for changes and auto-syncs."""
    
    def __init__(self, engine: SyncEngine, debounce_seconds: float = 2.0): ...
    
    async def run(self) -> None:
        """Watch local files + poll remote, sync on changes."""
        async with trio.open_nursery() as nursery:
            nursery.start_soon(self._watch_local)   # inotify/fswatch
            nursery.start_soon(self._poll_remote)   # Periodic remote check
            nursery.start_soon(self._process_queue) # Debounced sync
```

### `cli.py` — New Commands

```bash
# Initialize workspace from Notion page/database
notionfs init https://notion.so/... [--path ./my-workspace]

# Pull remote changes
notionfs pull [--force]  # --force overwrites local conflicts

# Push local changes  
notionfs push [--force]  # --force overwrites remote conflicts

# Bidirectional sync
notionfs sync

# Show status
notionfs status
# Output:
#   Modified locally:  3 files
#   Modified remotely: 1 file
#   Conflicts:         1 file
#   
#   Local changes:
#     M  pages/Meeting Notes.md
#     M  pages/Projects/Alpha.md
#     A  pages/New Page.md
#   
#   Conflicts:
#     C  pages/Shared Doc.md (both modified)

# Resolve conflicts
notionfs resolve pages/Shared\ Doc.md --keep-local
notionfs resolve pages/Shared\ Doc.md --keep-remote
notionfs resolve pages/Shared\ Doc.md --keep-both  # Creates .conflict copy

# Watch mode (daemon)
notionfs watch [--interval 30]

# List configured workspaces
notionfs list
```

## Directory Structure

```
~/.notionfs/
  config.toml                 # Global config (token, defaults)

./my-workspace/               # Or any directory
  .notionfs/
    config.toml               # Workspace config (root notion ID, settings)
    state.db                  # Sync state database
  
  Meeting Notes.md            # Leaf page → file
  Projects/                   # Page with children → directory
    _index.md                 # Parent page content
    Project Alpha.md          # Child page
    Project Beta.md
  Tasks Database/             # Database → directory
    _schema.yaml              # Database schema (properties, views)
    Task 1.md                 # Database entries
    Task 2.md
```

## File Format

```markdown
---
# Database entry properties only (pages have no frontmatter unless in a database)
status: In Progress
priority: High
due_date: 2024-02-01
---

# Meeting Notes

Content here...
```

**Metadata tracking:**
- `notion_id`, `notion_url` → state.db (keyed by path)
- `created_time` → file ctime (set on first sync)
- `last_edited_time` → file mtime (updated on sync)
- `title` → derived from filename (sans `.md`)

**Leaf pages:** No frontmatter at all, just content.
**Database entries:** Frontmatter contains database properties only.

## Sync Algorithm

### Pull

```
for each remote_page in fetch_tree(root_id):
    entry = state.get_entry_by_notion_id(remote_page.id)
    
    if entry is None:
        # New remote page
        write_file(remote_page)
        state.set_entry(new_entry(status=clean))
    
    elif remote_page.last_edited_time > entry.remote_mtime:
        # Remote changed
        if entry.local_hash != hash(read_file(entry.path)):
            # Local also changed → conflict
            entry.sync_status = conflict
        else:
            # Only remote changed → update local
            write_file(remote_page)
            entry.sync_status = clean
            entry.remote_mtime = remote_page.last_edited_time
        
        state.set_entry(entry)

# Handle deletions
for entry in state.get_all_entries():
    if entry.notion_id not in remote_ids:
        if entry.local_hash == hash(read_file(entry.path)):
            # Clean local file, remote deleted → delete local
            delete_file(entry.path)
            state.delete_entry(entry.path)
        else:
            # Local modified, remote deleted → conflict
            entry.sync_status = deleted_remote
            state.set_entry(entry)
```

### Push

```
for each local_file in scan_workspace():
    entry = state.get_entry(local_file.path)
    current_hash = hash(read_file(local_file.path))
    
    if entry is None:
        # New local file → create in Notion
        notion_id = create_page(local_file)
        state.set_entry(new_entry(notion_id, status=clean))
    
    elif current_hash != entry.local_hash:
        # Local changed
        if needs_remote_check and remote_changed(entry):
            entry.sync_status = conflict
        else:
            # Push to Notion
            update_page(entry.notion_id, local_file)
            entry.local_hash = current_hash
            entry.sync_status = clean
        
        state.set_entry(entry)

# Handle deletions
for entry in state.get_all_entries():
    if not exists(entry.path):
        if entry.remote_hash == last_known_remote_hash:
            # Local deleted, remote unchanged → delete remote
            delete_page(entry.notion_id)
            state.delete_entry(entry.path)
        else:
            # Local deleted, remote changed → conflict
            entry.sync_status = deleted_local
            state.set_entry(entry)
```

## Rate Limiting

```python
class NotionAPIClient:
    def __init__(self, token: str, max_concurrent: int = 3):
        self._limiter = trio.CapacityLimiter(max_concurrent)
        self._last_request = 0.0
        self._min_interval = 0.34  # ~3 req/sec baseline
    
    async def _call(self, func, *args, **kwargs):
        async with self._limiter:
            # Minimum spacing between requests
            elapsed = time.monotonic() - self._last_request
            if elapsed < self._min_interval:
                await trio.sleep(self._min_interval - elapsed)
            
            self._last_request = time.monotonic()
            return await self._call_with_retry(func, *args, **kwargs)
```

## Migration Path

1. Delete `fs/`, `cache/`, `sync/manager.py`
2. Keep `converter/`, `notion/`, `config.py`
3. Add `sync/state.py`, `sync/engine.py`, `sync/watcher.py`
4. Rewrite `cli.py` with new commands
5. Update tests (converter tests stay, fs tests go)

## Benefits

- **Greppable**: Real files on disk, `rg` works instantly
- **Offline**: Edit without network, sync when ready
- **Atomic**: Sync is explicit, no half-written states
- **Debuggable**: `notionfs status` shows exactly what will happen
- **Conflict-aware**: Explicit conflict resolution, not silent overwrites
- **Editor-agnostic**: Any editor, any tool, real filesystem
