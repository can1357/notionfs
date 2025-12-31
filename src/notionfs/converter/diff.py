"""Diff-based block updates for efficient Notion sync.

Instead of delete-all + append-all, this module computes minimal changes
to preserve block IDs, comments, and synced content.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class DiffAction(Enum):
    """Types of operations in a block diff."""

    KEEP = "keep"  # Block unchanged, no action needed
    UPDATE = "update"  # Block content changed, update in place
    DELETE = "delete"  # Block removed, delete it
    INSERT = "insert"  # New block, append at position


@dataclass
class BlockDiff:
    """A single diff operation."""

    action: DiffAction
    block_id: str | None = None  # For KEEP/UPDATE/DELETE
    new_block: dict[str, Any] | None = None  # For UPDATE/INSERT
    position: int = 0  # Target position (for ordering)


def _extract_text_content(item: dict[str, Any]) -> str:
    """Extract text content from a rich_text item.

    Handles both Notion API format (plain_text) and generated blocks (text.content).
    """
    plain = item.get("plain_text")
    if plain:
        return str(plain)
    text_obj = item.get("text", {})
    if isinstance(text_obj, dict):
        content = text_obj.get("content", "")
        return str(content) if content else ""
    return ""


def _extract_annotations(rich_text: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract formatting annotations from rich_text for signature comparison."""
    annotations = []
    for item in rich_text:
        if isinstance(item, dict):
            ann = item.get("annotations", {})
            # Include non-default annotations (bold, italic, etc.)
            filtered_ann = {
                k: v
                for k, v in ann.items()
                if v and k in ("bold", "italic", "strikethrough", "underline", "code")
            }
            if filtered_ann:
                annotations.append(
                    {
                        "text": _extract_text_content(item),
                        "annotations": filtered_ann,
                    }
                )
    return annotations


def block_signature(block: dict[str, Any]) -> str:
    """Compute a content-based signature for a block.

    This is used to detect if two blocks are "the same" for diffing purposes.
    We hash the type + text content + formatting, ignoring IDs and timestamps.
    """
    block_type = block.get("type", "")
    content = block.get(block_type, {})

    # Extract text content for comparison
    rich_text = content.get("rich_text", [])
    text_content = "".join(
        _extract_text_content(item) for item in rich_text if isinstance(item, dict)
    )

    # Include type + normalized text + formatting in signature
    sig_data: dict[str, Any] = {
        "type": block_type,
        "text": text_content,
    }

    # Include formatting annotations in signature
    annotations = _extract_annotations(rich_text)
    if annotations:
        sig_data["annotations"] = annotations

    # Special handling for certain block types
    if block_type == "code":
        sig_data["language"] = content.get("language", "")
    elif block_type == "to_do":
        sig_data["checked"] = content.get("checked", False)
    elif block_type == "image":
        if "external" in content:
            sig_data["url"] = content["external"].get("url", "")
        elif "file" in content:
            sig_data["url"] = content["file"].get("url", "")
    elif block_type == "divider":
        sig_data["divider"] = True

    return hashlib.sha256(json.dumps(sig_data, sort_keys=True).encode()).hexdigest()[:16]


