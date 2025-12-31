"""Tests for diff-based block updates."""

from notionfs.converter.diff import (
    BlockDiff,
    BlockDiffTracker,
    DiffAction,
    apply_diff_plan,
    block_signature,
    blocks_equal,
    compute_block_diff,
)


def make_paragraph(text: str, block_id: str | None = None) -> dict:
    """Create a paragraph block for testing."""
    block = {
        "type": "paragraph",
        "paragraph": {"rich_text": [{"plain_text": text, "type": "text"}]},
    }
    if block_id:
        block["id"] = block_id
    return block


def make_heading(text: str, level: int = 1, block_id: str | None = None) -> dict:
    """Create a heading block for testing."""
    block_type = f"heading_{level}"
    block = {
        "type": block_type,
        block_type: {"rich_text": [{"plain_text": text, "type": "text"}]},
    }
    if block_id:
        block["id"] = block_id
    return block


def make_divider(block_id: str | None = None) -> dict:
    """Create a divider block for testing."""
    block = {"type": "divider", "divider": {}}
    if block_id:
        block["id"] = block_id
    return block


class TestBlockSignature:
    """Tests for block_signature function."""

    def test_same_content_same_signature(self):
        """Blocks with same content have same signature."""
        b1 = make_paragraph("Hello world")
        b2 = make_paragraph("Hello world")
        assert block_signature(b1) == block_signature(b2)

    def test_different_content_different_signature(self):
        """Blocks with different content have different signatures."""
        b1 = make_paragraph("Hello")
        b2 = make_paragraph("World")
        assert block_signature(b1) != block_signature(b2)

    def test_id_ignored(self):
        """Block ID should not affect signature."""
        b1 = make_paragraph("Hello", block_id="abc-123")
        b2 = make_paragraph("Hello", block_id="xyz-789")
        assert block_signature(b1) == block_signature(b2)

    def test_different_types_different_signature(self):
        """Different block types have different signatures."""
        b1 = make_paragraph("Title")
        b2 = make_heading("Title", level=1)
        assert block_signature(b1) != block_signature(b2)

    def test_dividers_same_signature(self):
        """All dividers have the same signature."""
        b1 = make_divider("id-1")
        b2 = make_divider("id-2")
        assert block_signature(b1) == block_signature(b2)


class TestBlocksEqual:
    """Tests for blocks_equal function."""

    def test_equal_content(self):
        """blocks_equal returns True for same content."""
        b1 = make_paragraph("Hello")
        b2 = make_paragraph("Hello")
        assert blocks_equal(b1, b2)

    def test_unequal_content(self):
        """blocks_equal returns False for different content."""
        b1 = make_paragraph("Hello")
        b2 = make_paragraph("World")
        assert not blocks_equal(b1, b2)


