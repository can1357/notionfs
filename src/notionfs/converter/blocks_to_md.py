"""Convert Notion blocks to markdown."""

from typing import Any, Callable

# LinkResolver takes a notion_id and returns the filename (e.g., "Page.md") or None
LinkResolver = Callable[[str], str | None]

_LIST_BLOCK_TYPES = {"bulleted_list_item", "numbered_list_item", "to_do"}
_HEADING_BLOCK_TYPES = {"heading_1", "heading_2", "heading_3"}


def rich_text_to_md(rich_text: list[dict[str, Any]]) -> str:
    """Convert Notion rich text array to markdown with formatting."""
    if not rich_text:
        return ""

    parts: list[str] = []
    for item in rich_text:
        if not isinstance(item, dict):
            continue

        # Notion API returns plain_text, but our generated blocks use text.content
        text = item.get("plain_text", "")
        if not text:
            text = item.get("text", {}).get("content", "")
        if not text:
            continue

        # Apply annotations
        annotations = item.get("annotations", {})
        if annotations.get("code") and text:
            text = f"`{text}`"

        # Handle bold+italic together as *** to avoid nesting issues
        is_bold = annotations.get("bold")
        is_italic = annotations.get("italic")
        if is_bold and is_italic:
            text = f"***{text}***"
        elif is_bold:
            text = f"**{text}**"
        elif is_italic:
            text = f"*{text}*"

        if annotations.get("strikethrough"):
            text = f"~~{text}~~"

        # Apply link
        href = item.get("href")
        if href:
            text = f"[{text}]({href})"

        parts.append(text)

    return "".join(parts)


def blocks_to_markdown(
    blocks: list[dict[str, Any]],
    link_resolver: LinkResolver | None = None,
) -> str:
    """Convert Notion blocks to markdown.

    Args:
       blocks: List of Notion block objects from the API.
       link_resolver: Optional callback to resolve notion_id to filename.
          If provided, child_page links become relative .md paths.
          If None or returns None, links use notion:// URIs.

    Returns:
       Markdown string representation of the blocks.
    """
    if not blocks:
        return ""

    rendered: list[tuple[str, str, bool]] = []
    numbered_counter = 0

    for block in blocks:
        block_type = block.get("type", "")
        content = block.get(block_type, {})

        # Track numbered list continuity
        if block_type == "numbered_list_item":
            numbered_counter += 1
        else:
            numbered_counter = 0

        # Convert block to markdown line(s)
        md_line = _convert_block(block_type, content, block, numbered_counter, link_resolver)
        if md_line is not None:
            rendered.append((block_type, md_line, False))

        # Handle nested children (skip for types that handle children themselves)
        if block.get("has_children") and "children" in block:
            if block_type not in ("toggle", "table"):
                children = block.get("children", [])
                if children:
                    child_md = blocks_to_markdown(children, link_resolver)
                    if child_md:
                        # Indent child content by 4 spaces
                        indented = "\n".join(
                            "    " + line if line else line for line in child_md.split("\n")
                        )
                        rendered.append((block_type, indented, True))

    return _join_rendered_blocks(rendered)


def _join_rendered_blocks(rendered: list[tuple[str, str, bool]]) -> str:
    """Join rendered blocks with spacing while preserving list continuity."""
    if not rendered:
        return ""

    lines: list[str] = []
    prev_type: str | None = None

    for block_type, md_line, no_spacing_before in rendered:
        if lines and not no_spacing_before and _needs_blank_line(prev_type, block_type):
            lines.append("")
        lines.append(md_line)
        prev_type = block_type

    return "\n".join(lines)


def _needs_blank_line(prev_type: str | None, curr_type: str) -> bool:
    """Return True when blocks should be separated by a blank line."""
    if prev_type is None:
        return False
    if curr_type in _HEADING_BLOCK_TYPES:
        return True
    if prev_type in _HEADING_BLOCK_TYPES:
        return False
    if prev_type in _LIST_BLOCK_TYPES and curr_type in _LIST_BLOCK_TYPES:
        return False
    return True