def blocks_equal(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Check if two blocks have equivalent content.

    Returns True if the blocks have the same type and content,
    even if they differ in IDs or timestamps.
    """
    return block_signature(old) == block_signature(new)


# Block types that are managed by page hierarchy, not block content
# These should be preserved through diff, not deleted/updated
PRESERVED_BLOCK_TYPES = {"child_page", "child_database"}


def compute_block_diff(
    old_blocks: list[dict[str, Any]], new_blocks: list[dict[str, Any]]
) -> list[BlockDiff]:
    """Compute minimal diff between old and new block lists.

    Uses a simple LCS-like approach to find matching blocks,
    then generates update/insert/delete operations.

    Note: child_page and child_database blocks are preserved automatically
    since they're managed by page hierarchy, not block content.

    Args:
        old_blocks: Original blocks from Notion (with IDs)
        new_blocks: New blocks from parsed markdown (no IDs)

    Returns:
        List of diff operations to transform old to new.
    """
    diffs, _ = compute_block_diff_with_meta(old_blocks, new_blocks)
    return diffs


def compute_block_diff_with_meta(
    old_blocks: list[dict[str, Any]], new_blocks: list[dict[str, Any]]
) -> tuple[list[BlockDiff], bool]:
    """Compute diff and whether inserts are append-only.

    Returns:
        Tuple of (diff operations, appends_only) where appends_only indicates
        all inserts are at the end of the new block list.
    """
    # Separate preserved blocks (child_page, child_database) from diffable blocks
    preserved_blocks = [b for b in old_blocks if b.get("type") in PRESERVED_BLOCK_TYPES]
    diffable_old = [b for b in old_blocks if b.get("type") not in PRESERVED_BLOCK_TYPES]

    # Also filter out link_to_page from new blocks that reference preserved pages
    # (they would have been created as child_page markers)
    preserved_ids = {b.get("id") for b in preserved_blocks}
    diffable_new = []
    for b in new_blocks:
        if b.get("type") == "link_to_page":
            link = b.get("link_to_page", {})
            ref_id = link.get("page_id") or link.get("database_id")
            if ref_id in preserved_ids:
                continue  # Skip - this is a reference to an existing child page
        diffable_new.append(b)

    if len(diffable_old) == len(diffable_new):
        types_match = all(
            old.get("type") == new.get("type")
            for old, new in zip(diffable_old, diffable_new)
        )
        ids_present = all(old.get("id") for old in diffable_old)
        if types_match and ids_present:
            diffs: list[BlockDiff] = []
            for i, (old_block, new_block) in enumerate(zip(diffable_old, diffable_new)):
                block_id = old_block.get("id")
                if not block_id:
                    continue
                if blocks_equal(old_block, new_block):
                    diffs.append(BlockDiff(action=DiffAction.KEEP, block_id=block_id, position=i))
                else:
                    diffs.append(
                        BlockDiff(
                            action=DiffAction.UPDATE,
                            block_id=block_id,
                            new_block=new_block,
                            position=i,
                        )
                    )

            for i, block in enumerate(preserved_blocks):
                diffs.append(
                    BlockDiff(
                        action=DiffAction.KEEP,
                        block_id=block.get("id"),
                        position=len(diffable_new) + i,
                    )
                )

            diffs.sort(key=lambda d: (d.position, d.action.value))
            return diffs, True

    if not diffable_old:
        # All inserts
        result: list[BlockDiff] = [
            BlockDiff(action=DiffAction.INSERT, new_block=block, position=i)
            for i, block in enumerate(diffable_new)
        ]
        # Add keeps for preserved blocks
        for i, block in enumerate(preserved_blocks):
            result.append(
                BlockDiff(
                    action=DiffAction.KEEP, block_id=block.get("id"), position=len(diffable_new) + i
                )
            )
        return result, True

    if not diffable_new:
        # All deletes (but keep preserved blocks)
        return [
            BlockDiff(action=DiffAction.DELETE, block_id=block.get("id"))
            for block in diffable_old
            if block.get("id")
        ] + [
            BlockDiff(action=DiffAction.KEEP, block_id=block.get("id"))
            for block in preserved_blocks
        ], True

    # Compute signatures for matching
    old_sigs = [block_signature(b) for b in diffable_old]
    new_sigs = [block_signature(b) for b in diffable_new]

    # Build LCS matrix
    m, n = len(diffable_old), len(diffable_new)
    dp = [[0] * (n + 1) for _ in range(m + 1)]

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if old_sigs[i - 1] == new_sigs[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    # Backtrack to find matches
    matches: list[tuple[int, int]] = []  # (old_idx, new_idx)
    i, j = m, n
    while i > 0 and j > 0:
        if old_sigs[i - 1] == new_sigs[j - 1]:
            matches.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif dp[i - 1][j] > dp[i][j - 1]:
            i -= 1
        else:
            j -= 1

    matches.reverse()

    # Generate diff operations
    lcs_diffs: list[BlockDiff] = []
    matched_old: set[int] = set()
    matched_new: set[int] = set()

    for old_idx, new_idx in matches:
        matched_old.add(old_idx)
        matched_new.add(new_idx)

    # Deletions: old blocks not in matches
    for i, old_block in enumerate(diffable_old):
        if i not in matched_old:
            block_id = old_block.get("id")
            if block_id:
                lcs_diffs.append(BlockDiff(action=DiffAction.DELETE, block_id=block_id))

    # Keeps: matched blocks (signature equality implies content equality)
    for old_idx, new_idx in matches:
        old_block = diffable_old[old_idx]
        block_id = old_block.get("id")
        lcs_diffs.append(BlockDiff(action=DiffAction.KEEP, block_id=block_id, position=new_idx))

    # Insertions: new blocks not in matches
    insert_positions: list[int] = []
    for i, new_block in enumerate(diffable_new):
        if i not in matched_new:
            lcs_diffs.append(BlockDiff(action=DiffAction.INSERT, new_block=new_block, position=i))
            insert_positions.append(i)

    # Always keep preserved blocks (child_page, child_database)
    # Use unique positions to maintain stable order
    for i, block in enumerate(preserved_blocks):
        lcs_diffs.append(
            BlockDiff(
                action=DiffAction.KEEP,
                block_id=block.get("id"),
                position=len(diffable_new) + i,  # Unique position per preserved block
            )
        )

    # Sort by position for ordered application
    lcs_diffs.sort(key=lambda d: (d.position, d.action.value))

    if not insert_positions:
        appends_only = True
    else:
        insert_positions.sort()
        expected_start = len(diffable_new) - len(insert_positions)
        appends_only = all(pos >= expected_start for pos in insert_positions)

    return lcs_diffs, appends_only


def apply_diff_plan(
    old_blocks: list[dict[str, Any]], diffs: list[BlockDiff]
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert diff operations to API calls.

    Args:
        old_blocks: Original blocks with IDs
        diffs: List of diff operations

    Returns:
        Tuple of (block_ids_to_delete, blocks_to_update, blocks_to_append).

        - block_ids_to_delete: List of block IDs to delete
        - blocks_to_update: List of (block_id, new_content) for updates
        - blocks_to_append: List of new blocks to append (in order)
    """
    to_delete: list[str] = []
    to_update: list[dict[str, Any]] = []
    to_append: list[dict[str, Any]] = []

    for diff in diffs:
        if diff.action == DiffAction.DELETE:
            if diff.block_id:
                to_delete.append(diff.block_id)
        elif diff.action == DiffAction.UPDATE:
            if diff.block_id and diff.new_block:
                # For updates, include the block_id in the payload
                update_block = dict(diff.new_block)
                update_block["id"] = diff.block_id
                to_update.append(update_block)
        elif diff.action == DiffAction.INSERT:
            if diff.new_block:
                to_append.append(diff.new_block)
        # KEEP blocks don't need any action

    return to_delete, to_update, to_append


class BlockDiffTracker:
    """Tracks original blocks for computing diffs on write."""

    def __init__(self) -> None:
        # notion_id -> list of original blocks (with IDs)
        self._original_blocks: dict[str, list[dict[str, Any]]] = {}

    def store_original(self, notion_id: str, blocks: list[dict[str, Any]]) -> None:
        """Store the original blocks fetched from Notion.

        Should be called after get_page() when reading content.
        """
        # Deep copy to avoid mutation issues
        self._original_blocks[notion_id] = [_deep_copy_block(b) for b in blocks]

    def get_original(self, notion_id: str) -> list[dict[str, Any]] | None:
        """Get the stored original blocks for a page."""
        return self._original_blocks.get(notion_id)

    def clear(self, notion_id: str) -> None:
        """Clear stored blocks for a page (after successful sync)."""
        self._original_blocks.pop(notion_id, None)

    def clear_all(self) -> None:
        """Clear all stored blocks (for cache invalidation)."""
        self._original_blocks.clear()

    def compute_diff(
        self, notion_id: str, new_blocks: list[dict[str, Any]]
    ) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]] | None:
        """Compute diff between original and new blocks.

        Returns None if no original blocks are stored.
        Otherwise returns (to_delete, to_update, to_append).
        """
        original = self._original_blocks.get(notion_id)
        if original is None:
            return None

        diffs = compute_block_diff(original, new_blocks)
        to_delete, to_update, to_append = apply_diff_plan(original, diffs)
        return to_delete, to_update, to_append


def _deep_copy_block(block: dict[str, Any]) -> dict[str, Any]:
    """Deep copy a block dict, preserving structure."""
    result: dict[str, Any] = json.loads(json.dumps(block))
    return result