class TestComputeBlockDiff:
    """Tests for compute_block_diff function."""

    def test_empty_to_empty(self):
        """Empty old and new results in no diffs."""
        diffs = compute_block_diff([], [])
        assert diffs == []

    def test_empty_to_blocks(self):
        """Adding blocks to empty page."""
        new = [make_paragraph("One"), make_paragraph("Two")]
        diffs = compute_block_diff([], new)

        assert len(diffs) == 2
        assert all(d.action == DiffAction.INSERT for d in diffs)
        assert diffs[0].new_block == new[0]
        assert diffs[1].new_block == new[1]

    def test_blocks_to_empty(self):
        """Removing all blocks."""
        old = [
            make_paragraph("One", block_id="id-1"),
            make_paragraph("Two", block_id="id-2"),
        ]
        diffs = compute_block_diff(old, [])

        assert len(diffs) == 2
        assert all(d.action == DiffAction.DELETE for d in diffs)
        assert {d.block_id for d in diffs} == {"id-1", "id-2"}

    def test_unchanged_blocks(self):
        """Identical blocks result in KEEP actions."""
        old = [
            make_paragraph("One", block_id="id-1"),
            make_paragraph("Two", block_id="id-2"),
        ]
        new = [make_paragraph("One"), make_paragraph("Two")]
        diffs = compute_block_diff(old, new)

        keep_diffs = [d for d in diffs if d.action == DiffAction.KEEP]
        assert len(keep_diffs) == 2
        assert {d.block_id for d in keep_diffs} == {"id-1", "id-2"}

    def test_update_in_place(self):
        """Changed content with same types updates in place."""
        old = [make_paragraph("Old", block_id="id-1")]
        new = [make_paragraph("New")]
        diffs = compute_block_diff(old, new)

        updates = [d for d in diffs if d.action == DiffAction.UPDATE]
        assert len(updates) == 1
        assert updates[0].block_id == "id-1"
        assert (
            updates[0].new_block["paragraph"]["rich_text"][0]["plain_text"] == "New"
        )

    def test_insert_in_middle(self):
        """Insert a new block between existing blocks."""
        old = [
            make_paragraph("One", block_id="id-1"),
            make_paragraph("Three", block_id="id-3"),
        ]
        new = [
            make_paragraph("One"),
            make_paragraph("Two"),  # New
            make_paragraph("Three"),
        ]
        diffs = compute_block_diff(old, new)

        actions = {d.action for d in diffs}
        assert DiffAction.INSERT in actions
        assert DiffAction.KEEP in actions

        inserts = [d for d in diffs if d.action == DiffAction.INSERT]
        assert len(inserts) == 1
        assert inserts[0].new_block["paragraph"]["rich_text"][0]["plain_text"] == "Two"

    def test_delete_from_middle(self):
        """Delete a block from the middle."""
        old = [
            make_paragraph("One", block_id="id-1"),
            make_paragraph("Two", block_id="id-2"),  # Will be deleted
            make_paragraph("Three", block_id="id-3"),
        ]
        new = [make_paragraph("One"), make_paragraph("Three")]
        diffs = compute_block_diff(old, new)

        deletes = [d for d in diffs if d.action == DiffAction.DELETE]
        assert len(deletes) == 1
        assert deletes[0].block_id == "id-2"

    def test_reorder_blocks(self):
        """Reordering blocks."""
        old = [
            make_paragraph("A", block_id="id-a"),
            make_paragraph("B", block_id="id-b"),
            make_paragraph("C", block_id="id-c"),
        ]
        new = [
            make_paragraph("C"),
            make_paragraph("A"),
            make_paragraph("B"),
        ]
        diffs = compute_block_diff(old, new)

        # Should detect that blocks are reused (matched by content)
        # Some will be KEEP, some may be DELETE+INSERT depending on LCS
        assert len(diffs) > 0


class TestApplyDiffPlan:
    """Tests for apply_diff_plan function."""

    def test_all_deletes(self):
        """All DELETE actions result in delete list."""
        old = [
            make_paragraph("One", block_id="id-1"),
            make_paragraph("Two", block_id="id-2"),
        ]
        diffs = [
            BlockDiff(action=DiffAction.DELETE, block_id="id-1"),
            BlockDiff(action=DiffAction.DELETE, block_id="id-2"),
        ]
        to_delete, to_update, to_append = apply_diff_plan(old, diffs)

        assert to_delete == ["id-1", "id-2"]
        assert to_update == []
        assert to_append == []

    def test_all_inserts(self):
        """All INSERT actions result in append list."""
        block1 = make_paragraph("New 1")
        block2 = make_paragraph("New 2")
        diffs = [
            BlockDiff(action=DiffAction.INSERT, new_block=block1, position=0),
            BlockDiff(action=DiffAction.INSERT, new_block=block2, position=1),
        ]
        to_delete, to_update, to_append = apply_diff_plan([], diffs)

        assert to_delete == []
        assert to_update == []
        assert to_append == [block1, block2]

    def test_keep_ignored(self):
        """KEEP actions don't generate any operations."""
        old = [make_paragraph("Keep me", block_id="id-1")]
        diffs = [BlockDiff(action=DiffAction.KEEP, block_id="id-1")]
        to_delete, to_update, to_append = apply_diff_plan(old, diffs)

        assert to_delete == []
        assert to_update == []
        assert to_append == []

    def test_update_included(self):
        """UPDATE actions produce update payloads with IDs."""
        old = [make_paragraph("Old", block_id="id-1")]
        new_block = make_paragraph("New")
        diffs = [
            BlockDiff(
                action=DiffAction.UPDATE,
                block_id="id-1",
                new_block=new_block,
                position=0,
            )
        ]
        to_delete, to_update, to_append = apply_diff_plan(old, diffs)

        assert to_delete == []
        assert to_append == []
        assert len(to_update) == 1
        assert to_update[0]["id"] == "id-1"

    def test_mixed_operations(self):
        """Mix of operations."""
        old = [
            make_paragraph("Keep", block_id="id-keep"),
            make_paragraph("Delete", block_id="id-delete"),
        ]
        new_block = make_paragraph("New")
        diffs = [
            BlockDiff(action=DiffAction.KEEP, block_id="id-keep"),
            BlockDiff(action=DiffAction.DELETE, block_id="id-delete"),
            BlockDiff(action=DiffAction.INSERT, new_block=new_block, position=1),
        ]
        to_delete, to_update, to_append = apply_diff_plan(old, diffs)

        assert to_delete == ["id-delete"]
        assert to_update == []
        assert to_append == [new_block]