def _convert_block(
    block_type: str,
    content: dict[str, Any],
    block: dict[str, Any],
    numbered_counter: int,
    link_resolver: LinkResolver | None = None,
) -> str | None:
    """Convert a single block to markdown."""
    match block_type:
        case "paragraph":
            return rich_text_to_md(content.get("rich_text", []))

        case "heading_1":
            text = rich_text_to_md(content.get("rich_text", []))
            return f"# {text}"

        case "heading_2":
            text = rich_text_to_md(content.get("rich_text", []))
            return f"## {text}"

        case "heading_3":
            text = rich_text_to_md(content.get("rich_text", []))
            return f"### {text}"

        case "bulleted_list_item":
            text = rich_text_to_md(content.get("rich_text", []))
            return f"- {text}"

        case "numbered_list_item":
            text = rich_text_to_md(content.get("rich_text", []))
            return f"{numbered_counter}. {text}"

        case "to_do":
            text = rich_text_to_md(content.get("rich_text", []))
            checked = content.get("checked", False)
            checkbox = "[x]" if checked else "[ ]"
            return f"- {checkbox} {text}"

        case "code":
            code_text = rich_text_to_md(content.get("rich_text", []))
            language = content.get("language", "")
            fence = "````" if "```" in code_text else "```"
            return f"{fence}{language}\n{code_text}\n{fence}"

        case "quote":
            text = rich_text_to_md(content.get("rich_text", []))
            # Handle multiline quotes
            quoted_lines = "\n".join(f"> {line}" for line in text.split("\n"))
            return quoted_lines

        case "callout":
            text = rich_text_to_md(content.get("rich_text", []))
            icon = content.get("icon", {})
            emoji = icon.get("emoji", "") if icon else ""
            prefix = f"{emoji} " if emoji else ""
            return f"> {prefix}{text}"

        case "divider":
            return "---"

        case "image":
            url = _extract_file_url(content)
            url = url.replace(")", "%29")
            caption = rich_text_to_md(content.get("caption", []))
            alt_text = caption if caption else ""
            return f"![{alt_text}]({url})"

        case "bookmark":
            url = content.get("url", "")
            url = url.replace(")", "%29")
            caption = rich_text_to_md(content.get("caption", []))
            title = caption if caption else url
            return f"[{title}]({url})"

        case "embed":
            url = content.get("url", "")
            url = url.replace(")", "%29")
            return f"[embed]({url})"

        case "table":
            return _convert_table(block)

        case "toggle":
            text = rich_text_to_md(content.get("rich_text", []))
            # Get toggle children content
            children_md = ""
            if block.get("has_children") and "children" in block:
                children = block.get("children", [])
                if children:
                    children_md = blocks_to_markdown(children, link_resolver)
            return f"<details><summary>{text}</summary>\n\n{children_md}\n</details>"

        case "child_page":
            title = content.get("title", "Untitled")
            page_id = block.get("id", "")
            # Try to resolve to local filename
            if link_resolver:
                filename = link_resolver(page_id)
                if filename:
                    return f"[{title}](./{filename})"
            return f"[{title}](notion://{page_id})"

        case "child_database":
            title = content.get("title", "Untitled Database")
            db_id = block.get("id", "")
            # Try to resolve to local directory
            if link_resolver:
                filename = link_resolver(db_id)
                if filename:
                    return f"[{title}](./{filename})"
            return f"[{title}](notion://{db_id})"

        case "link_to_page":
            # link_to_page blocks can reference pages or databases
            link_type = content.get("type", "")
            target_id = content.get(link_type, "") if link_type else ""

            if not target_id:
                # Fallback: try page_id or database_id directly
                target_id = content.get("page_id", "") or content.get("database_id", "")
                link_type = "page_id" if content.get("page_id") else "database_id"

            # Try to resolve to local filename
            if link_resolver and target_id:
                filename = link_resolver(target_id)
                if filename:
                    # Use filename without extension as display text
                    display_name = filename.rsplit(".", 1)[0] if "." in filename else filename
                    return f"[{display_name}](./{filename})"

            # Fallback to notion:// URI
            if target_id:
                return f"[Link to page](notion://{target_id})"
            return "<!-- notion:link_to_page (invalid) -->"

        case "table_row":
            # Table rows are handled by _convert_table
            return None

        case _:
            # Unknown block type - emit comment
            return f"<!-- notion:{block_type} -->"


def _extract_file_url(content: dict[str, Any]) -> str:
    """Extract URL from file or external image content."""
    if "file" in content:
        file_data = content["file"]
        return str(file_data.get("url", "")) if file_data else ""
    if "external" in content:
        external_data = content["external"]
        return str(external_data.get("url", "")) if external_data else ""
    return ""


def _convert_table(block: dict[str, Any]) -> str:
    """Convert a Notion table block to markdown."""
    children = block.get("children", [])
    if not children:
        return "<!-- notion:table (empty) -->"

    rows: list[list[str]] = []
    for row_block in children:
        if row_block.get("type") != "table_row":
            continue
        row_content = row_block.get("table_row", {})
        cells = row_content.get("cells", [])
        row_cells: list[str] = []
        for cell in cells:
            cell_text = rich_text_to_md(cell) if cell else ""
            # Escape newlines and pipes in cell content
            cell_text = cell_text.replace("\n", " ").replace("|", "\\|")
            row_cells.append(cell_text)
        rows.append(row_cells)

    if not rows:
        return "<!-- notion:table (no rows) -->"

    # Determine column count from first row
    col_count = len(rows[0]) if rows else 0
    if col_count == 0:
        return "<!-- notion:table (no columns) -->"

    # Normalize all rows to same column count
    for row in rows:
        while len(row) < col_count:
            row.append("")

    lines: list[str] = []

    # Header row
    header = rows[0]
    lines.append("| " + " | ".join(header) + " |")

    # Separator
    lines.append("| " + " | ".join(["---"] * col_count) + " |")

    # Data rows
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)
