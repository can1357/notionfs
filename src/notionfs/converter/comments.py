"""Convert between Notion comments and YAML format."""

from collections import defaultdict
from typing import Any

import yaml

COMMENTS_SUFFIX = ".comments.yaml"


def _extract_plain_text(rich_text: list[dict[str, Any]]) -> str:
    """Extract plain text from Notion rich_text array."""
    return "".join(item.get("plain_text", "") for item in rich_text)


def _extract_user_name(user: dict[str, Any]) -> str:
    """Extract user name from partial user object."""
    name = user.get("name")
    if name:
        return str(name)
    user_id = user.get("id")
    return str(user_id) if user_id else "Unknown"


def comments_to_yaml(comments: list[dict[str, Any]]) -> str:
    """Convert Notion API comments to YAML format.

    Groups comments by discussion_id and formats as YAML with sections
    for existing discussions and new comment input.

    Args:
       comments: List of comment objects from Notion API

    Returns:
       YAML string with discussions and empty new_comments/new_replies sections
    """
    # Group by discussion_id
    discussions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for comment in comments:
        disc_id = comment.get("discussion_id", "")
        discussions[disc_id].append(comment)

    # Build output structure
    output: dict[str, Any] = {"discussions": []}

    for disc_id, disc_comments in discussions.items():
        disc_entry: dict[str, Any] = {
            "discussion_id": disc_id,
            "comments": [],
        }

        for comment in disc_comments:
            comm_entry = {
                "id": comment.get("id", ""),
                "created_by": _extract_user_name(comment.get("created_by", {})),
                "created_time": comment.get("created_time", ""),
                "text": _extract_plain_text(comment.get("rich_text", [])),
            }
            disc_entry["comments"].append(comm_entry)

        output["discussions"].append(disc_entry)

    # Add empty sections for new comments/replies
    output["new_comments"] = []
    output["new_replies"] = []

    result: str = yaml.dump(output, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return result


def parse_new_comments(yaml_str: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Parse YAML to extract new comments and replies.

    Args:
       yaml_str: YAML content from comments file

    Returns:
       Tuple of:
       - List of new comment texts (page-level)
       - List of (discussion_id, text) tuples for replies
    """
    try:
        data = yaml.safe_load(yaml_str)
    except yaml.YAMLError:
        return [], []

    if not isinstance(data, dict):
        return [], []

    new_comments: list[str] = []
    new_replies: list[tuple[str, str]] = []

    # Parse new_comments (page-level)
    raw_comments = data.get("new_comments", [])
    if not isinstance(raw_comments, list):
        raw_comments = []
    for item in raw_comments or []:
        if isinstance(item, dict) and "text" in item:
            text = item["text"]
            if text and isinstance(text, str):
                new_comments.append(text)
        elif isinstance(item, str) and item:
            new_comments.append(item)

    # Parse new_replies (to existing threads)
    for item in data.get("new_replies", []) or []:
        if isinstance(item, dict):
            disc_id = item.get("discussion_id", "")
            text = item.get("text", "")
            if disc_id and text and isinstance(disc_id, str) and isinstance(text, str):
                new_replies.append((disc_id, text))

    return new_comments, new_replies


def has_new_content(yaml_str: str) -> bool:
    """Check if YAML has any new comments or replies to process.

    Args:
       yaml_str: YAML content from comments file

    Returns:
       True if there are new_comments or new_replies to create
    """
    new_comments, new_replies = parse_new_comments(yaml_str)
    return bool(new_comments) or bool(new_replies)