class TestBlockDiffTracker:
    """Tests for BlockDiffTracker class."""

    def test_store_and_retrieve(self):
        """Store and retrieve original blocks."""
        tracker = BlockDiffTracker()
        blocks = [make_paragraph("Hello", block_id="id-1")]

        tracker.store_original("page-123", blocks)
        retrieved = tracker.get_original("page-123")

        assert retrieved is not None
        assert len(retrieved) == 1
        assert retrieved[0]["id"] == "id-1"

    def test_retrieve_nonexistent(self):
        """Retrieving non-existent page returns None."""
        tracker = BlockDiffTracker()
        assert tracker.get_original("unknown-page") is None

    def test_clear(self):
        """Clear removes stored blocks."""
        tracker = BlockDiffTracker()
        tracker.store_original("page-123", [make_paragraph("Hello")])
        tracker.clear("page-123")

        assert tracker.get_original("page-123") is None

    def test_compute_diff_no_original(self):
        """compute_diff returns None if no original stored."""
        tracker = BlockDiffTracker()
        result = tracker.compute_diff("page-123", [make_paragraph("Hello")])
        assert result is None

    def test_compute_diff_with_original(self):
        """compute_diff returns tuple when original exists."""
        tracker = BlockDiffTracker()
        original = [make_paragraph("Old", block_id="id-1")]
        tracker.store_original("page-123", original)

        new_blocks = [make_paragraph("Old"), make_paragraph("New")]
        result = tracker.compute_diff("page-123", new_blocks)

        assert result is not None
        to_delete, to_update, to_append = result
        assert isinstance(to_delete, list)
        assert isinstance(to_update, list)
        assert isinstance(to_append, list)

    def test_deep_copy_isolation(self):
        """Stored blocks should be deep copied."""
        tracker = BlockDiffTracker()
        blocks = [make_paragraph("Hello", block_id="id-1")]
        tracker.store_original("page-123", blocks)

        # Modify original
        blocks[0]["paragraph"]["rich_text"][0]["plain_text"] = "Modified"

        # Retrieved should not be affected
        retrieved = tracker.get_original("page-123")
        assert retrieved[0]["paragraph"]["rich_text"][0]["plain_text"] == "Hello"


class TestIntegration:
    """Integration tests for the full diff workflow."""

    def test_add_block_workflow(self):
        """Test adding a block to existing content."""
        tracker = BlockDiffTracker()

        # Simulate reading a page with one block
        original = [make_paragraph("First paragraph", block_id="block-1")]
        tracker.store_original("page-abc", original)

        # User adds a new paragraph
        new_blocks = [
            make_paragraph("First paragraph"),
            make_paragraph("Second paragraph"),
        ]

        # Compute diff
        result = tracker.compute_diff("page-abc", new_blocks)
        assert result is not None
        to_delete, to_update, to_append = result

        # Should have: keep first block, insert second
        assert len(to_delete) == 0
        assert len(to_update) == 0
        assert len(to_append) == 1
        assert to_append[0]["paragraph"]["rich_text"][0]["plain_text"] == "Second paragraph"

    def test_delete_block_workflow(self):
        """Test deleting a block from existing content."""
        tracker = BlockDiffTracker()

        # Simulate reading a page with two blocks
        original = [
            make_paragraph("Keep me", block_id="block-1"),
            make_paragraph("Delete me", block_id="block-2"),
        ]
        tracker.store_original("page-abc", original)

        # User removes second paragraph
        new_blocks = [make_paragraph("Keep me")]

        result = tracker.compute_diff("page-abc", new_blocks)
        assert result is not None
        to_delete, to_update, to_append = result

        # Should have: keep first block, delete second
        assert to_delete == ["block-2"]
        assert len(to_update) == 0
        assert len(to_append) == 0

    def test_replace_all_workflow(self):
        """Test replacing all content."""
        tracker = BlockDiffTracker()

        original = [
            make_paragraph("Old content", block_id="block-1"),
        ]
        tracker.store_original("page-abc", original)

        new_blocks = [
            make_paragraph("Completely new content"),
        ]

        result = tracker.compute_diff("page-abc", new_blocks)
        assert result is not None
        to_delete, to_update, to_append = result

        # Content is different but type matches, so update in place
        assert len(to_delete) == 0
        assert len(to_append) == 0
        assert len(to_update) == 1


