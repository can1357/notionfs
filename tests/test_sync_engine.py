"""Tests for sync engine."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notionfs.sync.engine import (
   RemotePage,
   SyncEngine,
   _compute_hash,
   _desanitize_filename,
   _extract_title,
   _make_unique_filename,
   _normalize_unicode,
   _parse_datetime,
   _sanitize_filename,
)
from notionfs.sync.state import SyncEntry, SyncState, SyncStatus

# --- Utility function tests ---


class TestSanitizeFilename:
   """Tests for filename sanitization."""

   def test_normal_filename(self) -> None:
      """Normal filenames pass through unchanged."""
      assert _sanitize_filename("My Page") == "My Page"
      assert _sanitize_filename("test-file") == "test-file"

   def test_forbidden_chars_replaced(self) -> None:
      """Forbidden characters are replaced with look-alikes."""
      assert "/" not in _sanitize_filename("path/with/slashes")
      assert ":" not in _sanitize_filename("time:stamp")
      assert "?" not in _sanitize_filename("question?")
      assert "*" not in _sanitize_filename("star*")
      assert "<" not in _sanitize_filename("<tag>")
      assert ">" not in _sanitize_filename("<tag>")

   def test_empty_title(self) -> None:
      """Empty title becomes 'Untitled'."""
      assert _sanitize_filename("") == "Untitled"
      assert _sanitize_filename("   ") == "Untitled"

   def test_unicode_normalization(self) -> None:
      """Titles are normalized to NFC form."""
      # NFD form: e + combining acute accent
      nfd = "cafe\u0301"
      # NFC form: precomposed é
      nfc = "café"
      assert _sanitize_filename(nfd) == nfc

   def test_emoji_preserved(self) -> None:
      """Emoji in titles are preserved."""
      assert "🚀" in _sanitize_filename("Launch 🚀 Day")
      assert "📝" in _sanitize_filename("📝 Notes")


class TestDesanitizeFilename:
   """Tests for filename desanitization (reversibility)."""

   def test_roundtrip(self) -> None:
      """Sanitize then desanitize recovers original title."""
      titles = [
         "path/with/slashes",
         "time:stamp",
         "question?",
         "star*wild",
         '"quoted"',
         "<tag>",
         "pipe|char",
         "back\\slash",
      ]
      for title in titles:
         sanitized = _sanitize_filename(title)
         recovered = _desanitize_filename(sanitized)
         assert recovered == title, f"Failed roundtrip for {title!r}"

   def test_strips_duplicate_suffix(self) -> None:
      """Desanitize removes duplicate suffix like ' (2)'."""
      assert _desanitize_filename("My Page (2)") == "My Page"
      assert _desanitize_filename("Test (10)") == "Test"
      assert _desanitize_filename("No Suffix") == "No Suffix"


class TestMakeUniqueFilename:
   """Tests for duplicate filename handling."""

   def test_no_conflict(self) -> None:
      """Returns base name when no conflict."""
      used: set[str] = {"other", "files"}
      assert _make_unique_filename("new", used) == "new"

   def test_adds_suffix_on_conflict(self) -> None:
      """Adds (2) suffix when base name exists."""
      used: set[str] = {"existing"}
      assert _make_unique_filename("existing", used) == "existing (2)"

   def test_increments_suffix(self) -> None:
      """Increments suffix number for multiple conflicts."""
      used: set[str] = {"page", "page (2)", "page (3)"}
      assert _make_unique_filename("page", used) == "page (4)"

   def test_empty_used_set(self) -> None:
      """Works with empty used set."""
      assert _make_unique_filename("any", set()) == "any"


class TestNormalizeUnicode:
   """Tests for Unicode normalization."""

   def test_nfc_normalization(self) -> None:
      """Normalizes to NFC form."""
      # NFD: e + combining acute
      nfd = "e\u0301"
      result = _normalize_unicode(nfd)
      assert result == "é"
      assert len(result) == 1  # NFC is single codepoint

   def test_already_nfc(self) -> None:
      """Already-NFC text unchanged."""
      text = "café"
      assert _normalize_unicode(text) == text


class TestComputeHash:
   """Tests for content hashing."""

   def test_hash_deterministic(self) -> None:
      """Same content produces same hash."""
      content = "Hello, World!"
      assert _compute_hash(content) == _compute_hash(content)

   def test_hash_different_content(self) -> None:
      """Different content produces different hash."""
      assert _compute_hash("Hello") != _compute_hash("World")

   def test_hash_format(self) -> None:
      """Hash is a 64-character hex string (SHA256)."""
      h = _compute_hash("test")
      assert len(h) == 64
      assert all(c in "0123456789abcdef" for c in h)


class TestExtractTitle:
   """Tests for title extraction from Notion page objects."""

   def test_title_property(self) -> None:
      """Extract from 'title' property."""
      page: dict[str, Any] = {
         "properties": {
            "title": {
               "type": "title",
               "title": [
                  {"plain_text": "Hello"},
                  {"plain_text": " World"},
               ]
            }
         }
      }
      assert _extract_title(page) == "Hello World"

   def test_name_property(self) -> None:
      """Extract from 'Name' property (database entry)."""
      page: dict[str, Any] = {
         "properties": {
            "Name": {
               "type": "title",
               "title": [{"plain_text": "Database Entry"}]
            }
         }
      }
      assert _extract_title(page) == "Database Entry"

   def test_no_title(self) -> None:
      """Returns 'Untitled' when no title found."""
      assert _extract_title({}) == "Untitled"
      assert _extract_title({"properties": {}}) == "Untitled"


class TestParseDatetime:
   """Tests for datetime parsing."""

   def test_iso_with_z(self) -> None:
      """Parse ISO datetime with Z suffix."""
      dt = _parse_datetime("2024-01-15T10:30:00.000Z")
      assert dt is not None
      assert dt.year == 2024
      assert dt.month == 1
      assert dt.day == 15

   def test_iso_with_offset(self) -> None:
      """Parse ISO datetime with timezone offset."""
      dt = _parse_datetime("2024-01-15T10:30:00+00:00")
      assert dt is not None

   def test_none_input(self) -> None:
      """None input returns None."""
      assert _parse_datetime(None) is None

   def test_invalid_format(self) -> None:
      """Invalid format returns None."""
      assert _parse_datetime("not-a-date") is None


# --- Mock fixtures ---


@pytest.fixture
def mock_api_client() -> MagicMock:
   """Create a mock NotionAPIClient."""
   client = MagicMock()
   client.get_page = AsyncMock()
   client.get_page_metadata = AsyncMock()
   client.get_database = AsyncMock()
   client.query_database = AsyncMock()
   client.get_block_children = AsyncMock()
   client.create_page = AsyncMock()
   client.update_page_properties = AsyncMock()
   client.update_blocks = AsyncMock()
   client.archive_page = AsyncMock()
   return client


@pytest.fixture
def sync_state(tmp_cache_dir: Path) -> SyncState:
   """Create a SyncState instance."""
   return SyncState(tmp_cache_dir / "sync.db")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
   """Create a temporary workspace directory with git initialized."""
   import git
   ws = tmp_path / "workspace"
   ws.mkdir()
   # Initialize git for the workspace (required for change detection)
   repo = git.Repo.init(ws)
   repo.config_writer().set_value("user", "email", "test@test.com").release()
   repo.config_writer().set_value("user", "name", "Test").release()
   # Create .gitignore and initial commit
   (ws / ".gitignore").write_text(".notionfs/\n")
   repo.index.add("*")
   repo.index.commit("init")
   return ws


@pytest.fixture
async def engine(
   workspace: Path,
   mock_api_client: MagicMock,
   sync_state: SyncState,
) -> SyncEngine:
   """Create a SyncEngine with mocked dependencies."""
   await sync_state.initialize()
   eng = SyncEngine(
      workspace_path=workspace,
      root_id="root-id-123",
      api_client=mock_api_client,
      state=sync_state,
   )
   yield eng
   await sync_state.close()


# --- Pull tests ---


@pytest.mark.trio
class TestPull:
   """Tests for pull operation."""

   async def test_pull_creates_new_file(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
      workspace: Path,
   ) -> None:
      """Pull creates local file from remote page."""
      # get_page_metadata returns just the page object (for mtime check)
      mock_api_client.get_page_metadata.return_value = {
         "id": "page-123",
         "url": "https://notion.so/page-123",
         "parent": {"page_id": "root-id-123"},
         "last_edited_time": "2024-01-15T10:00:00.000Z",
         "properties": {
            "title": {"type": "title", "title": [{"plain_text": "Test Page"}]}
         },
      }
      # get_block_children returns the blocks
      mock_api_client.get_block_children.return_value = [
         {
            "type": "paragraph",
            "paragraph": {
               "rich_text": [{"type": "text", "text": {"content": "Hello"}}]
            },
         }
      ]
      # get_page is still used by push operations
      mock_api_client.get_page.return_value = {
         "page": mock_api_client.get_page_metadata.return_value,
         "blocks": mock_api_client.get_block_children.return_value,
      }

      result = await engine.pull()

      assert len(result.created) == 1
      assert "Test Page.md" in result.created[0]
      assert (workspace / "Test Page.md").exists()
      content = (workspace / "Test Page.md").read_text()
      assert "Hello" in content

   async def test_pull_updates_changed_file(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
      workspace: Path,
      sync_state: SyncState,
   ) -> None:
      """Pull updates local file when remote has changed."""
      # Create initial entry - notion_id must match root_id used by engine
      old_time = datetime(2024, 1, 10, tzinfo=timezone.utc)
      entry = SyncEntry(
         path="Test Page.md",
         notion_id="root-id-123",  # Must match engine's root_id
         notion_url="https://notion.so/root-id-123",
         notion_parent_id=None,
         is_directory=False,
         remote_hash=_compute_hash("Old content"),
         remote_mtime=old_time,
         status=SyncStatus.clean,
      )
      await sync_state.set_entry(entry)
      (workspace / "Test Page.md").write_text("Old content")

      # Remote has newer content
      mock_api_client.get_page_metadata.return_value = {
         "id": "root-id-123",
         "url": "https://notion.so/root-id-123",
         "parent": {"page_id": "parent-123"},
         "last_edited_time": "2024-01-15T10:00:00.000Z",  # Newer
         "properties": {
            "title": {"type": "title", "title": [{"plain_text": "Test Page"}]}
         },
      }
      mock_api_client.get_block_children.return_value = [
         {
            "type": "paragraph",
            "paragraph": {
               "rich_text": [{"type": "text", "text": {"content": "New content"}}]
            },
         }
      ]

      result = await engine.pull()

      assert len(result.updated) == 1
      content = (workspace / "Test Page.md").read_text()
      assert "New content" in content

   async def test_pull_detects_conflict(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
      workspace: Path,
      sync_state: SyncState,
   ) -> None:
      """Pull detects conflict when both local and remote changed."""
      old_time = datetime(2024, 1, 10, tzinfo=timezone.utc)
      old_hash = _compute_hash("Original")
      entry = SyncEntry(
         path="Test Page.md",
         notion_id="root-id-123",  # Must match engine's root_id
         notion_url="https://notion.so/root-id-123",
         notion_parent_id=None,
         is_directory=False,
         remote_hash=old_hash,
         remote_mtime=old_time,
         status=SyncStatus.clean,
      )
      await sync_state.set_entry(entry)
      # Local file has different content (modified locally)
      (workspace / "Test Page.md").write_text("Local changes")

      # Remote also changed
      mock_api_client.get_page_metadata.return_value = {
         "id": "root-id-123",
         "url": "https://notion.so/root-id-123",
         "parent": {"page_id": "parent-123"},
         "last_edited_time": "2024-01-15T10:00:00.000Z",
         "properties": {
            "title": {"type": "title", "title": [{"plain_text": "Test Page"}]}
         },
      }
      mock_api_client.get_block_children.return_value = [
         {
            "type": "paragraph",
            "paragraph": {
               "rich_text": [{"type": "text", "text": {"content": "Remote changes"}}]
            },
         }
      ]

      result = await engine.pull()

      assert len(result.conflicts) == 1
      # Local file unchanged on conflict
      content = (workspace / "Test Page.md").read_text()
      assert content == "Local changes"
      # Entry marked as conflict
      updated_entry = await sync_state.get_entry("Test Page.md")
      assert updated_entry is not None
      assert updated_entry.status == SyncStatus.conflict

   async def test_pull_force_overwrites_conflict(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
      workspace: Path,
      sync_state: SyncState,
   ) -> None:
      """Pull with force=True overwrites local changes."""
      old_time = datetime(2024, 1, 10, tzinfo=timezone.utc)
      old_hash = _compute_hash("Original")
      entry = SyncEntry(
         path="Test Page.md",
         notion_id="root-id-123",  # Must match engine's root_id
         notion_url="https://notion.so/root-id-123",
         notion_parent_id=None,
         is_directory=False,
         remote_hash=old_hash,
         remote_mtime=old_time,
         status=SyncStatus.clean,
      )
      await sync_state.set_entry(entry)
      (workspace / "Test Page.md").write_text("Local changes")

      mock_api_client.get_page_metadata.return_value = {
         "id": "root-id-123",
         "url": "https://notion.so/root-id-123",
         "parent": {"page_id": "parent-123"},
         "last_edited_time": "2024-01-15T10:00:00.000Z",
         "properties": {
            "title": {"type": "title", "title": [{"plain_text": "Test Page"}]}
         },
      }
      mock_api_client.get_block_children.return_value = [
         {
            "type": "paragraph",
            "paragraph": {
               "rich_text": [{"type": "text", "text": {"content": "Remote wins"}}]
            },
         }
      ]

      result = await engine.pull(force=True)

      assert len(result.updated) == 1
      assert len(result.conflicts) == 0
      content = (workspace / "Test Page.md").read_text()
      assert "Remote wins" in content

   async def test_pull_handles_remote_deletion(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
      workspace: Path,
      sync_state: SyncState,
   ) -> None:
      """Pull handles page deleted remotely."""
      # Entry exists but remote doesn't return it
      entry = SyncEntry(
         path="Deleted.md",
         notion_id="deleted-123",
         notion_url="https://notion.so/deleted-123",
         notion_parent_id="root-id-123",
         is_directory=False,
         remote_hash=_compute_hash("Content"),
         remote_mtime=datetime.now(timezone.utc),
         status=SyncStatus.clean,
      )
      await sync_state.set_entry(entry)
      (workspace / "Deleted.md").write_text("Content")

      # Root page has no children
      mock_api_client.get_page_metadata.return_value = {
         "id": "root-id-123",
         "url": "https://notion.so/root-id-123",
         "parent": {},
         "last_edited_time": "2024-01-15T10:00:00.000Z",
         "properties": {
            "title": {"type": "title", "title": [{"plain_text": "Root"}]}
         },
      }
      mock_api_client.get_block_children.return_value = []

      result = await engine.pull()

      assert len(result.deleted) == 1
      assert not (workspace / "Deleted.md").exists()

   async def test_pull_returns_errors_on_fetch_failure(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
   ) -> None:
      """Pull returns errors when fetch tree fails."""
      mock_api_client.get_page_metadata.side_effect = Exception("API error")

      result = await engine.pull()

      assert len(result.errors) == 1
      assert "Failed to fetch remote tree" in result.errors[0]
      assert len(result.created) == 0
      assert len(result.updated) == 0


# --- Push tests ---


@pytest.mark.trio
class TestPush:
   """Tests for push operation."""

   async def test_push_detects_local_changes(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
      workspace: Path,
      sync_state: SyncState,
   ) -> None:
      """Push detects and uploads local changes."""
      old_hash = _compute_hash("Old content")
      entry = SyncEntry(
         path="Modified.md",
         notion_id="mod-123",
         notion_url="https://notion.so/mod-123",
         notion_parent_id="root-id-123",
         is_directory=False,
         remote_hash=old_hash,
         remote_mtime=datetime.now(timezone.utc),
         status=SyncStatus.clean,
      )
      await sync_state.set_entry(entry)
      (workspace / "Modified.md").write_text("New local content")

      # Mock remote hasn't changed
      mock_api_client.get_page.return_value = {
         "page": {
            "id": "mod-123",
            "url": "https://notion.so/mod-123",
            "last_edited_time": entry.remote_mtime.isoformat() if entry.remote_mtime else "",
         },
         "blocks": [],
      }

      # Mock git status to report the file as modified
      with patch("notionfs.sync.engine._get_local_changes") as mock_git:
         mock_git.return_value = {"Modified.md": "M"}
         result = await engine.push()

      assert len(result.updated) == 1
      mock_api_client.update_blocks.assert_called_once()

   async def test_push_creates_new_page(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
      workspace: Path,
   ) -> None:
      """Push creates new page in Notion for untracked file."""
      (workspace / "New Page.md").write_text("# New Page\n\nContent here")

      mock_api_client.create_page.return_value = "new-page-id"
      mock_api_client.get_page.return_value = {
         "page": {
            "id": "new-page-id",
            "url": "https://notion.so/new-page-id",
            "last_edited_time": "2024-01-15T10:00:00.000Z",
         },
         "blocks": [],
      }

      result = await engine.push()

      assert len(result.created) == 1
      mock_api_client.create_page.assert_called_once()

   async def test_push_detects_remote_conflict(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
      workspace: Path,
      sync_state: SyncState,
   ) -> None:
      """Push detects conflict when remote changed since last sync."""
      old_time = datetime(2024, 1, 10, tzinfo=timezone.utc)
      old_hash = _compute_hash("Old content")
      entry = SyncEntry(
         path="Conflict.md",
         notion_id="conflict-123",
         notion_url="https://notion.so/conflict-123",
         notion_parent_id="root-id-123",
         is_directory=False,
         remote_hash=old_hash,
         remote_mtime=old_time,
         status=SyncStatus.clean,
      )
      await sync_state.set_entry(entry)
      (workspace / "Conflict.md").write_text("Local changes")

      # Remote has changed since last sync
      mock_api_client.get_page.return_value = {
         "page": {
            "id": "conflict-123",
            "url": "https://notion.so/conflict-123",
            "last_edited_time": "2024-01-15T10:00:00.000Z",  # Newer than old_time
         },
         "blocks": [],
      }

      result = await engine.push()

      assert len(result.conflicts) == 1
      mock_api_client.update_blocks.assert_not_called()

   async def test_push_handles_local_deletion(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
      workspace: Path,
      sync_state: SyncState,
   ) -> None:
      """Push archives page when local file deleted."""
      old_time = datetime.now(timezone.utc)
      entry = SyncEntry(
         path="ToDelete.md",
         notion_id="delete-123",
         notion_url="https://notion.so/delete-123",
         notion_parent_id="root-id-123",
         is_directory=False,
         remote_hash=_compute_hash("Content"),
         remote_mtime=old_time,
         status=SyncStatus.clean,
      )
      await sync_state.set_entry(entry)
      # File does not exist locally

      mock_api_client.get_page.return_value = {
         "page": {
            "id": "delete-123",
            "url": "https://notion.so/delete-123",
            "last_edited_time": old_time.isoformat(),
         },
         "blocks": [],
      }

      result = await engine.push()

      assert len(result.deleted) == 1
      mock_api_client.archive_page.assert_called_once_with("delete-123")


# --- Status tests ---


@pytest.mark.trio
class TestStatus:
   """Tests for status operation."""

   async def test_status_returns_pending_changes(
      self,
      engine: SyncEngine,
      workspace: Path,
      sync_state: SyncState,
   ) -> None:
      """Status returns entries with non-clean status."""
      import git
      # Create entries with various statuses
      # For clean entry, hash must match actual file content
      content_hash = _compute_hash("content")
      statuses = [
         ("clean.md", SyncStatus.clean, content_hash),  # Hash matches file
         ("remote_mod.md", SyncStatus.remote_modified, "old-hash"),
         ("conflict.md", SyncStatus.conflict, "old-hash"),
      ]
      for path, status, file_hash in statuses:
         entry = SyncEntry(
            path=path,
            notion_id=f"id-{path}",
            notion_url=f"https://notion.so/{path}",
            notion_parent_id="root-id-123",
            is_directory=False,
            remote_hash=file_hash,
            remote_mtime=datetime.now(timezone.utc),
            status=status,
         )
         await sync_state.set_entry(entry)
         (workspace / path).write_text("content")

      # Commit files to git so clean.md shows as unchanged
      repo = git.Repo(workspace)
      repo.index.add("*")
      repo.index.commit("test")

      pending = await engine.status()

      # Only non-clean entries returned
      paths = {e.path for e in pending}
      assert "clean.md" not in paths
      assert "remote_mod.md" in paths
      assert "conflict.md" in paths

   async def test_status_detects_local_modifications(
      self,
      engine: SyncEngine,
      workspace: Path,
      sync_state: SyncState,
   ) -> None:
      """Status refreshes and detects local file changes via git."""
      old_hash = _compute_hash("Original")
      entry = SyncEntry(
         path="tracked.md",
         notion_id="tr-123",
         notion_url="https://notion.so/tr-123",
         notion_parent_id="root-id-123",
         is_directory=False,
         remote_hash=old_hash,
         remote_mtime=datetime.now(timezone.utc),
         status=SyncStatus.clean,
      )
      await sync_state.set_entry(entry)
      # Write different content
      (workspace / "tracked.md").write_text("Modified content")

      # Mock git status to report the file as modified
      with patch("notionfs.sync.engine._get_local_changes") as mock_git:
         mock_git.return_value = {"tracked.md": "M"}
         pending = await engine.status()

      assert len(pending) == 1
      assert pending[0].path == "tracked.md"
      # Git-detected modifications are reported (status details depend on engine impl)

   async def test_status_detects_deleted_local(
      self,
      engine: SyncEngine,
      workspace: Path,
      sync_state: SyncState,
   ) -> None:
      """Status detects locally deleted files via git."""
      entry = SyncEntry(
         path="deleted.md",
         notion_id="del-123",
         notion_url="https://notion.so/del-123",
         notion_parent_id="root-id-123",
         is_directory=False,
         remote_hash="hash",
         remote_mtime=datetime.now(timezone.utc),
         status=SyncStatus.clean,
      )
      await sync_state.set_entry(entry)
      # File does not exist - mock git to report deletion

      with patch("notionfs.sync.engine._get_local_changes") as mock_git:
         mock_git.return_value = {"deleted.md": "D"}
         pending = await engine.status()

      assert len(pending) == 1
      assert pending[0].path == "deleted.md"
      assert pending[0].status == SyncStatus.deleted_local


# --- Fetch tree tests ---


@pytest.mark.trio
class TestFetchTree:
   """Tests for tree fetching."""

   async def test_fetch_tree_single_page(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
   ) -> None:
      """Fetch tree for a single page (no children)."""
      mock_api_client.get_page_metadata.return_value = {
         "id": "single-123",
         "url": "https://notion.so/single-123",
         "parent": {},
         "last_edited_time": "2024-01-15T10:00:00.000Z",
         "properties": {
            "title": {"type": "title", "title": [{"plain_text": "Single Page"}]}
         },
      }
      mock_api_client.get_block_children.return_value = [
         {
            "type": "paragraph",
            "paragraph": {"rich_text": []},
         }
      ]

      tree = await engine.fetch_tree("single-123")

      assert tree is not None
      assert tree.id == "single-123"
      assert tree.title == "Single Page"
      assert tree.has_children is False
      assert len(tree.children) == 0

   async def test_fetch_tree_with_children(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
   ) -> None:
      """Fetch tree for page with child pages."""
      # Parent page with child_page block
      mock_api_client.get_page_metadata.side_effect = [
         # Parent
         {
            "id": "parent-123",
            "url": "https://notion.so/parent-123",
            "parent": {},
            "last_edited_time": "2024-01-15T10:00:00.000Z",
            "properties": {
               "title": {"type": "title", "title": [{"plain_text": "Parent"}]}
            },
         },
         # Child
         {
            "id": "child-456",
            "url": "https://notion.so/child-456",
            "parent": {"page_id": "parent-123"},
            "last_edited_time": "2024-01-15T10:00:00.000Z",
            "properties": {
               "title": {"type": "title", "title": [{"plain_text": "Child"}]}
            },
         },
      ]
      mock_api_client.get_block_children.side_effect = [
         # Parent's blocks
         [{"type": "child_page", "id": "child-456"}],
         # Child's blocks
         [],
      ]

      tree = await engine.fetch_tree("parent-123")

      assert tree is not None
      assert tree.has_children is True
      assert len(tree.children) == 1
      assert tree.children[0].title == "Child"

   async def test_fetch_tree_handles_error(
      self,
      engine: SyncEngine,
      mock_api_client: MagicMock,
   ) -> None:
      """Fetch tree returns None on error."""
      mock_api_client.get_page_metadata.side_effect = Exception("API error")

      tree = await engine.fetch_tree("bad-id")

      assert tree is None


# --- RemotePage tests ---


class TestRemotePage:
   """Tests for RemotePage dataclass."""

   def test_create_page(self) -> None:
      """Create a basic RemotePage."""
      page = RemotePage(
         id="test-123",
         title="Test",
         url="https://notion.so/test",
         parent_id=None,
         is_database=False,
         last_edited_time=datetime.now(timezone.utc),
      )
      assert page.id == "test-123"
      assert page.blocks is None
      assert page.children == []
      assert not page.has_children

   def test_create_database(self) -> None:
      """Create a database RemotePage."""
      page = RemotePage(
         id="db-123",
         title="My Database",
         url="https://notion.so/db",
         parent_id="parent-123",
         is_database=True,
         last_edited_time=datetime.now(timezone.utc),
         schema={"Name": {"type": "title"}},
         has_children=True,
      )
      assert page.is_database
      assert page.has_children
      assert "Name" in page.schema


# --- Scan local files ---


@pytest.mark.trio
class TestScanLocalFiles:
   """Tests for local file scanning."""

   async def test_scan_finds_md_files(
      self,
      engine: SyncEngine,
      workspace: Path,
   ) -> None:
      """Scan finds all .md files in workspace."""
      (workspace / "file1.md").write_text("content")
      (workspace / "file2.md").write_text("content")
      (workspace / "subdir").mkdir()
      (workspace / "subdir" / "file3.md").write_text("content")

      files = engine._scan_local_files()

      assert len(files) == 3
      assert "file1.md" in files
      assert "file2.md" in files
      assert "subdir/file3.md" in files

   async def test_scan_ignores_hidden_dirs(
      self,
      engine: SyncEngine,
      workspace: Path,
   ) -> None:
      """Scan ignores hidden directories."""
      (workspace / "visible.md").write_text("content")
      (workspace / ".hidden").mkdir()
      (workspace / ".hidden" / "secret.md").write_text("content")

      files = engine._scan_local_files()

      assert len(files) == 1
      assert "visible.md" in files

   async def test_scan_ignores_non_md_files(
      self,
      engine: SyncEngine,
      workspace: Path,
   ) -> None:
      """Scan ignores non-.md files."""
      (workspace / "page.md").write_text("content")
      (workspace / "image.png").write_bytes(b"binary")
      (workspace / "data.json").write_text("{}")

      files = engine._scan_local_files()

      assert files == ["page.md"]

   async def test_scan_ignores_symlinks(
      self,
      engine: SyncEngine,
      workspace: Path,
      tmp_path: Path,
   ) -> None:
      """Scan ignores symlinks (files and directories)."""
      # Create real file
      (workspace / "real.md").write_text("real content")

      # Create target for symlink outside workspace
      external = tmp_path / "external"
      external.mkdir()
      (external / "secret.md").write_text("external content")

      # Create symlinked file
      target_file = tmp_path / "target.md"
      target_file.write_text("symlinked file")
      symlink_file = workspace / "linked.md"
      symlink_file.symlink_to(target_file)

      # Create symlinked directory
      symlink_dir = workspace / "linked_dir"
      symlink_dir.symlink_to(external)

      files = engine._scan_local_files()

      # Only real file found, symlinks ignored
      assert files == ["real.md"]
