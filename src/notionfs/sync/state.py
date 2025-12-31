"""Sync state tracking with SQLite backend."""

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import trio

logger = logging.getLogger(__name__)


class SyncStatus(Enum):
   """Sync status for tracked entries."""

   clean = "clean"  # Local and remote in sync
   local_modified = "local_modified"  # Local changes pending push
   remote_modified = "remote_modified"  # Remote changes pending pull
   conflict = "conflict"  # Both modified, needs resolution
   deleted_local = "deleted_local"  # Deleted locally, exists remotely
   deleted_remote = "deleted_remote"  # Exists locally, deleted remotely


@dataclass
class SyncEntry:
   """Represents sync state for a single file/directory."""

   path: str  # Relative path from workspace root (primary key)
   notion_id: str  # Notion page/database ID
   notion_url: str  # Full Notion URL
   notion_parent_id: str | None  # Parent ID for tree structure
   is_directory: bool  # Page with children or database
   local_hash: str | None  # SHA256 of local content at last sync
   remote_hash: str | None  # SHA256 of remote content at last sync
   local_mtime: datetime | None  # Local file mtime at last sync
   remote_mtime: datetime | None  # Notion's last_edited_time at last sync
   status: SyncStatus  # Current sync status


SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    path TEXT PRIMARY KEY,
    notion_id TEXT NOT NULL,
    notion_url TEXT NOT NULL,
    notion_parent_id TEXT,
    is_directory INTEGER NOT NULL DEFAULT 0,
    local_hash TEXT,
    remote_hash TEXT,
    local_mtime TEXT,
    remote_mtime TEXT,
    status TEXT NOT NULL DEFAULT 'clean'
);