class TestPreservedBlocks:
    """Tests for child_page and child_database block preservation."""

    def test_child_page_preserved_in_diff(self):
        """child_page blocks should be preserved, not deleted."""
        old_blocks = [
            make_paragraph("Content", block_id="para-1"),
            {
                "type": "child_page",
                "child_page": {"title": "Child Page"},
                "id": "child-page-id",
            },
        ]
        new_blocks = [make_paragraph("Content")]

        diffs = compute_block_diff(old_blocks, new_blocks)

        # child_page should be kept, not deleted
        actions = {d.action for d in diffs}
        assert DiffAction.KEEP in actions
        delete_ids = [d.block_id for d in diffs if d.action == DiffAction.DELETE]
        assert "child-page-id" not in delete_ids

    def test_child_database_preserved_in_diff(self):
        """child_database blocks should be preserved, not deleted."""
        old_blocks = [
            {
                "type": "child_database",
                "child_database": {"title": "My Database"},
                "id": "db-id",
            },
        ]
        new_blocks = []  # Empty new blocks

        diffs = compute_block_diff(old_blocks, new_blocks)

        # child_database should be kept
        keep_ids = [d.block_id for d in diffs if d.action == DiffAction.KEEP]
        assert "db-id" in keep_ids

    def test_link_to_page_filtered_for_existing_child(self):
        """link_to_page blocks referencing existing child_page should be filtered."""
        old_blocks = [
            make_paragraph("Content", block_id="para-1"),
            {
                "type": "child_page",
                "child_page": {"title": "Child"},
                "id": "child-id",
            },
        ]
        # New blocks include a link_to_page that references the child
        new_blocks = [
            make_paragraph("Content"),
            {
                "type": "link_to_page",
                "link_to_page": {"type": "page_id", "page_id": "child-id"},
            },
        ]

        diffs = compute_block_diff(old_blocks, new_blocks)

        # link_to_page should be filtered out (not inserted)
        insert_types = [
            d.new_block.get("type") if d.new_block else None
            for d in diffs
            if d.action == DiffAction.INSERT
        ]
        assert "link_to_page" not in insert_types

        # child_page should still be kept
        keep_ids = [d.block_id for d in diffs if d.action == DiffAction.KEEP]
        assert "child-id" in keep_ids

    def test_regular_paragraphs_still_diffed(self):
        """Regular paragraphs should still be properly diffed."""
        old_blocks = [
            make_paragraph("Old text", block_id="para-1"),
            {
                "type": "child_page",
                "child_page": {"title": "Child"},
                "id": "child-id",
            },
            make_paragraph("More text", block_id="para-2"),
        ]
        new_blocks = [
            make_paragraph("Old text"),
            make_paragraph("New text"),  # New paragraph
        ]

        diffs = compute_block_diff(old_blocks, new_blocks)

        # para-2 should be updated in place (text changed)
        update_blocks = [d for d in diffs if d.action == DiffAction.UPDATE]
        assert any(d.block_id == "para-2" for d in update_blocks)
        assert any(
            d.new_block
            and d.new_block.get("paragraph", {}).get("rich_text", [{}])[0].get("plain_text")
            == "New text"
            for d in update_blocks
        )
