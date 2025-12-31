"""Tests for sync state management."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from notionfs.sync.state import SyncEntry, SyncState, SyncStatus


@pytest.fixture
def sync_state(tmp_cache_dir: Path) -> SyncState:
   """Create a SyncState instance with temporary database."""
   return SyncState(tmp_cache_dir / "sync.db")


# --- SyncEntry creation and serialization ---


class TestSyncEntry:
   """Tests for SyncEntry dataclass."""

   def test_create_minimal_entry(self) -> None:
      """Create entry with minimal required fields."""
      entry = SyncEntry(
         path="test.md",
         notion_id="abc123",
         notion_url="https://notion.so/abc123",
         notion_parent_id=None,
         is_directory=False,
         remote_hash=None,
         remote_mtime=None,
         status=SyncStatus.clean,
      )
      assert entry.path == "test.md"
      assert entry.notion_id == "abc123"
      assert entry.status == SyncStatus.clean
      assert not entry.is_directory

   def test_create_full_entry(self) -> None:
      """Create entry with all fields populated."""
      now = datetime.now(timezone.utc)
      entry = SyncEntry(
         path="folder/_index.md",
         notion_id="def456",
         notion_url="https://notion.so/def456",
         notion_parent_id="parent-id",
         is_directory=True,
         remote_hash="sha256-remote",
         remote_mtime=now,
         status=SyncStatus.remote_modified,
      )
      assert entry.path == "folder/_index.md"
      assert entry.is_directory
      assert entry.remote_hash == "sha256-remote"
      assert entry.status == SyncStatus.remote_modified


class TestSyncStatus:
   """Tests for SyncStatus enum."""

   def test_all_statuses_exist(self) -> None:
      """Verify all expected sync statuses are defined."""
      expected = {
         "clean",
         "remote_modified",
         "conflict",
         "deleted_local",
         "deleted_remote",
      }
      actual = {s.value for s in SyncStatus}
      assert actual == expected

   def test_status_string_values(self) -> None:
      """Verify status string values match their names."""
      for status in SyncStatus:
         assert status.value == status.name


# --- SyncState CRUD operations ---


@pytest.mark.trio
class TestSyncStateCRUD:
   """Tests for SyncState database operations."""

   @pytest.fixture
   async def initialized_state(self, sync_state: SyncState) -> SyncState:
      """Initialize the sync state."""
      await sync_state.initialize()
      yield sync_state
      await sync_state.close()

   async def test_initialize_creates_database(
      self, tmp_cache_dir: Path, sync_state: SyncState
   ) -> None:
      """Initialize creates database file and schema."""
      await sync_state.initialize()
      assert sync_state.db_path.exists()
      await sync_state.close()

   async def test_set_and_get_entry(self, initialized_state: SyncState) -> None:
      """Set an entry and retrieve it by path."""
      entry = SyncEntry(
         path="page.md",
         notion_id="id-123",
         notion_url="https://notion.so/id-123",
         notion_parent_id=None,
         is_directory=False,
         remote_hash="abc",
         remote_mtime=datetime.now(timezone.utc),
         status=SyncStatus.clean,
      )
      await initialized_state.set_entry(entry)

      retrieved = await initialized_state.get_entry("page.md")
      assert retrieved is not None
      assert retrieved.path == "page.md"
      assert retrieved.notion_id == "id-123"
      assert retrieved.status == SyncStatus.clean

   async def test_get_entry_not_found(self, initialized_state: SyncState) -> None:
      """Get entry returns None for non-existent path."""
      result = await initialized_state.get_entry("nonexistent.md")
      assert result is None

   async def test_get_entry_by_notion_id(self, initialized_state: SyncState) -> None:
      """Retrieve entry by Notion ID."""
      entry = SyncEntry(
         path="test.md",
         notion_id="notion-id-xyz",
         notion_url="https://notion.so/xyz",
         notion_parent_id=None,
         is_directory=False,
         remote_hash=None,
         remote_mtime=None,
         status=SyncStatus.clean,
      )
      await initialized_state.set_entry(entry)

      retrieved = await initialized_state.get_entry_by_notion_id("notion-id-xyz")
      assert retrieved is not None
      assert retrieved.path == "test.md"

   async def test_update_existing_entry(self, initialized_state: SyncState) -> None:
      """Update existing entry with set_entry."""
      entry = SyncEntry(
         path="update-me.md",
         notion_id="up-123",
         notion_url="https://notion.so/up-123",
         notion_parent_id=None,
         is_directory=False,
         remote_hash="old-hash",
         remote_mtime=None,
         status=SyncStatus.clean,
      )
      await initialized_state.set_entry(entry)

      # Update the entry
      entry.remote_hash = "new-hash"
      entry.status = SyncStatus.remote_modified
      await initialized_state.set_entry(entry)

      retrieved = await initialized_state.get_entry("update-me.md")
      assert retrieved is not None
      assert retrieved.remote_hash == "new-hash"
      assert retrieved.status == SyncStatus.remote_modified

   async def test_delete_entry(self, initialized_state: SyncState) -> None:
      """Delete entry by path."""
      entry = SyncEntry(
         path="delete-me.md",
         notion_id="del-123",
         notion_url="https://notion.so/del-123",
         notion_parent_id=None,
         is_directory=False,
         remote_hash=None,
         remote_mtime=None,
         status=SyncStatus.clean,
      )
      await initialized_state.set_entry(entry)
      await initialized_state.delete_entry("delete-me.md")

      result = await initialized_state.get_entry("delete-me.md")
      assert result is None

   async def test_list_entries(self, initialized_state: SyncState) -> None:
      """List all entries."""
      for i in range(3):
         entry = SyncEntry(
            path=f"file{i}.md",
            notion_id=f"id-{i}",
            notion_url=f"https://notion.so/id-{i}",
            notion_parent_id=None,
            is_directory=False,
            remote_hash=None,
            remote_mtime=None,
            status=SyncStatus.clean,
         )
         await initialized_state.set_entry(entry)

      entries = await initialized_state.list_entries()
      assert len(entries) == 3
      # Sorted by path
      assert [e.path for e in entries] == ["file0.md", "file1.md", "file2.md"]

   async def test_list_by_status(self, initialized_state: SyncState) -> None:
      """List entries filtered by status."""
      statuses = [SyncStatus.clean, SyncStatus.remote_modified, SyncStatus.conflict]
      for i, status in enumerate(statuses):
         entry = SyncEntry(
            path=f"entry{i}.md",
            notion_id=f"id-{i}",
            notion_url=f"https://notion.so/id-{i}",
            notion_parent_id=None,
            is_directory=False,
            remote_hash=None,
            remote_mtime=None,
            status=status,
         )
         await initialized_state.set_entry(entry)

      modified = await initialized_state.list_by_status(SyncStatus.remote_modified)
      assert len(modified) == 1
      assert modified[0].path == "entry1.md"

   async def test_list_by_statuses(self, initialized_state: SyncState) -> None:
      """List entries with any of multiple statuses."""
      statuses = [
         SyncStatus.clean,
         SyncStatus.remote_modified,
         SyncStatus.conflict,
         SyncStatus.deleted_local,
      ]
      for i, status in enumerate(statuses):
         entry = SyncEntry(
            path=f"multi{i}.md",
            notion_id=f"id-{i}",
            notion_url=f"https://notion.so/id-{i}",
            notion_parent_id=None,
            is_directory=False,
            remote_hash=None,
            remote_mtime=None,
            status=status,
         )
         await initialized_state.set_entry(entry)

      # Query for multiple statuses
      pending = await initialized_state.list_by_statuses([
         SyncStatus.remote_modified,
         SyncStatus.conflict,
      ])
      assert len(pending) == 2
      paths = {e.path for e in pending}
      assert paths == {"multi1.md", "multi2.md"}

   async def test_list_by_statuses_empty(self, initialized_state: SyncState) -> None:
      """List by statuses with empty list returns empty."""
      result = await initialized_state.list_by_statuses([])
      assert result == []

   async def test_list_children(self, initialized_state: SyncState) -> None:
      """List entries by parent Notion ID."""
      parent_id = "parent-abc"
      # Create parent
      parent = SyncEntry(
         path="parent/_index.md",
         notion_id=parent_id,
         notion_url="https://notion.so/parent-abc",
         notion_parent_id=None,
         is_directory=True,
         remote_hash=None,
         remote_mtime=None,
         status=SyncStatus.clean,
      )
      await initialized_state.set_entry(parent)

      # Create children
      for i in range(2):
         child = SyncEntry(
            path=f"parent/child{i}.md",
            notion_id=f"child-{i}",
            notion_url=f"https://notion.so/child-{i}",
            notion_parent_id=parent_id,
            is_directory=False,
            remote_hash=None,
            remote_mtime=None,
            status=SyncStatus.clean,
         )
         await initialized_state.set_entry(child)

      children = await initialized_state.list_children(parent_id)
      assert len(children) == 2

   async def test_count(self, initialized_state: SyncState) -> None:
      """Count total entries."""
      assert await initialized_state.count() == 0

      for i in range(5):
         entry = SyncEntry(
            path=f"count{i}.md",
            notion_id=f"id-{i}",
            notion_url=f"https://notion.so/id-{i}",
            notion_parent_id=None,
            is_directory=False,
            remote_hash=None,
            remote_mtime=None,
            status=SyncStatus.clean,
         )
         await initialized_state.set_entry(entry)

      assert await initialized_state.count() == 5

   async def test_count_by_status(self, initialized_state: SyncState) -> None:
      """Count entries with specific status."""
      # Create entries with mixed statuses
      for i in range(3):
         entry = SyncEntry(
            path=f"clean{i}.md",
            notion_id=f"clean-{i}",
            notion_url=f"https://notion.so/clean-{i}",
            notion_parent_id=None,
            is_directory=False,
            remote_hash=None,
            remote_mtime=None,
            status=SyncStatus.clean,
         )
         await initialized_state.set_entry(entry)

      entry = SyncEntry(
         path="modified.md",
         notion_id="modified-1",
         notion_url="https://notion.so/modified-1",
         notion_parent_id=None,
         is_directory=False,
         remote_hash=None,
         remote_mtime=None,
         status=SyncStatus.remote_modified,
      )
      await initialized_state.set_entry(entry)

      assert await initialized_state.count_by_status(SyncStatus.clean) == 3
      assert await initialized_state.count_by_status(SyncStatus.remote_modified) == 1
      assert await initialized_state.count_by_status(SyncStatus.conflict) == 0

   async def test_clear(self, initialized_state: SyncState) -> None:
      """Clear all entries."""
      for i in range(3):
         entry = SyncEntry(
            path=f"clear{i}.md",
            notion_id=f"id-{i}",
            notion_url=f"https://notion.so/id-{i}",
            notion_parent_id=None,
            is_directory=False,
            remote_hash=None,
            remote_mtime=None,
            status=SyncStatus.clean,
         )
         await initialized_state.set_entry(entry)

      await initialized_state.clear()
      assert await initialized_state.count() == 0


# --- Status transitions ---


@pytest.mark.trio
class TestStatusTransitions:
   """Tests for status update operations."""

   @pytest.fixture
   async def initialized_state(self, sync_state: SyncState) -> SyncState:
      """Initialize the sync state."""
      await sync_state.initialize()
      yield sync_state
      await sync_state.close()

   async def test_update_status(self, initialized_state: SyncState) -> None:
      """Update only the status field."""
      entry = SyncEntry(
         path="status-test.md",
         notion_id="st-123",
         notion_url="https://notion.so/st-123",
         notion_parent_id=None,
         is_directory=False,
         remote_hash="hash",
         remote_mtime=None,
         status=SyncStatus.clean,
      )
      await initialized_state.set_entry(entry)

      await initialized_state.update_status("status-test.md", SyncStatus.conflict)

      updated = await initialized_state.get_entry("status-test.md")
      assert updated is not None
      assert updated.status == SyncStatus.conflict
      # Other fields unchanged
      assert updated.remote_hash == "hash"

   async def test_update_remote_hash(self, initialized_state: SyncState) -> None:
      """Update remote hash field."""
      entry = SyncEntry(
         path="hash-test.md",
         notion_id="ht-123",
         notion_url="https://notion.so/ht-123",
         notion_parent_id=None,
         is_directory=False,
         remote_hash="old-remote",
         remote_mtime=None,
         status=SyncStatus.clean,
      )
      await initialized_state.set_entry(entry)

      # Update remote hash
      await initialized_state.update_remote_hash("hash-test.md", remote_hash="new-remote")
      updated = await initialized_state.get_entry("hash-test.md")
      assert updated is not None
      assert updated.remote_hash == "new-remote"


# --- Datetime serialization ---


@pytest.mark.trio
class TestDatetimeSerialization:
   """Tests for datetime round-trip through database."""

   @pytest.fixture
   async def initialized_state(self, sync_state: SyncState) -> SyncState:
      """Initialize the sync state."""
      await sync_state.initialize()
      yield sync_state
      await sync_state.close()

   async def test_datetime_roundtrip(self, initialized_state: SyncState) -> None:
      """Datetime values survive database round-trip."""
      now = datetime.now(timezone.utc)
      entry = SyncEntry(
         path="datetime.md",
         notion_id="dt-123",
         notion_url="https://notion.so/dt-123",
         notion_parent_id=None,
         is_directory=False,
         remote_hash=None,
         remote_mtime=now,
         status=SyncStatus.clean,
      )
      await initialized_state.set_entry(entry)

      retrieved = await initialized_state.get_entry("datetime.md")
      assert retrieved is not None
      assert retrieved.remote_mtime is not None
      # Compare ISO strings to avoid microsecond precision issues
      assert retrieved.remote_mtime.isoformat() == now.isoformat()

   async def test_none_datetime(self, initialized_state: SyncState) -> None:
      """None datetime values are preserved."""
      entry = SyncEntry(
         path="nodatetime.md",
         notion_id="ndt-123",
         notion_url="https://notion.so/ndt-123",
         notion_parent_id=None,
         is_directory=False,
         remote_hash=None,
         remote_mtime=None,
         status=SyncStatus.clean,
      )
      await initialized_state.set_entry(entry)

      retrieved = await initialized_state.get_entry("nodatetime.md")
      assert retrieved is not None
      assert retrieved.remote_mtime is None