CREATE INDEX IF NOT EXISTS idx_entries_notion_id ON entries(notion_id);
CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);
CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(notion_parent_id);
"""


class SyncState:
   """SQLite-backed sync state management."""

   def __init__(self, db_path: Path):
      """Initialize sync state.

      Args:
          db_path: Path to the SQLite database file.
      """
      self.db_path = db_path
      self._conn: sqlite3.Connection | None = None
      self._db_lock = threading.Lock()

   async def _run_sync(self, fn: Any, *args: Any) -> Any:
      """Run blocking DB operation in thread with lock protection."""

      def _locked_fn(*a: Any) -> Any:
         with self._db_lock:
            return fn(*a)

      return await trio.to_thread.run_sync(partial(_locked_fn, *args))

   def _connect_sync(self) -> None:
      """Connect to database (sync, called via run_sync)."""
      self.db_path.parent.mkdir(parents=True, exist_ok=True)
      conn = sqlite3.connect(self.db_path, check_same_thread=False)
      conn.row_factory = sqlite3.Row
      conn.execute("PRAGMA journal_mode=WAL;")
      conn.execute("PRAGMA busy_timeout=5000;")
      conn.executescript(SCHEMA)
      conn.commit()
      self._conn = conn

   def _require_conn(self) -> sqlite3.Connection:
      """Get connection or raise if not initialized."""
      if self._conn is None:
         raise RuntimeError("SyncState not initialized - call initialize() first")
      return self._conn

   async def initialize(self) -> None:
      """Initialize the database connection and schema."""
      await self._run_sync(self._connect_sync)
      logger.info(f"Sync state initialized at {self.db_path}")

   async def close(self) -> None:
      """Close the database connection."""
      if self._conn:

         def _close() -> None:
            conn = self._conn
            self._conn = None  # Clear before close, under lock
            if conn:
               conn.close()

         await self._run_sync(_close)

   async def __aenter__(self) -> 'SyncState':
      await self.initialize()
      return self

   async def __aexit__(
      self,
      exc_type: type[BaseException] | None,
      exc_val: BaseException | None,
      exc_tb: TracebackType | None,
   ) -> None:
      await self.close()

   async def get_entry(self, path: str) -> SyncEntry | None:
      """Get entry by path.

      Args:
          path: Relative path from workspace root.

      Returns:
          SyncEntry if found, None otherwise.
      """
      conn = self._require_conn()

      def _query() -> sqlite3.Row | None:
         cursor = conn.execute("SELECT * FROM entries WHERE path = ?", (path,))
         return cast(sqlite3.Row | None, cursor.fetchone())

      row = await self._run_sync(_query)
      return self._row_to_entry(row) if row else None

   async def get_entry_by_notion_id(self, notion_id: str) -> SyncEntry | None:
      """Get entry by Notion ID.

      Args:
          notion_id: Notion page or database ID.

      Returns:
          SyncEntry if found, None otherwise.
      """
      conn = self._require_conn()

      def _query() -> sqlite3.Row | None:
         cursor = conn.execute("SELECT * FROM entries WHERE notion_id = ?", (notion_id,))
         return cast(sqlite3.Row | None, cursor.fetchone())

      row = await self._run_sync(_query)
      return self._row_to_entry(row) if row else None

   async def set_entry(self, entry: SyncEntry) -> None:
      """Create or update an entry.

      Args:
          entry: SyncEntry to store.
      """
      conn = self._require_conn()

      def _upsert() -> None:
         conn.execute(
            """
            INSERT INTO entries (
                path, notion_id, notion_url, notion_parent_id, is_directory,
                local_hash, remote_hash, local_mtime, remote_mtime, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                notion_id = excluded.notion_id,
                notion_url = excluded.notion_url,
                notion_parent_id = excluded.notion_parent_id,
                is_directory = excluded.is_directory,
                local_hash = excluded.local_hash,
                remote_hash = excluded.remote_hash,
                local_mtime = excluded.local_mtime,
                remote_mtime = excluded.remote_mtime,
                status = excluded.status
            """,
            (
               entry.path,
               entry.notion_id,
               entry.notion_url,
               entry.notion_parent_id,
               1 if entry.is_directory else 0,
               entry.local_hash,
               entry.remote_hash,
               entry.local_mtime.isoformat() if entry.local_mtime else None,
               entry.remote_mtime.isoformat() if entry.remote_mtime else None,
               entry.status.value,
            ),
         )
         conn.commit()

      await self._run_sync(_upsert)

   async def delete_entry(self, path: str) -> None:
      """Delete an entry by path.

      Args:
          path: Relative path from workspace root.
      """
      conn = self._require_conn()

      def _delete() -> None:
         conn.execute("DELETE FROM entries WHERE path = ?", (path,))
         conn.commit()

      await self._run_sync(_delete)

   async def list_entries(self) -> list[SyncEntry]:
      """Get all entries.

      Returns:
          List of all SyncEntry objects.
      """
      conn = self._require_conn()

      def _query() -> list[sqlite3.Row]:
         cursor = conn.execute("SELECT * FROM entries ORDER BY path")
         return cursor.fetchall()

      rows = await self._run_sync(_query)
      return [self._row_to_entry(row) for row in rows]

   async def list_by_status(self, status: SyncStatus) -> list[SyncEntry]:
      """Get entries with a specific status.

      Args:
          status: SyncStatus to filter by.

      Returns:
          List of SyncEntry objects with matching status.
      """
      conn = self._require_conn()

      def _query() -> list[sqlite3.Row]:
         cursor = conn.execute(
            "SELECT * FROM entries WHERE status = ? ORDER BY path",
            (status.value,),
         )
         return cursor.fetchall()

      rows = await self._run_sync(_query)
      return [self._row_to_entry(row) for row in rows]

   async def list_by_statuses(self, statuses: list[SyncStatus]) -> list[SyncEntry]:
      """Get entries with any of the specified statuses.

      Args:
          statuses: List of SyncStatus values to filter by.

      Returns:
          List of SyncEntry objects with matching status.
      """
      if not statuses:
         return []

      conn = self._require_conn()
      placeholders = ",".join("?" * len(statuses))
      status_values = [s.value for s in statuses]

      def _query() -> list[sqlite3.Row]:
         cursor = conn.execute(
            f"SELECT * FROM entries WHERE status IN ({placeholders}) ORDER BY path",
            status_values,
         )
         return cursor.fetchall()

      rows = await self._run_sync(_query)
      return [self._row_to_entry(row) for row in rows]

   async def get_modified(self) -> list[SyncEntry]:
      """Get entries with local_modified status."""
      return await self.list_by_status(SyncStatus.local_modified)

   async def get_conflicts(self) -> list[SyncEntry]:
      """Get entries with conflict status."""
      return await self.list_by_status(SyncStatus.conflict)

   async def list_children(self, parent_notion_id: str) -> list[SyncEntry]:
      """Get entries with a specific parent.

      Args:
          parent_notion_id: Notion ID of the parent.

      Returns:
          List of SyncEntry objects with matching parent.
      """
      conn = self._require_conn()

      def _query() -> list[sqlite3.Row]:
         cursor = conn.execute(
            "SELECT * FROM entries WHERE notion_parent_id = ? ORDER BY path",
            (parent_notion_id,),
         )
         return cursor.fetchall()

      rows = await self._run_sync(_query)
      return [self._row_to_entry(row) for row in rows]

   async def update_status(self, path: str, status: SyncStatus) -> None:
      """Update only the status of an entry.

      Args:
          path: Relative path from workspace root.
          status: New SyncStatus.
      """
      conn = self._require_conn()

      def _update() -> None:
         conn.execute(
            "UPDATE entries SET status = ? WHERE path = ?",
            (status.value, path),
         )
         conn.commit()

      await self._run_sync(_update)

   async def update_hashes(
      self,
      path: str,
      local_hash: str | None = None,
      remote_hash: str | None = None,
   ) -> None:
      """Update hash values for an entry.

      Args:
          path: Relative path from workspace root.
          local_hash: New local hash (None = don't update).
          remote_hash: New remote hash (None = don't update).
      """
      conn = self._require_conn()

      updates = []
      params: list[Any] = []

      if local_hash is not None:
         updates.append("local_hash = ?")
         params.append(local_hash)
      if remote_hash is not None:
         updates.append("remote_hash = ?")
         params.append(remote_hash)

      if not updates:
         return

      params.append(path)

      def _update() -> None:
         conn.execute(
            f"UPDATE entries SET {', '.join(updates)} WHERE path = ?",
            params,
         )
         conn.commit()

      await self._run_sync(_update)

   async def clear(self) -> None:
      """Delete all entries. Use with caution."""
      conn = self._require_conn()

      def _clear() -> None:
         conn.execute("DELETE FROM entries")
         conn.commit()

      await self._run_sync(_clear)

   async def count(self) -> int:
      """Get total number of entries.

      Returns:
          Number of entries in the database.
      """
      conn = self._require_conn()

      def _count() -> int:
         cursor = conn.execute("SELECT COUNT(*) FROM entries")
         row = cursor.fetchone()
         return int(row[0]) if row else 0

      result: int = await self._run_sync(_count)
      return result

   async def count_by_status(self, status: SyncStatus) -> int:
      """Get number of entries with a specific status.

      Args:
          status: SyncStatus to count.

      Returns:
          Number of entries with matching status.
      """
      conn = self._require_conn()

      def _count() -> int:
         cursor = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE status = ?",
            (status.value,),
         )
         row = cursor.fetchone()
         return int(row[0]) if row else 0

      result: int = await self._run_sync(_count)
      return result

   def _row_to_entry(self, row: sqlite3.Row) -> SyncEntry:
      """Convert a database row to a SyncEntry."""
      return SyncEntry(
         path=row["path"],
         notion_id=row["notion_id"],
         notion_url=row["notion_url"],
         notion_parent_id=row["notion_parent_id"],
         is_directory=bool(row["is_directory"]),
         local_hash=row["local_hash"],
         remote_hash=row["remote_hash"],
         local_mtime=(
            datetime.fromisoformat(row["local_mtime"]) if row["local_mtime"] else None
         ),
         remote_mtime=(
            datetime.fromisoformat(row["remote_mtime"]) if row["remote_mtime"] else None
         ),
         status=SyncStatus(row["status"]),
      )
