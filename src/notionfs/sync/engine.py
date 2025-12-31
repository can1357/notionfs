"""Sync engine for local-first Notion synchronization."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import trio

from notionfs.converter import (
   blocks_to_markdown,
   build_frontmatter,
   frontmatter_to_properties,
   markdown_to_blocks,
   parse_frontmatter,
   parse_schema,
   properties_to_frontmatter,
   schema_to_notion_format,
   schema_to_yaml,
)
from notionfs.notion.api_client import NotionAPIClient
from notionfs.sync.state import SyncEntry, SyncState, SyncStatus

if TYPE_CHECKING:
   from notionfs.sync.progress import SyncProgress

# Type alias for progress callback (simple string notification)
ProgressCallback = Callable[[str], None] | None

logger = logging.getLogger(__name__)


# Async file I/O helpers (wrap blocking calls to avoid blocking trio event loop)
async def _async_read_text(path: Path, encoding: str = "utf-8") -> str:
   """Read file contents asynchronously."""
   return await trio.to_thread.run_sync(lambda: path.read_text(encoding=encoding))


async def _async_exists(path: Path) -> bool:
   """Check if path exists asynchronously."""
   return await trio.to_thread.run_sync(path.exists)


async def _async_unlink(path: Path) -> None:
   """Delete file asynchronously."""
   await trio.to_thread.run_sync(path.unlink)


async def _async_mkdir(path: Path, parents: bool = False, exist_ok: bool = False) -> None:
   """Create directory asynchronously."""
   await trio.to_thread.run_sync(lambda: path.mkdir(parents=parents, exist_ok=exist_ok))


async def _async_rmdir(path: Path) -> None:
   """Remove empty directory asynchronously."""
   await trio.to_thread.run_sync(path.rmdir)


@dataclass
class RemotePage:
   """Represents a page/database fetched from Notion."""

   id: str
   title: str
   url: str
   parent_id: str | None
   is_database: bool
   last_edited_time: datetime
   # For pages: blocks content. For databases: None (entries fetched separately)
   blocks: list[dict[str, Any]] | None = None
   # For database entries: property values
   properties: dict[str, Any] = field(default_factory=dict)
   # For databases: schema
   schema: dict[str, Any] = field(default_factory=dict)
   # Children pages/databases (for tree structure)
   children: list["RemotePage"] = field(default_factory=list)
   # Whether this page has child pages (determines if it's a directory)
   has_children: bool = False
   # True if blocks were intentionally skipped (page unchanged since last sync)
   content_skipped: bool = False


@dataclass
class PullResult:
   """Result of a pull operation."""

   created: list[str]  # Paths of newly created files
   updated: list[str]  # Paths of updated files
   deleted: list[str]  # Paths of deleted files
   conflicts: list[str]  # Paths with conflicts
   errors: list[str] = field(default_factory=list)  # Error messages from failed operations


@dataclass
class PushResult:
   """Result of a push operation."""

   created: list[str]  # Paths of newly created pages
   updated: list[str]  # Paths of updated pages
   deleted: list[str]  # Paths of deleted pages
   conflicts: list[str]  # Paths with conflicts


@dataclass
class SyncResult:
   """Combined result of pull + push."""

   pull: PullResult
   push: PushResult


# Forbidden filename characters → Unicode look-alikes (reversible mapping)
_FORBIDDEN_CHARS = {
   "/": "\u2215",  # Division slash
   "\\": "\u29F5",  # Reverse solidus operator
   ":": "\uA789",  # Modifier letter colon
   "*": "\u2217",  # Asterisk operator
   "?": "\uFF1F",  # Fullwidth question mark
   '"': "\u201C",  # Left double quotation
   "<": "\uFF1C",  # Fullwidth less-than
   ">": "\uFF1E",  # Fullwidth greater-than
   "|": "\u2223",  # Divides
}

# Reverse mapping for desanitization
_REVERSE_CHARS = {v: k for k, v in _FORBIDDEN_CHARS.items()}

# Pattern to match duplicate suffix: " (N)" at end of filename
_DUPLICATE_SUFFIX_RE = re.compile(r" \((\d+)\)$")

# Schema filename for databases
SCHEMA_FILENAME = "_schema.yaml"


def _normalize_unicode(text: str) -> str:
   """Normalize Unicode to NFC form for consistent filenames."""
   return unicodedata.normalize("NFC", text)


def _sanitize_filename(title: str) -> str:
   """Sanitize title for use as filename.

   - Normalizes Unicode to NFC form
   - Replaces forbidden characters with Unicode look-alikes
   - Strips whitespace
   - Handles empty/whitespace-only titles

   The mapping is reversible via _desanitize_filename().
   """
   if not title:
      return "Untitled"
   # Normalize Unicode first
   result = _normalize_unicode(title)
   # Replace forbidden chars
   for char, replacement in _FORBIDDEN_CHARS.items():
      result = result.replace(char, replacement)
   return result.strip() or "Untitled"


def _desanitize_filename(filename: str) -> str:
   """Recover original title from sanitized filename.

   Reverses the character substitutions made by _sanitize_filename().
   Also strips any duplicate suffix like " (2)".
   """
   # Strip duplicate suffix if present
   result = _DUPLICATE_SUFFIX_RE.sub("", filename)
   # Reverse character substitutions
   for replacement, original in _REVERSE_CHARS.items():
      result = result.replace(replacement, original)
   return result


def _make_unique_filename(
   base_name: str,
   existing_names: set[str],
) -> str:
   """Generate unique filename by adding suffix if needed.

   Args:
       base_name: The sanitized base filename (without extension).
       existing_names: Set of already-used filenames in this directory.

   Returns:
       A unique filename, with " (N)" suffix if base_name already exists.
   """
   if base_name not in existing_names:
      return base_name

   # Find next available suffix
   n = 2
   while True:
      candidate = f"{base_name} ({n})"
      if candidate not in existing_names:
         return candidate
      n += 1


def _compute_hash(content: str) -> str:
   """Compute SHA256 hash of content."""
   return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _extract_title(page: dict[str, Any]) -> str:
   """Extract title from Notion page object."""
   props = page.get("properties", {})
   # Find the property with type "title" (can have any name)
   for prop in props.values():
      if isinstance(prop, dict) and prop.get("type") == "title":
         title_arr = prop.get("title", [])
         if title_arr:
            return "".join(t.get("plain_text", "") for t in title_arr)
   return "Untitled"


def _parse_datetime(dt_str: str | None) -> datetime | None:
   """Parse ISO datetime string to datetime object."""
   if not dt_str:
      return None
   try:
      # Handle Notion's ISO format (with or without milliseconds)
      if dt_str.endswith("Z"):
         dt_str = dt_str[:-1] + "+00:00"
      return datetime.fromisoformat(dt_str)
   except (ValueError, TypeError):
      return None


class SyncEngine:
   """Core sync engine for local-first Notion synchronization."""

   def __init__(
      self,
      workspace_path: Path,
      root_id: str,
      api_client: NotionAPIClient,
      state: SyncState,
      root_type: str = "page",
   ) -> None:
      """Initialize sync engine.

      Args:
          workspace_path: Path to local workspace directory.
          root_id: Notion page or database ID to sync.
          api_client: NotionAPIClient instance.
          state: SyncState instance for tracking sync state.
          root_type: "page" or "database" - type of the root item.
      """
      self.workspace_path = workspace_path
      self.root_id = root_id
      self.api_client = api_client
      self.state = state
      self.root_type = root_type
      # Track seen notion IDs during pull for deletion detection
      self._remote_ids: set[str] = set()
      # Track IDs that failed to fetch (to avoid false deletion detection)
      self._fetch_failed_ids: set[str] = set()
      # Progress tracking (optional)
      self._progress: SyncProgress | None = None
      # Cache of known remote mtimes from state (notion_id -> remote_mtime)
      # Used to skip fetching blocks for unchanged pages
      self._known_mtimes: dict[str, datetime] = {}

   def set_progress(self, progress: SyncProgress | None) -> None:
      """Set progress tracker for UI feedback.

      Args:
          progress: SyncProgress instance or None to disable.
      """
      self._progress = progress

   def _advance_progress(self, count: int = 1) -> None:
      """Advance progress by count entries if progress is set."""
      if self._progress:
         self._progress.advance(count)

   def _set_progress_total(self, total: int) -> None:
      """Update progress total if progress is set."""
      if self._progress:
         self._progress.set_total(total)

   async def pull(self, force: bool = False) -> PullResult:
      """Fetch remote changes and write to local files.

      Args:
          force: If True, overwrite local changes on conflict.

      Returns:
          PullResult with created, updated, deleted, and conflicted paths.
      """
      logger.info("Starting pull (force=%s, root_type=%s)", force, self.root_type)
      result = PullResult(created=[], updated=[], deleted=[], conflicts=[])

      self._remote_ids.clear()
      self._fetch_failed_ids.clear()
      is_database = self.root_type == "database"

      # Build mtime cache from existing state for incremental fetching
      all_entries = await self.state.list_entries()
      self._known_mtimes.clear()
      for entry in all_entries:
         if entry.remote_mtime:
            self._known_mtimes[entry.notion_id] = entry.remote_mtime

      # For databases, use streaming pull (fetch+write per entry)
      if is_database:
         await self._pull_database_streaming(force, result)
      else:
         # For pages, use tree-based pull
         tree = await self.fetch_tree(self.root_id, is_database=False)
         if tree is None:
            error_msg = f"Failed to fetch remote tree from {self.root_id}"
            logger.error(error_msg)
            result.errors.append(error_msg)
            return result

         # Count total entries for progress
         total_entries = self._count_tree_entries(tree)
         total_entries += len(all_entries)
         self._set_progress_total(total_entries)

         used_filenames: dict[str, set[str]] = {}
         await self._process_remote_tree(tree, Path(""), result, force, used_filenames)

         # Handle deletions (skip entries whose fetch failed to avoid false deletion)
         for entry in all_entries:
            if entry.notion_id not in self._remote_ids:
               if entry.notion_id not in self._fetch_failed_ids:
                  await self._handle_remote_deletion(entry, result, force)
            self._advance_progress()

      logger.info(
         "Pull complete: created=%d, updated=%d, deleted=%d, conflicts=%d",
         len(result.created),
         len(result.updated),
         len(result.deleted),
         len(result.conflicts),
      )
      return result

   async def _pull_database_streaming(self, force: bool, result: PullResult) -> None:
      """Stream-pull a database: fetch and write each entry as it completes.

      This provides better UX for large databases by showing progress during fetch.
      """
      database_id = self.root_id

      # Fetch database metadata
      try:
         self._remote_ids.add(database_id)
         db = await self.api_client.get_database(database_id)
      except Exception as e:
         result.errors.append(f"Failed to fetch database {database_id}: {e}")
         return

      # Write schema
      schema = db.get("properties", {})
      if schema:
         await self._write_schema(Path(""), schema, result)

      # Query all entries
      try:
         entries = await self.api_client.query_database(database_id)
      except Exception as e:
         result.errors.append(f"Failed to query database {database_id}: {e}")
         return

      # Set up progress
      all_entries = await self.state.list_entries()
      self._set_progress_total(len(entries) + len(all_entries))

      # Track filenames for deduplication (needs lock for parallel access)
      used_filenames: set[str] = set()
      filename_lock = trio.Lock()
      # Lock for result list mutations from parallel tasks
      result_lock = trio.Lock()

      async def fetch_and_write_entry(entry: dict[str, Any]) -> None:
         """Fetch blocks for one entry and write it immediately."""
         entry_id = entry.get("id")
         if not entry_id:
            return

         self._remote_ids.add(entry_id)

         try:
            # Fetch blocks
            entry_blocks = await self.api_client.get_block_children(entry_id)

            # Build RemotePage
            remote = RemotePage(
               id=entry_id,
               title=_extract_title(entry),
               url=entry.get("url", ""),
               parent_id=database_id,
               is_database=False,
               last_edited_time=_parse_datetime(entry.get("last_edited_time"))
               or datetime.now(timezone.utc),
               blocks=entry_blocks,
               properties=entry.get("properties", {}),
               has_children=False,
            )

            # Determine file path (with lock for filename deduplication)
            existing = await self.state.get_entry_by_notion_id(entry_id)
            if existing:
               file_path = Path(existing.path)
               async with filename_lock:
                  used_filenames.add(file_path.stem)
            else:
               base_filename = _sanitize_filename(remote.title)
               async with filename_lock:
                  unique_filename = _make_unique_filename(base_filename, used_filenames)
                  used_filenames.add(unique_filename)
               file_path = Path(f"{unique_filename}.md")

            # Generate and write content
            content = self._generate_markdown(remote)
            remote_hash = _compute_hash(content)
            abs_path = self.workspace_path / file_path
            rel_path = str(file_path)

            if existing is None:
               # New entry
               await self._write_page(abs_path, content)
               new_entry = SyncEntry(
                  path=rel_path,
                  notion_id=remote.id,
                  notion_url=remote.url,
                  notion_parent_id=remote.parent_id,
                  is_directory=False,
                  local_hash=remote_hash,
                  remote_hash=remote_hash,
                  local_mtime=datetime.now(timezone.utc),
                  remote_mtime=remote.last_edited_time,
                  status=SyncStatus.clean,
               )
               await self.state.set_entry(new_entry)
               async with result_lock:
                  result.created.append(rel_path)
            elif remote.last_edited_time > (
               existing.remote_mtime or datetime.min.replace(tzinfo=timezone.utc)
            ):
               # Remote changed - check for conflict
               local_hash = None
               if await _async_exists(abs_path):
                  local_content = await _async_read_text(abs_path)
                  local_hash = _compute_hash(local_content)

               if local_hash and local_hash != existing.local_hash and not force:
                  existing.status = SyncStatus.conflict
                  await self.state.set_entry(existing)
                  async with result_lock:
                     result.conflicts.append(rel_path)
               else:
                  await self._write_page(abs_path, content)
                  existing.local_hash = remote_hash
                  existing.remote_hash = remote_hash
                  existing.remote_mtime = remote.last_edited_time
                  existing.status = SyncStatus.clean
                  await self.state.set_entry(existing)
                  async with result_lock:
                     result.updated.append(rel_path)

            self._advance_progress()

         except Exception as e:
            logger.error("Failed to process entry %s: %s", entry_id, e)
            async with result_lock:
               result.errors.append(f"Failed to process entry {entry_id}: {e}")

      # Fetch and write all entries in parallel
      async with trio.open_nursery() as nursery:
         for entry in entries:
            nursery.start_soon(fetch_and_write_entry, entry)

      # Handle deletions (skip entries whose fetch failed to avoid false deletion)
      for state_entry in all_entries:
         if state_entry.notion_id not in self._remote_ids:
            if state_entry.notion_id not in self._fetch_failed_ids:
               await self._handle_remote_deletion(state_entry, result, force)
         self._advance_progress()

   def _count_tree_entries(self, tree: RemotePage) -> int:
      """Count total entries in a remote tree (for progress tracking)."""
      count = 1  # Count this node
      for child in tree.children:
         count += self._count_tree_entries(child)
      return count

   async def push(self, force: bool = False) -> PushResult:
      """Scan local files and push changes to Notion.

      Args:
          force: If True, overwrite remote changes on conflict.

      Returns:
          PushResult with created, updated, deleted, and conflicted paths.
      """
      logger.info("Starting push (force=%s)", force)
      result = PushResult(created=[], updated=[], deleted=[], conflicts=[])

      # Scan local files
      local_files = self._scan_local_files()

      # Handle local deletions (entries in state but file doesn't exist)
      all_entries = await self.state.list_entries()

      # Pre-scan: determine which files actually need pushing
      files_to_push: list[tuple[str, str, str]] = []  # (rel_path, content, hash)
      for rel_path in local_files:
         abs_path = self.workspace_path / rel_path
         content = await _async_read_text(abs_path)
         current_hash = _compute_hash(content)
         entry = await self.state.get_entry(rel_path)
         if entry is None or current_hash != entry.local_hash:
            files_to_push.append((rel_path, content, current_hash))

      # Pre-scan: determine which entries are locally deleted
      deletions_to_process: list[SyncEntry] = []
      for entry in all_entries:
         local_path = self.workspace_path / entry.path
         if not await _async_exists(local_path):
            deletions_to_process.append(entry)

      # Set progress to actual work count
      total_work = len(files_to_push) + len(deletions_to_process)
      self._set_progress_total(total_work)

      # Process files that need pushing
      for rel_path, content, current_hash in files_to_push:
         await self._process_local_file_with_content(
            rel_path, content, current_hash, result, force
         )
         self._advance_progress()

      # Process deletions
      for entry in deletions_to_process:
         await self._handle_local_deletion(entry, result, force)
         self._advance_progress()

      logger.info(
         "Push complete: created=%d, updated=%d, deleted=%d, conflicts=%d",
         len(result.created),
         len(result.updated),
         len(result.deleted),
         len(result.conflicts),
      )
      return result

   async def sync(self) -> SyncResult:
      """Bidirectional sync: pull then push.

      Returns:
          SyncResult containing both pull and push results.
      """
      logger.info("Starting bidirectional sync")
      pull_result = await self.pull()
      push_result = await self.push()
      return SyncResult(pull=pull_result, push=push_result)

   async def status(self) -> list[SyncEntry]:
      """Show pending changes without syncing.

      Returns:
          List of SyncEntry objects with non-clean status.
      """
      # Refresh status from current state
      await self._refresh_status()

      # Return entries with pending changes
      non_clean = await self.state.list_by_statuses([
         SyncStatus.local_modified,
         SyncStatus.remote_modified,
         SyncStatus.conflict,
         SyncStatus.deleted_local,
         SyncStatus.deleted_remote,
      ])
      return non_clean

   async def resolve_conflict(
      self, path: str, resolution: str
   ) -> None:
      """Resolve a conflict for a specific file.

      Args:
          path: Relative path of the conflicted file.
          resolution: One of 'keep_local', 'keep_remote', or 'keep_both'.

      Raises:
          ValueError: If path not found or not in conflict status.
          ValueError: If resolution is invalid.
      """
      if resolution not in ("keep_local", "keep_remote", "keep_both"):
         raise ValueError(f"Invalid resolution: {resolution}")

      entry = await self.state.get_entry(path)
      if entry is None:
         raise ValueError(f"No entry found for path: {path}")

      if entry.status not in (SyncStatus.conflict, SyncStatus.deleted_remote,
                              SyncStatus.deleted_local):
         raise ValueError(f"Entry is not in conflict status: {entry.status.value}")

      abs_path = self.workspace_path / path

      if resolution == "keep_local":
         # Mark as local_modified so next push will overwrite remote
         entry.status = SyncStatus.local_modified
         await self.state.set_entry(entry)
         logger.info("Resolved conflict (keep_local): %s", path)

      elif resolution == "keep_remote":
         # Pull remote with force to overwrite local
         if entry.status == SyncStatus.deleted_remote:
            # Remote was deleted - delete local file
            if await _async_exists(abs_path):
               await _async_unlink(abs_path)
            await self.state.delete_entry(path)
            logger.info("Resolved conflict (keep_remote, deleted): %s", path)
         else:
            # Fetch remote content and overwrite local
            try:
               page_data = await self.api_client.get_page(entry.notion_id)
               page = page_data["page"]
               blocks = page_data["blocks"]

               remote = RemotePage(
                  id=entry.notion_id,
                  title=_extract_title(page),
                  url=page.get("url", ""),
                  parent_id=entry.notion_parent_id,
                  is_database=entry.is_directory,
                  last_edited_time=_parse_datetime(page.get("last_edited_time"))
                  or datetime.now(timezone.utc),
                  blocks=blocks,
                  properties=page.get("properties", {}),
               )

               content = self._generate_markdown(remote)
               remote_hash = _compute_hash(content)
               await self._write_page(abs_path, content)

               entry.local_hash = remote_hash
               entry.remote_hash = remote_hash
               entry.remote_mtime = remote.last_edited_time
               entry.status = SyncStatus.clean
               await self.state.set_entry(entry)
               logger.info("Resolved conflict (keep_remote): %s", path)

            except Exception as e:
               logger.error("Failed to fetch remote for conflict resolution: %s", e)
               raise

      elif resolution == "keep_both":
         # Save local as .conflict copy, then pull remote
         if await _async_exists(abs_path):
            local_content = await _async_read_text(abs_path)
            # Generate conflict filename: Page.md → Page.conflict.md
            conflict_path = abs_path.with_suffix(".conflict" + abs_path.suffix)
            await self._write_page(conflict_path, local_content)
            logger.info("Saved local copy to: %s", conflict_path)

         # Now pull remote (same as keep_remote)
         if entry.status != SyncStatus.deleted_remote:
            try:
               page_data = await self.api_client.get_page(entry.notion_id)
               page = page_data["page"]
               blocks = page_data["blocks"]

               remote = RemotePage(
                  id=entry.notion_id,
                  title=_extract_title(page),
                  url=page.get("url", ""),
                  parent_id=entry.notion_parent_id,
                  is_database=entry.is_directory,
                  last_edited_time=_parse_datetime(page.get("last_edited_time"))
                  or datetime.now(timezone.utc),
                  blocks=blocks,
                  properties=page.get("properties", {}),
               )

               content = self._generate_markdown(remote)
               remote_hash = _compute_hash(content)
               await self._write_page(abs_path, content)

               entry.local_hash = remote_hash
               entry.remote_hash = remote_hash
               entry.remote_mtime = remote.last_edited_time
               entry.status = SyncStatus.clean
               await self.state.set_entry(entry)
               logger.info("Resolved conflict (keep_both): %s", path)

            except Exception as e:
               logger.error("Failed to fetch remote for conflict resolution: %s", e)
               raise
         else:
            # Remote deleted, we kept local copy, remove original tracking
            await self.state.delete_entry(path)
            logger.info("Resolved conflict (keep_both, remote deleted): %s", path)

   async def fetch_tree(self, page_id: str, is_database: bool = False) -> RemotePage | None:
      """Recursively fetch page/database tree from Notion.

      Uses trio parallelism with the API client's rate limiter.
      Skips fetching blocks for pages whose last_edited_time hasn't changed
      since the last sync (uses _known_mtimes cache).

      Args:
          page_id: Notion page or database ID.
          is_database: If True, fetch as database instead of page.

      Returns:
          RemotePage tree structure, or None on error.
      """
      logger.debug("Fetching tree from %s (is_database=%s)", page_id, is_database)

      # If this is a database, delegate to _fetch_database
      if is_database:
         return await self._fetch_database(page_id)

      try:
         # Fetch page metadata first (without blocks)
         page = await self.api_client.get_page_metadata(page_id)

         self._remote_ids.add(page_id)

         title = _extract_title(page)
         url = page.get("url", "")
         parent = page.get("parent", {})
         parent_id = parent.get("page_id") or parent.get("database_id")
         last_edited = _parse_datetime(page.get("last_edited_time"))

         # Check if page content changed since last sync
         known_mtime = self._known_mtimes.get(page_id)
         content_skipped = False

         if known_mtime and last_edited and last_edited <= known_mtime:
            # Page unchanged - mark as skipped (no need to write file)
            logger.debug("Page %s unchanged (mtime %s <= %s)", page_id, last_edited, known_mtime)
            content_skipped = True

         # Always fetch block children for tree traversal (needed to find child_page/child_database)
         # TODO: future optimization - cache tree structure to skip this for unchanged parents
         blocks = await self.api_client.get_block_children(page_id)

         # Check for child pages/databases in blocks
         child_refs: list[tuple[str, str]] = []  # (id, type)
         for block in blocks:
            block_type = block.get("type", "")
            if block_type == "child_page":
               child_id = block.get("id")
               if child_id:
                  child_refs.append((child_id, "page"))
            elif block_type == "child_database":
               child_id = block.get("id")
               if child_id:
                  child_refs.append((child_id, "database"))

         has_children = len(child_refs) > 0

         remote_page = RemotePage(
            id=page_id,
            title=title,
            url=url,
            parent_id=parent_id,
            is_database=False,
            last_edited_time=last_edited or datetime.now(timezone.utc),
            blocks=blocks,
            properties=page.get("properties", {}),
            has_children=has_children,
            content_skipped=content_skipped,
         )

         # Fetch children in parallel
         if child_refs:
            children: list[RemotePage] = []

            async def fetch_child(child_id: str, child_type: str) -> RemotePage | None:
               if child_type == "database":
                  return await self._fetch_database(child_id)
               else:
                  return await self.fetch_tree(child_id)

            async with trio.open_nursery() as nursery:
               results: list[RemotePage | None] = [None] * len(child_refs)

               async def fetch_and_store(idx: int, cid: str, ctype: str) -> None:
                  result = await fetch_child(cid, ctype)
                  if result is None:
                     # Track failed ID to prevent false deletion detection
                     self._fetch_failed_ids.add(cid)
                  results[idx] = result

               for i, (cid, ctype) in enumerate(child_refs):
                  nursery.start_soon(fetch_and_store, i, cid, ctype)

            for child in results:
               if child is not None:
                  children.append(child)

            remote_page.children = children

         return remote_page

      except Exception as e:
         logger.error("Failed to fetch tree for %s: %s", page_id, e)
         return None

   async def _fetch_database(self, database_id: str) -> RemotePage | None:
      """Fetch a database and its entries."""
      try:
         self._remote_ids.add(database_id)
         db = await self.api_client.get_database(database_id)

         # Extract title from database
         title_arr = db.get("title", [])
         title = "".join(t.get("plain_text", "") for t in title_arr) or "Untitled Database"

         url = db.get("url", "")
         parent = db.get("parent", {})
         parent_id = parent.get("page_id") or parent.get("database_id")
         last_edited = _parse_datetime(db.get("last_edited_time"))

         remote_db = RemotePage(
            id=database_id,
            title=title,
            url=url,
            parent_id=parent_id,
            is_database=True,
            last_edited_time=last_edited or datetime.now(timezone.utc),
            blocks=None,
            schema=db.get("properties", {}),
            has_children=True,  # Databases always have potential children
         )

         # Fetch database entries
         entries = await self.api_client.query_database(database_id)

         # Build list of entries to fetch blocks for
         entry_data: list[tuple[str, str, str, datetime | None, dict[str, Any]]] = []
         for entry in entries:
            entry_id = entry.get("id")
            if not entry_id:
               continue
            self._remote_ids.add(entry_id)
            entry_data.append((
               entry_id,
               _extract_title(entry),
               entry.get("url", ""),
               _parse_datetime(entry.get("last_edited_time")),
               entry.get("properties", {}),
            ))

         # Fetch blocks for all entries in parallel
         children: list[RemotePage | None] = [None] * len(entry_data)

         async def fetch_entry_blocks(idx: int, eid: str, title: str, url: str,
                                       edited: datetime | None, props: dict[str, Any]) -> None:
            try:
               entry_blocks = await self.api_client.get_block_children(eid)
               children[idx] = RemotePage(
                  id=eid,
                  title=title,
                  url=url,
                  parent_id=database_id,
                  is_database=False,
                  last_edited_time=edited or datetime.now(timezone.utc),
                  blocks=entry_blocks,
                  properties=props,
                  has_children=False,
               )
            except Exception as e:
               logger.error("Failed to fetch blocks for entry %s: %s", eid, e)
               self._fetch_failed_ids.add(eid)

         async with trio.open_nursery() as nursery:
            for i, (eid, title, url, edited, props) in enumerate(entry_data):
               nursery.start_soon(fetch_entry_blocks, i, eid, title, url, edited, props)

         remote_db.children = [c for c in children if c is not None]
         return remote_db

      except Exception as e:
         logger.error("Failed to fetch database %s: %s", database_id, e)
         return None

   async def _process_remote_tree(
      self,
      remote: RemotePage,
      parent_path: Path,
      result: PullResult,
      force: bool,
      used_filenames: dict[str, set[str]],
   ) -> None:
      """Process a remote page/database and its children.

      Args:
          remote: RemotePage to process.
          parent_path: Parent directory path (relative).
          result: PullResult to accumulate changes.
          force: Whether to force overwrite on conflicts.
          used_filenames: Dict tracking used filenames per directory for deduplication.
      """
      # Check existing entry first - use existing path for stability
      entry = await self.state.get_entry_by_notion_id(remote.id)

      parent_key = str(parent_path)
      if parent_key not in used_filenames:
         used_filenames[parent_key] = set()

      if entry is not None:
         # Existing page: use stable path from state
         file_path = Path(entry.path)
         # Extract the filename part for tracking
         if file_path.name == "_index.md":
            dir_path = file_path.parent
            base_filename = dir_path.name
         else:
            dir_path = None
            base_filename = file_path.stem
         # Mark as used to prevent conflicts with new pages
         used_filenames[parent_key].add(base_filename)
      else:
         # New page: generate unique filename
         base_filename = _sanitize_filename(remote.title)
         unique_filename = _make_unique_filename(base_filename, used_filenames[parent_key])
         used_filenames[parent_key].add(unique_filename)

         if remote.is_database or remote.has_children:
            dir_path = parent_path / unique_filename
            file_path = dir_path / "_index.md"
         else:
            file_path = parent_path / f"{unique_filename}.md"
            dir_path = None

      rel_path = str(file_path)
      abs_path = self.workspace_path / file_path

      # Skip processing if content was not fetched (page unchanged)
      if remote.content_skipped and entry is not None:
         logger.debug("Skipping unchanged page: %s", rel_path)
         self._advance_progress()
         # Still process children - use stored path for child parent
         if Path(entry.path).name == "_index.md":
            child_parent = Path(entry.path).parent
         else:
            child_parent = parent_path
         for child in remote.children:
            await self._process_remote_tree(child, child_parent, result, force, used_filenames)
         return

      # Generate markdown content
      content = self._generate_markdown(remote)
      remote_hash = _compute_hash(content)

      if entry is None:
         # New remote page → create local
         await self._write_page(abs_path, content)
         if dir_path:
            await _async_mkdir(self.workspace_path / dir_path, parents=True, exist_ok=True)

         new_entry = SyncEntry(
            path=rel_path,
            notion_id=remote.id,
            notion_url=remote.url,
            notion_parent_id=remote.parent_id,
            is_directory=remote.is_database or remote.has_children,
            local_hash=remote_hash,
            remote_hash=remote_hash,
            local_mtime=datetime.now(timezone.utc),
            remote_mtime=remote.last_edited_time,
            status=SyncStatus.clean,
         )
         await self.state.set_entry(new_entry)
         result.created.append(rel_path)
         logger.debug("Created: %s", rel_path)

      elif remote.last_edited_time > (
         entry.remote_mtime or datetime.min.replace(tzinfo=timezone.utc)
      ):
         # Remote changed since last sync
         local_hash = None
         if await _async_exists(abs_path):
            local_content = await _async_read_text(abs_path)
            local_hash = _compute_hash(local_content)

         if local_hash and local_hash != entry.local_hash:
            # Local also changed → conflict
            if force:
               # Force: overwrite local
               await self._write_page(abs_path, content)
               entry.local_hash = remote_hash
               entry.remote_hash = remote_hash
               entry.remote_mtime = remote.last_edited_time
               entry.status = SyncStatus.clean
               await self.state.set_entry(entry)
               result.updated.append(rel_path)
               logger.debug("Force updated (conflict resolved): %s", rel_path)
            else:
               entry.status = SyncStatus.conflict
               await self.state.set_entry(entry)
               result.conflicts.append(rel_path)
               logger.warning("Conflict: %s", rel_path)
         else:
            # Only remote changed → update local
            try:
               await self._write_page(abs_path, content)
               entry.local_hash = remote_hash
               entry.remote_hash = remote_hash
               entry.remote_mtime = remote.last_edited_time
               entry.status = SyncStatus.clean
               await self.state.set_entry(entry)
               result.updated.append(rel_path)
               logger.debug("Updated: %s", rel_path)
            except Exception as e:
               # Write failed - mark as remote_modified so we know there's a pending pull
               logger.error("Failed to write %s: %s", rel_path, e)
               entry.remote_hash = remote_hash
               entry.remote_mtime = remote.last_edited_time
               entry.status = SyncStatus.remote_modified
               await self.state.set_entry(entry)

      elif entry is not None and remote_hash != entry.remote_hash:
         # Remote content changed but timestamp didn't update (edge case) or
         # we're tracking remote changes without writing. Set remote_modified.
         entry.remote_hash = remote_hash
         entry.status = SyncStatus.remote_modified
         await self.state.set_entry(entry)
         logger.debug("Remote modified (no timestamp change): %s", rel_path)

      # Advance progress after processing this entry
      self._advance_progress()

      # Generate schema file for databases
      if remote.is_database and remote.schema and dir_path:
         await self._write_schema(dir_path, remote.schema, result)

      # Process children - determine child parent path
      if entry is not None:
         # For existing entries, derive from stored path
         if Path(entry.path).name == "_index.md":
            child_parent = Path(entry.path).parent
         else:
            child_parent = parent_path
      else:
         child_parent = dir_path if dir_path else parent_path

      for child in remote.children:
         await self._process_remote_tree(child, child_parent, result, force, used_filenames)

   async def _handle_remote_deletion(
      self, entry: SyncEntry, result: PullResult, force: bool
   ) -> None:
      """Handle a page deleted remotely."""
      abs_path = self.workspace_path / entry.path

      if not await _async_exists(abs_path):
         # Already deleted locally, just remove from state
         await self.state.delete_entry(entry.path)
         return

      local_content = await _async_read_text(abs_path)
      local_hash = _compute_hash(local_content)

      if local_hash == entry.local_hash or force:
         # Clean local or force: delete local file
         await _async_unlink(abs_path)
         # Clean up empty parent directories
         parent = abs_path.parent
         while parent != self.workspace_path:
            try:
               await _async_rmdir(parent)
               parent = parent.parent
            except OSError:
               break
         await self.state.delete_entry(entry.path)
         result.deleted.append(entry.path)
         logger.debug("Deleted: %s", entry.path)
      else:
         # Local modified, remote deleted → conflict
         entry.status = SyncStatus.deleted_remote
         await self.state.set_entry(entry)
         result.conflicts.append(entry.path)
         logger.warning("Conflict (deleted remote): %s", entry.path)

   async def _process_local_file(
      self, rel_path: str, result: PushResult, force: bool
   ) -> None:
      """Process a local file for push."""
      abs_path = self.workspace_path / rel_path
      content = await _async_read_text(abs_path)
      current_hash = _compute_hash(content)
      await self._process_local_file_with_content(
         rel_path, content, current_hash, result, force
      )

   async def _process_local_file_with_content(
      self, rel_path: str, content: str, current_hash: str, result: PushResult, force: bool
   ) -> None:
      """Process a local file for push with pre-computed content and hash."""
      entry = await self.state.get_entry(rel_path)

      if entry is None:
         # New local file → create in Notion
         await self._create_notion_page(rel_path, content, current_hash, result)
      elif current_hash != entry.local_hash:
         # Local changed
         await self._push_local_changes(entry, content, current_hash, result, force)

   async def _create_notion_page(
      self, rel_path: str, content: str, content_hash: str, result: PushResult
   ) -> None:
      """Create a new page in Notion from local file."""
      path = Path(rel_path)
      metadata, body = parse_frontmatter(content)

      # Determine parent
      parent_path = path.parent
      if parent_path == Path("."):
         parent_id = self.root_id
         # Use root_type to determine parent type at root level
         parent_type = "database_id" if self.root_type == "database" else "page_id"
      else:
         # Look up parent in state
         parent_entry = await self.state.get_entry(str(parent_path / "_index.md"))
         if parent_entry:
            parent_id = parent_entry.notion_id
         else:
            # Try as direct parent directory
            for entry in await self.state.list_entries():
               if entry.is_directory and Path(entry.path).parent == parent_path:
                  parent_id = entry.notion_id
                  break
            else:
               parent_id = self.root_id
         parent_type = "page_id"  # Non-root parents default to page

      # Extract title from filename
      if path.name == "_index.md":
         title = path.parent.name
      else:
         title = path.stem

      # Check if parent is a database by looking for _schema.yaml
      schema = await self._load_schema(parent_path)

      # Convert properties and blocks
      if schema:
         # Parent is a database - find the title property name from schema
         parent_type = "database_id"
         title_prop_name = "Name"  # Default fallback
         for prop_name, prop_def in schema.items():
            if isinstance(prop_def, dict) and prop_def.get("type") == "title":
               title_prop_name = prop_name
               break
         properties: dict[str, Any] = {
            title_prop_name: {"title": [{"text": {"content": title}}]}
         }
         if metadata:
            # Convert frontmatter to Notion properties
            properties.update(frontmatter_to_properties(metadata, schema))
      else:
         # Parent is a page - use standard "title" property
         properties = {"title": {"title": [{"text": {"content": title}}]}}

      blocks = markdown_to_blocks(body)

      try:
         new_id = await self.api_client.create_page(
            parent_id=parent_id,
            properties=properties,
            children=blocks,
            parent_type=parent_type,
         )

         # Fetch created page for URL
         page_data = await self.api_client.get_page(new_id)
         url = page_data["page"].get("url", "")
         remote_mtime = _parse_datetime(page_data["page"].get("last_edited_time"))

         new_entry = SyncEntry(
            path=rel_path,
            notion_id=new_id,
            notion_url=url,
            notion_parent_id=parent_id,
            is_directory=path.name == "_index.md",
            local_hash=content_hash,
            remote_hash=content_hash,
            local_mtime=datetime.now(timezone.utc),
            remote_mtime=remote_mtime,
            status=SyncStatus.clean,
         )
         await self.state.set_entry(new_entry)
         result.created.append(rel_path)
         logger.debug("Created in Notion: %s → %s", rel_path, new_id)

      except Exception as e:
         logger.error("Failed to create page %s: %s", rel_path, e)

   async def _push_local_changes(
      self,
      entry: SyncEntry,
      content: str,
      content_hash: str,
      result: PushResult,
      force: bool,
   ) -> None:
      """Push local changes to an existing Notion page."""
      # Check if remote has changed
      if not force:
         try:
            page_data = await self.api_client.get_page(entry.notion_id)
            remote_mtime = _parse_datetime(page_data["page"].get("last_edited_time"))

            if remote_mtime and entry.remote_mtime and remote_mtime > entry.remote_mtime:
               # Remote also changed → conflict
               entry.status = SyncStatus.conflict
               await self.state.set_entry(entry)
               result.conflicts.append(entry.path)
               logger.warning("Conflict: %s", entry.path)
               return
         except Exception as e:
            logger.error("Failed to check remote state for %s: %s", entry.path, e)
            return

      # Parse and push
      metadata, body = parse_frontmatter(content)
      blocks = markdown_to_blocks(body)

      try:
         # Update properties if metadata present
         if metadata:
            # Load schema from parent directory's _schema.yaml
            parent_path = Path(entry.path).parent
            schema = await self._load_schema(parent_path)
            if schema:
               properties = frontmatter_to_properties(metadata, schema)
               if properties:
                  await self.api_client.update_page_properties(entry.notion_id, properties)

         # Update blocks
         await self.api_client.update_blocks(entry.notion_id, blocks)

         # Refresh remote state
         page_data = await self.api_client.get_page(entry.notion_id)
         remote_mtime = _parse_datetime(page_data["page"].get("last_edited_time"))

         entry.local_hash = content_hash
         entry.remote_hash = content_hash
         entry.remote_mtime = remote_mtime
         entry.status = SyncStatus.clean
         await self.state.set_entry(entry)
         result.updated.append(entry.path)
         logger.debug("Pushed: %s", entry.path)

      except Exception as e:
         logger.error("Failed to push %s: %s", entry.path, e)

   async def _handle_local_deletion(
      self, entry: SyncEntry, result: PushResult, force: bool
   ) -> None:
      """Handle a locally deleted file."""
      if not force:
         # Check if remote has changed
         try:
            page_data = await self.api_client.get_page(entry.notion_id)
            remote_mtime = _parse_datetime(page_data["page"].get("last_edited_time"))

            if remote_mtime and entry.remote_mtime and remote_mtime > entry.remote_mtime:
               # Remote changed after local deletion → conflict
               entry.status = SyncStatus.deleted_local
               await self.state.set_entry(entry)
               result.conflicts.append(entry.path)
               logger.warning("Conflict (deleted local): %s", entry.path)
               return
         except Exception as e:
            # Page might already be deleted remotely
            logger.debug("Could not check remote for deleted %s: %s", entry.path, e)

      # Archive the page
      try:
         await self.api_client.archive_page(entry.notion_id)
         await self.state.delete_entry(entry.path)
         result.deleted.append(entry.path)
         logger.debug("Archived in Notion: %s", entry.path)
      except Exception as e:
         logger.error("Failed to archive %s: %s", entry.path, e)

   def _scan_local_files(self) -> list[str]:
      """Scan workspace for .md files.

      Symlinks are explicitly ignored - they are not followed and not synced.
      This prevents potential cycles, security issues, and unexpected behavior
      when syncing external content.
      """
      files: list[str] = []
      for root, dirnames, filenames in os.walk(self.workspace_path):
         # Skip hidden directories
         if any(part.startswith(".") for part in Path(root).parts):
            continue

         # Filter out symlinked directories (don't descend into them)
         dirnames[:] = [d for d in dirnames if not (Path(root) / d).is_symlink()]

         for filename in filenames:
            abs_path = Path(root) / filename
            # Skip symlinks
            if abs_path.is_symlink():
               logger.debug("Skipping symlink: %s", abs_path)
               continue
            if filename.endswith(".md"):
               rel_path = abs_path.relative_to(self.workspace_path)
               files.append(str(rel_path))
      return sorted(files)

   async def _refresh_status(self) -> None:
      """Refresh status of all entries based on current local/remote state."""
      all_entries = await self.state.list_entries()

      for entry in all_entries:
         abs_path = self.workspace_path / entry.path

         if not await _async_exists(abs_path):
            if entry.status not in (SyncStatus.deleted_local, SyncStatus.deleted_remote):
               entry.status = SyncStatus.deleted_local
               await self.state.set_entry(entry)
            continue

         local_content = await _async_read_text(abs_path)
         local_hash = _compute_hash(local_content)

         if local_hash != entry.local_hash:
            if entry.status == SyncStatus.clean:
               entry.status = SyncStatus.local_modified
               await self.state.set_entry(entry)

   def _generate_markdown(self, remote: RemotePage) -> str:
      """Generate markdown content from remote page."""
      # Convert blocks to markdown
      if remote.blocks:
         body = blocks_to_markdown(remote.blocks)
      else:
         body = ""

      # Add frontmatter for database entries
      if remote.properties and remote.parent_id:
         # Check if this looks like a database entry (has non-title properties)
         prop_keys = set(remote.properties.keys()) - {"title", "Name"}
         if prop_keys:
            try:
               metadata = properties_to_frontmatter(remote.properties)
               # Remove readonly keys we don't want in frontmatter
               metadata.pop("notion_id", None)
               metadata.pop("notion_url", None)
               if metadata:
                  return build_frontmatter(metadata, body)
            except Exception as e:
               logger.warning("Failed to convert properties: %s", e)

      return body

   async def _write_page(self, path: Path, content: str) -> None:
      """Atomically write content to file using temp file."""
      await _async_mkdir(path.parent, parents=True, exist_ok=True)

      # Write to temp file in same directory, then move atomically
      # Use thread to avoid blocking event loop on sync I/O
      def _do_atomic_write() -> None:
         fd, temp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
         try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(temp_path, path)
         except Exception:
            os.close(fd)
            try:
               os.unlink(temp_path)
            except OSError:
               pass
            raise

      await trio.to_thread.run_sync(_do_atomic_write)

   async def _write_schema(
      self, dir_path: Path, schema: dict[str, Any], result: PullResult
   ) -> None:
      """Write _schema.yaml for a database directory.

      Creates/updates the schema file and tracks it in sync state for conflict detection.
      """
      schema_path = self.workspace_path / dir_path / SCHEMA_FILENAME
      schema_content = schema_to_yaml(schema)
      schema_hash = _compute_hash(schema_content)
      rel_path = str(dir_path / SCHEMA_FILENAME)

      # Check if we already track this schema
      existing_entry = await self.state.get_entry(rel_path)

      # Check if schema needs updating
      if await _async_exists(schema_path):
         existing = await _async_read_text(schema_path)
         if existing == schema_content:
            return  # No change

         # Schema changed - check for local modifications
         if existing_entry:
            existing_hash = _compute_hash(existing)
            if existing_hash != existing_entry.local_hash:
               # Local schema was modified, remote also changed → conflict
               existing_entry.status = SyncStatus.conflict
               existing_entry.remote_hash = schema_hash
               await self.state.set_entry(existing_entry)
               result.conflicts.append(rel_path)
               logger.warning("Schema conflict: %s", rel_path)
               return

      await self._write_page(schema_path, schema_content)

      # Track schema in sync state
      if existing_entry is None:
         # New schema file
         new_entry = SyncEntry(
            path=rel_path,
            notion_id=f"schema:{dir_path}",  # Synthetic ID for schemas
            notion_url="",
            notion_parent_id=None,
            is_directory=False,
            local_hash=schema_hash,
            remote_hash=schema_hash,
            local_mtime=datetime.now(timezone.utc),
            remote_mtime=datetime.now(timezone.utc),
            status=SyncStatus.clean,
         )
         await self.state.set_entry(new_entry)
         result.created.append(rel_path)
      else:
         # Updated schema
         existing_entry.local_hash = schema_hash
         existing_entry.remote_hash = schema_hash
         existing_entry.local_mtime = datetime.now(timezone.utc)
         existing_entry.remote_mtime = datetime.now(timezone.utc)
         existing_entry.status = SyncStatus.clean
         await self.state.set_entry(existing_entry)
         result.updated.append(rel_path)

      logger.debug("Wrote schema: %s", rel_path)

   async def _load_schema(self, dir_path: Path) -> dict[str, Any]:
      """Load schema from _schema.yaml in a directory.

      Args:
          dir_path: Directory path relative to workspace.

      Returns:
          Schema dict in Notion API format, or empty dict if not found.
      """
      schema_path = self.workspace_path / dir_path / SCHEMA_FILENAME
      if not await _async_exists(schema_path):
         return {}

      try:
         content = await _async_read_text(schema_path)
         parsed = parse_schema(content)
         return schema_to_notion_format(parsed)
      except Exception as e:
         logger.warning("Failed to load schema from %s: %s", schema_path, e)
         return {}
