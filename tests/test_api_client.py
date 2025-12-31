"""Tests for the Notion API client."""

from notionfs.notion.api_client import MAX_BLOCKS_PER_REQUEST, _chunk_blocks


class TestBlockChunking:
    """Tests for block chunking utility."""

    def test_empty_list(self):
        """Empty list returns empty result."""
        assert _chunk_blocks([]) == []

    def test_small_list_unchanged(self):
        """Lists smaller than limit returned as single chunk."""
        blocks = [{"type": "paragraph"} for _ in range(50)]
        result = _chunk_blocks(blocks)
        assert len(result) == 1
        assert result[0] == blocks

    def test_exactly_limit(self):
        """List exactly at limit returned as single chunk."""
        blocks = [{"type": "paragraph"} for _ in range(MAX_BLOCKS_PER_REQUEST)]
        result = _chunk_blocks(blocks)
        assert len(result) == 1
        assert len(result[0]) == MAX_BLOCKS_PER_REQUEST

    def test_over_limit_chunked(self):
        """List over limit split into multiple chunks."""
        blocks = [{"type": "paragraph", "id": i} for i in range(250)]
        result = _chunk_blocks(blocks)

        assert len(result) == 3
        assert len(result[0]) == 100
        assert len(result[1]) == 100
        assert len(result[2]) == 50

        # Verify all blocks preserved in order
        flat = [b for chunk in result for b in chunk]
        assert flat == blocks

    def test_custom_max_size(self):
        """Custom max size is respected."""
        blocks = [{"type": "paragraph"} for _ in range(25)]
        result = _chunk_blocks(blocks, max_size=10)

        assert len(result) == 3
        assert len(result[0]) == 10
        assert len(result[1]) == 10
        assert len(result[2]) == 5
