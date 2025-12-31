"""Convert markdown to Notion blocks."""

from __future__ import annotations

import builtins
import re
from typing import Any, Callable

import mistune

# LinkResolver: maps local filename (e.g., "Page.md") -> (notion_id, is_database)
# Returns None if unresolved
LinkResolver = Callable[[str], tuple[str, bool] | None]


# Notion's maximum rich_text segment length
MAX_RICH_TEXT_LENGTH = 2000


def _chunk_text(text: str, max_len: int = MAX_RICH_TEXT_LENGTH) -> list[str]:
    """Split text into chunks at word boundaries.

    Notion limits each rich_text segment to 2000 characters.
    This function splits long text at word boundaries to fit within the limit.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    start = 0

    while start < len(text):
        if start + max_len >= len(text):
            # Remaining text fits in one chunk
            chunks.append(text[start:])
            break

        # Find last space before max_len
        end = start + max_len
        space_pos = text.rfind(" ", start, end)

        if space_pos > start:
            # Split at word boundary, including the space in the chunk
            chunks.append(text[start : space_pos + 1])
            start = space_pos + 1
        else:
            # No space found, force split at max_len
            chunks.append(text[start:end])
            start = end

    return chunks


def _peel_outer_formatting(text: str) -> tuple[dict[str, bool], str]:
    """Peel off outer formatting markers and return (annotations, inner_text).

    Handles nested formatting like **bold [link](url)** by stripping outer
    markers first, allowing inner content to be parsed separately.
    """
    annotations: dict[str, bool] = {
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "code": False,
    }
    inner = text

    # Keep peeling until nothing changes
    changed = True
    while changed:
        changed = False

        # Bold+italic (*** or ___)
        if m := re.fullmatch(r"\*\*\*(.+)\*\*\*", inner, re.DOTALL):
            inner = m.group(1)
            annotations["bold"] = True
            annotations["italic"] = True
            changed = True
            continue
        if m := re.fullmatch(r"___(.+)___", inner, re.DOTALL):
            inner = m.group(1)
            annotations["bold"] = True
            annotations["italic"] = True
            changed = True
            continue

        # Bold (** or __)
        if m := re.fullmatch(r"\*\*(.+)\*\*", inner, re.DOTALL):
            inner = m.group(1)
            annotations["bold"] = True
            changed = True
            continue
        if m := re.fullmatch(r"__(.+)__", inner, re.DOTALL):
            inner = m.group(1)
            annotations["bold"] = True
            changed = True
            continue

        # Italic (* or _)
        if m := re.fullmatch(r"\*(.+)\*", inner, re.DOTALL):
            inner = m.group(1)
            annotations["italic"] = True
            changed = True
            continue
        if m := re.fullmatch(r"_(.+)_", inner, re.DOTALL):
            inner = m.group(1)
            annotations["italic"] = True
            changed = True
            continue

        # Strikethrough
        if m := re.fullmatch(r"~~(.+)~~", inner, re.DOTALL):
            inner = m.group(1)
            annotations["strikethrough"] = True
            changed = True
            continue

        # Code (leaf-level, don't continue peeling)
        if m := re.fullmatch(r"`(.+)`", inner, re.DOTALL):
            inner = m.group(1)
            annotations["code"] = True
            # Don't continue - code is leaf level
            break

    return annotations, inner


def _merge_annotations(base: dict[str, bool], overlay: dict[str, bool]) -> dict[str, bool]:
    """Merge two annotation dicts, OR-ing the boolean values."""
    return {
        "bold": base.get("bold", False) or overlay.get("bold", False),
        "italic": base.get("italic", False) or overlay.get("italic", False),
        "strikethrough": base.get("strikethrough", False) or overlay.get("strikethrough", False),
        "code": base.get("code", False) or overlay.get("code", False),
    }


def _parse_inline_segment(text: str, base_annotations: dict[str, bool]) -> list[dict[str, Any]]:
    """Parse a text segment for inline formatting, applying base annotations.

    This handles segments that don't contain links (links are parsed separately).
    """
    if not text:
        return []

    result: list[dict[str, Any]] = []

    # Pattern for inline formatting (no links here)
    combined = (
        r"(\*\*\*.*?\*\*\*|___.*?___|"  # bold+italic
        r"\*\*.*?\*\*|__.*?__|"  # bold
        r"\*.*?\*|_.*?_|"  # italic
        r"~~.*?~~|`.*?`)"  # strikethrough, code
    )

    parts = re.split(combined, text)

    for part in parts:
        if not part:
            continue

        inner_ann, content = _peel_outer_formatting(part)
        merged = _merge_annotations(base_annotations, inner_ann)

        for chunk in _chunk_text(content):
            result.append(
                {
                    "type": "text",
                    "text": {"content": chunk},
                    "annotations": merged.copy(),
                    "href": None,
                }
            )

    return result


def md_to_rich_text(text: str) -> list[dict[str, Any]]:
    """Convert plain/formatted text to Notion rich_text array.

    Handles: ***bold+italic***, **bold**, *italic*, ~~strike~~, `code`, [link](url)
    Also handles nested formatting like **bold [link](url)**.

    Long text segments are automatically split at word boundaries to stay
    within Notion's 2000 character limit per rich_text segment.
    """
    if not text:
        return []

    result: list[dict[str, Any]] = []

    # First, peel off any outer formatting that wraps the entire text
    outer_ann, inner = _peel_outer_formatting(text)

    # Split by links, preserving the link matches
    link_pattern = r"(\[[^\]]+\]\([^)]+\))"
    parts = re.split(link_pattern, inner)

    for part in parts:
        if not part:
            continue

        # Check if this part is a link
        link_match = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", part)
        if link_match:
            link_text = link_match.group(1)
            href = link_match.group(2)

            # Link text may have its own formatting
            link_ann, link_content = _peel_outer_formatting(link_text)
            merged = _merge_annotations(outer_ann, link_ann)

            for chunk in _chunk_text(link_content):
                result.append(
                    {
                        "type": "text",
                        "text": {"content": chunk},
                        "annotations": merged.copy(),
                        "href": href,
                    }
                )
        else:
            # Non-link segment - parse for inline formatting
            result.extend(_parse_inline_segment(part, outer_ann))

    return result if result else []


def _plain_text(content: str) -> dict[str, Any]:
    """Create a plain text rich_text element."""
    return {
        "type": "text",
        "text": {"content": content},
        "annotations": {
            "bold": False,
            "italic": False,
            "strikethrough": False,
            "code": False,
        },
        "href": None,
    }


def _make_block(block_type: str, rich_text: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """Create a Notion block with rich_text content."""
    block_data = {"rich_text": rich_text, **extra}
    return {
        "type": block_type,
        block_type: block_data,
    }


class NotionBlockRenderer(mistune.HTMLRenderer):
    """Mistune renderer that outputs Notion blocks instead of HTML."""

    # Sentinel to separate list items in concatenated text
    _ITEM_SEP = "\x00ITEM\x00"
    # Sentinel to mark content inside a block_quote
    _QUOTE_MARKER = "\x00QUOTE\x00"
    # Sentinel to mark image was rendered (skip enclosing paragraph)
    _IMAGE_MARKER = "\x00IMAGE\x00"
    # Pattern to match local .md links: [Title](./File.md)
    _LOCAL_LINK_PATTERN = re.compile(r"^\[([^\]]+)\]\(\./([^)]+\.md)\)$")

    def __init__(self, link_resolver: LinkResolver | None = None) -> None:
        super().__init__()
        self.blocks: list[dict[str, Any]] = []
        self._in_blockquote = False
        self._link_resolver = link_resolver

    def paragraph(self, text: str) -> str:
        if self._in_blockquote:
            # Return text for block_quote to handle; don't emit block here
            return self._QUOTE_MARKER + text
        # Skip empty paragraphs that only contain image markers
        if text.strip() == self._IMAGE_MARKER or not text.strip():
            return ""
        # Filter out image markers from mixed content
        text = text.replace(self._IMAGE_MARKER, "").strip()
        if not text:
            return ""

        # Check if this is a standalone local .md link (child page reference)
        if self._link_resolver and (match := self._LOCAL_LINK_PATTERN.fullmatch(text)):
            filename = match.group(2)
            resolved = self._link_resolver(filename)
            if resolved:
                notion_id, is_database = resolved
                if is_database:
                    self.blocks.append(
                        {
                            "type": "link_to_page",
                            "link_to_page": {"type": "database_id", "database_id": notion_id},
                        }
                    )
                else:
                    self.blocks.append(
                        {
                            "type": "link_to_page",
                            "link_to_page": {"type": "page_id", "page_id": notion_id},
                        }
                    )
                return ""

        self.blocks.append(_make_block("paragraph", md_to_rich_text(text)))
        return ""

    def heading(self, text: str, level: int, **attrs: Any) -> str:
        # Notion only supports heading_1, heading_2, heading_3
        heading_level = min(level, 3)
        block_type = f"heading_{heading_level}"
        self.blocks.append(_make_block(block_type, md_to_rich_text(text)))
        return ""

    def block_code(self, code: str, info: str | None = None, **attrs: Any) -> str:
        language = info.strip() if info else "plain text"
        # Map common language aliases to Notion's expected names
        lang_map = {
            "py": "python",
            "js": "javascript",
            "ts": "typescript",
            "sh": "shell",
            "bash": "shell",
            "yml": "yaml",
            "": "plain text",
        }
        language = lang_map.get(language, language)

        self.blocks.append(
            {
                "type": "code",
                "code": {
                    "rich_text": [_plain_text(code.rstrip("\n"))],
                    "language": language,
                },
            }
        )
        return ""

    def render_token(self, token: dict[str, Any], state: Any) -> str:
        """Override to track blockquote context."""
        if token["type"] == "block_quote":
            self._in_blockquote = True
            # Render children first
            children = token.get("children", [])
            children_text = self.render_tokens(children, state)
            self._in_blockquote = False
            # Now call block_quote with the collected text
            return self.block_quote(children_text)
        return super().render_token(token, state)

    def block_quote(self, text: str) -> str:
        # Extract content from nested paragraph (marked with QUOTE_MARKER)
        if self._QUOTE_MARKER in text:
            # May have multiple paragraphs
            parts = text.split(self._QUOTE_MARKER)
            content = "\n".join(p.strip() for p in parts if p.strip())
        else:
            content = text.strip()
        self.blocks.append(_make_block("quote", md_to_rich_text(content)))
        return ""

    def thematic_break(self) -> str:
        self.blocks.append({"type": "divider", "divider": {}})
        return ""

    def list_item(self, text: str, **attrs: Any) -> str:
        # Return text with sentinel separator for list() to split
        return self._ITEM_SEP + text

    # Note: 'list' shadows builtin but is required by mistune's renderer interface
    def list(self, text: str, ordered: bool, **attrs: Any) -> str:  # noqa: A003
        # Split by sentinel to get individual items
        items = [item_ for item_ in text.split(self._ITEM_SEP) if item_]

        for item_ in items:
            # Check for todo items: [ ] or [x] (with optional leading whitespace)
            todo_match = re.match(r"^\s*\[([xX ])\]\s*(.*)$", item_)
            if todo_match:
                checked = todo_match.group(1).lower() == "x"
                content = todo_match.group(2)
                self.blocks.append(
                    {
                        "type": "to_do",
                        "to_do": {
                            "rich_text": md_to_rich_text(content),
                            "checked": checked,
                        },
                    }
                )
            elif ordered:
                self.blocks.append(_make_block("numbered_list_item", md_to_rich_text(item_)))
            else:
                self.blocks.append(_make_block("bulleted_list_item", md_to_rich_text(item_)))

        return ""

    def image(self, text: str, url: str, title: str | None = None) -> str:
        self.blocks.append(
            {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": url},
                },
            }
        )
        return self._IMAGE_MARKER

    def link(self, text: str, url: str, title: str | None = None) -> str:
        # Inline link - return markdown format for rich_text processing
        return f"[{text}]({url})"

    def codespan(self, text: str) -> str:
        return f"`{text}`"

    def emphasis(self, text: str) -> str:
        return f"*{text}*"

    def strong(self, text: str) -> str:
        return f"**{text}**"

    def strikethrough(self, text: str) -> str:
        return f"~~{text}~~"

    def text(self, text: str) -> str:
        return text

    def linebreak(self) -> str:
        return "\n"

    def softbreak(self) -> str:
        return "\n"

    def newline(self) -> str:
        return ""

    def blank_line(self) -> str:
        return ""

    def finalize(self) -> builtins.list[dict[str, Any]]:
        """Finalize and return all collected blocks."""
        return self.blocks


def markdown_to_blocks(
    content: str,
    link_resolver: LinkResolver | None = None,
) -> list[dict[str, Any]]:
    """Parse markdown and return Notion block format.

    Args:
       content: Markdown text to convert
       link_resolver: Optional callback to resolve local .md filenames back to
          Notion IDs. Takes filename (e.g., "Page.md") and returns
          (notion_id, is_database) or None if unresolved.
          When resolved, local links become link_to_page blocks.

    Returns:
       List of Notion block objects
    """
    if not content or not content.strip():
        return []

    # Pre-process: convert task list syntax that mistune might miss
    # - [ ] item -> preserve for our list handler
    # - [x] item -> preserve for our list handler

    renderer = NotionBlockRenderer(link_resolver=link_resolver)
    md = mistune.create_markdown(
        renderer=renderer,
        plugins=["strikethrough"],
    )

    md(content)
    return renderer.finalize()
