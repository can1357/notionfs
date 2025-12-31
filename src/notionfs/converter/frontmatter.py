"""Frontmatter parsing and generation."""

from typing import Any, cast

import frontmatter  # type: ignore[import-untyped]
import yaml


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from markdown content.

    Returns (metadata dict, body content).
    """
    try:
        post = frontmatter.loads(content)
        return dict(post.metadata), cast(str, post.content)
    except yaml.YAMLError:
        return {}, content


def build_frontmatter(metadata: dict[str, Any], content: str) -> str:
    """Build markdown file with YAML frontmatter."""
    post = frontmatter.Post(content, **metadata)
    return cast(str, frontmatter.dumps(post))


# Keys that are read-only (managed by filesystem, not user-editable)
# Note: last_edited_time is reflected in file mtime, not frontmatter
READONLY_KEYS = frozenset(
    {
        "notion_id",
        "notion_url",
    }
)


def split_metadata(metadata: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split metadata into readonly and editable parts.

    Returns (readonly, editable).
    """
    readonly = {}
    editable = {}

    for key, value in metadata.items():
        if key in READONLY_KEYS:
            readonly[key] = value
        else:
            editable[key] = value

    return readonly, editable
