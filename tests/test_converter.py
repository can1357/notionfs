"""Tests for markdown conversion."""

from notionfs.converter.blocks_to_md import blocks_to_markdown, rich_text_to_md
from notionfs.converter.frontmatter import build_frontmatter, parse_frontmatter
from notionfs.converter.md_to_blocks import (
    MAX_RICH_TEXT_LENGTH,
    _chunk_text,
    markdown_to_blocks,
    md_to_rich_text,
)
from notionfs.converter.properties import (
    frontmatter_to_properties,
    properties_to_frontmatter,
)


class TestFrontmatter:
    """Tests for frontmatter parsing and building."""

    def test_parse_frontmatter(self):
        """Test parsing YAML frontmatter."""
        content = """---
title: Test Page
status: Active
tags:
  - one
  - two
---

# Hello World

Some content here.
"""
        metadata, body = parse_frontmatter(content)

        assert metadata["title"] == "Test Page"
        assert metadata["status"] == "Active"
        assert metadata["tags"] == ["one", "two"]
        assert "# Hello World" in body
        assert "Some content here." in body

    def test_parse_no_frontmatter(self):
        """Test parsing content without frontmatter."""
        content = "# Just content\n\nNo frontmatter."
        metadata, body = parse_frontmatter(content)

        assert metadata == {}
        assert "# Just content" in body

    def test_build_frontmatter(self):
        """Test building markdown with frontmatter."""
        metadata = {
            "title": "My Page",
            "notion_id": "abc123",
        }
        content = "# Content\n\nBody text."

        result = build_frontmatter(metadata, content)

        assert "---" in result
        assert "title: My Page" in result
        assert "notion_id: abc123" in result
        assert "# Content" in result
        assert "Body text." in result

    def test_build_empty_frontmatter(self):
        """Test building with empty metadata."""
        result = build_frontmatter({}, "Just content")
        # Empty dict is serialized as {} in YAML
        assert result == "---\n{}\n---\n\nJust content"


class TestRichTextConversion:
    """Tests for rich text <-> markdown."""

    def test_plain_text(self):
        """Test plain text conversion."""
        rich = [{"plain_text": "Hello", "type": "text"}]
        assert rich_text_to_md(rich) == "Hello"

    def test_bold_text(self):
        """Test bold annotation."""
        rich = [{"plain_text": "bold", "type": "text", "annotations": {"bold": True}}]
        assert rich_text_to_md(rich) == "**bold**"

    def test_italic_text(self):
        """Test italic annotation."""
        rich = [{"plain_text": "italic", "type": "text", "annotations": {"italic": True}}]
        assert rich_text_to_md(rich) == "*italic*"

    def test_code_text(self):
        """Test code annotation."""
        rich = [{"plain_text": "code", "type": "text", "annotations": {"code": True}}]
        assert rich_text_to_md(rich) == "`code`"

    def test_strikethrough_text(self):
        """Test strikethrough annotation."""
        rich = [{"plain_text": "struck", "type": "text", "annotations": {"strikethrough": True}}]
        assert rich_text_to_md(rich) == "~~struck~~"

    def test_link(self):
        """Test link conversion."""
        rich = [{"plain_text": "click", "type": "text", "href": "https://example.com"}]
        assert rich_text_to_md(rich) == "[click](https://example.com)"

    def test_md_to_rich_basic(self):
        """Test markdown to rich text conversion."""
        result = md_to_rich_text("plain text")
        assert len(result) == 1
        assert result[0]["text"]["content"] == "plain text"

    def test_md_to_rich_bold(self):
        """Test markdown bold to rich text."""
        result = md_to_rich_text("**bold**")
        assert any(r["annotations"]["bold"] for r in result)

    def test_md_to_rich_link(self):
        """Test markdown link to rich text."""
        result = md_to_rich_text("[text](https://example.com)")
        assert any(r.get("href") == "https://example.com" for r in result)


class TestBlocksToMarkdown:
    """Tests for Notion blocks to markdown conversion."""

    def test_paragraph(self):
        """Test paragraph block."""
        blocks = [
            {
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "Hello world"}]},
            }
        ]
        assert blocks_to_markdown(blocks) == "Hello world"

    def test_headings(self):
        """Test heading blocks."""
        blocks = [
            {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "H1"}]}},
            {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "H2"}]}},
            {"type": "heading_3", "heading_3": {"rich_text": [{"plain_text": "H3"}]}},
        ]
        result = blocks_to_markdown(blocks)
        assert "# H1" in result
        assert "## H2" in result
        assert "### H3" in result

    def test_bulleted_list(self):
        """Test bulleted list."""
        blocks = [
            {
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"plain_text": "Item 1"}]},
            },
            {
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"plain_text": "Item 2"}]},
            },
        ]
        result = blocks_to_markdown(blocks)
        assert result == "- Item 1\n- Item 2"

    def test_numbered_list(self):
        """Test numbered list."""
        blocks = [
            {
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": [{"plain_text": "First"}]},
            },
            {
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": [{"plain_text": "Second"}]},
            },
        ]
        result = blocks_to_markdown(blocks)
        assert result == "1. First\n2. Second"

    def test_todo(self):
        """Test todo blocks."""
        blocks = [
            {
                "type": "to_do",
                "to_do": {"rich_text": [{"plain_text": "Unchecked"}], "checked": False},
            },
            {
                "type": "to_do",
                "to_do": {"rich_text": [{"plain_text": "Checked"}], "checked": True},
            },
        ]
        result = blocks_to_markdown(blocks)
        assert result == "- [ ] Unchecked\n- [x] Checked"

    def test_code_block(self):
        """Test code block."""
        blocks = [
            {
                "type": "code",
                "code": {
                    "rich_text": [{"plain_text": "print('hello')"}],
                    "language": "python",
                },
            }
        ]
        result = blocks_to_markdown(blocks)
        assert "```python" in result
        assert "print('hello')" in result
        assert "```" in result

    def test_quote(self):
        """Test quote block."""
        blocks = [
            {"type": "quote", "quote": {"rich_text": [{"plain_text": "A quote"}]}},
        ]
        result = blocks_to_markdown(blocks)
        assert "> A quote" in result

    def test_divider(self):
        """Test divider block."""
        blocks = [{"type": "divider", "divider": {}}]
        result = blocks_to_markdown(blocks)
        assert "---" in result

    def test_image_external(self):
        """Test external image block."""
        blocks = [
            {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": "https://example.com/img.png"},
                    "caption": [{"plain_text": "Caption"}],
                },
            }
        ]
        result = blocks_to_markdown(blocks)
        assert "![Caption](https://example.com/img.png)" in result

    def test_unknown_block(self):
        """Test unknown block type."""
        blocks = [{"type": "unknown_type", "unknown_type": {}}]
        result = blocks_to_markdown(blocks)
        assert "<!-- notion:unknown_type -->" in result

    def test_paragraph_spacing(self):
        """Paragraphs should be separated by a blank line."""
        blocks = [
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "One"}]}},
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Two"}]}},
        ]
        assert blocks_to_markdown(blocks) == "One\n\nTwo"

    def test_heading_spacing(self):
        """Headings should be spaced from previous blocks, not from their content."""
        blocks = [
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Intro"}]}},
            {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Section"}]}},
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Body"}]}},
            {"type": "heading_3", "heading_3": {"rich_text": [{"plain_text": "Sub"}]}},
        ]
        assert blocks_to_markdown(blocks) == "Intro\n\n## Section\nBody\n\n### Sub"


class TestMarkdownToBlocks:
    """Tests for markdown to Notion blocks conversion."""

    def test_paragraph(self):
        """Test paragraph conversion."""
        blocks = markdown_to_blocks("Hello world")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"

    def test_headings(self):
        """Test heading conversion."""
        blocks = markdown_to_blocks("# H1\n## H2\n### H3")
        types = [b["type"] for b in blocks]
        assert "heading_1" in types
        assert "heading_2" in types
        assert "heading_3" in types

    def test_bulleted_list(self):
        """Test bulleted list conversion."""
        blocks = markdown_to_blocks("- Item 1\n- Item 2")
        assert all(b["type"] == "bulleted_list_item" for b in blocks)

    def test_numbered_list(self):
        """Test numbered list conversion."""
        blocks = markdown_to_blocks("1. First\n2. Second")
        assert all(b["type"] == "numbered_list_item" for b in blocks)

    def test_todo_unchecked(self):
        """Test unchecked todo conversion."""
        blocks = markdown_to_blocks("- [ ] Todo item")
        assert blocks[0]["type"] == "to_do"
        assert blocks[0]["to_do"]["checked"] is False

    def test_todo_checked(self):
        """Test checked todo conversion."""
        blocks = markdown_to_blocks("- [x] Done item")
        assert blocks[0]["type"] == "to_do"
        assert blocks[0]["to_do"]["checked"] is True

    def test_code_block(self):
        """Test code block conversion."""
        blocks = markdown_to_blocks("```python\nprint('hi')\n```")
        assert blocks[0]["type"] == "code"
        assert blocks[0]["code"]["language"] == "python"

    def test_quote(self):
        """Test blockquote conversion."""
        blocks = markdown_to_blocks("> A quote")
        assert blocks[0]["type"] == "quote"

    def test_divider(self):
        """Test horizontal rule conversion."""
        blocks = markdown_to_blocks("---")
        assert blocks[0]["type"] == "divider"

    def test_image(self):
        """Test image conversion."""
        blocks = markdown_to_blocks("![alt](https://example.com/img.png)")
        assert blocks[0]["type"] == "image"
        assert blocks[0]["image"]["external"]["url"] == "https://example.com/img.png"

    def test_empty_content(self):
        """Test empty content."""
        assert markdown_to_blocks("") == []
        assert markdown_to_blocks("   ") == []


class TestProperties:
    """Tests for property conversion."""

    def test_title_property(self):
        """Test title property extraction."""
        props = {
            "Name": {
                "type": "title",
                "title": [{"plain_text": "My Title"}],
            }
        }
        result = properties_to_frontmatter(props, None)
        assert result["Name"] == "My Title"

    def test_checkbox_property(self):
        """Test checkbox property."""
        props = {
            "Done": {"type": "checkbox", "checkbox": True},
        }
        result = properties_to_frontmatter(props, None)
        assert result["Done"] is True

    def test_number_property(self):
        """Test number property."""
        props = {
            "Count": {"type": "number", "number": 42},
        }
        result = properties_to_frontmatter(props, None)
        assert result["Count"] == 42

    def test_select_property(self):
        """Test select property."""
        props = {
            "Status": {"type": "select", "select": {"name": "Active"}},
        }
        result = properties_to_frontmatter(props, None)
        assert result["Status"] == "Active"

    def test_multi_select_property(self):
        """Test multi-select property."""
        props = {
            "Tags": {
                "type": "multi_select",
                "multi_select": [{"name": "one"}, {"name": "two"}],
            },
        }
        result = properties_to_frontmatter(props, None)
        assert result["Tags"] == ["one", "two"]

    def test_frontmatter_to_properties_title(self):
        """Test converting frontmatter to title property."""
        fm = {"Name": "My Title"}
        schema = {"Name": {"type": "title"}}
        result = frontmatter_to_properties(fm, schema)
        assert result["Name"]["title"][0]["text"]["content"] == "My Title"

    def test_frontmatter_to_properties_checkbox(self):
        """Test converting frontmatter to checkbox property."""
        fm = {"Done": True}
        schema = {"Done": {"type": "checkbox"}}
        result = frontmatter_to_properties(fm, schema)
        assert result["Done"]["checkbox"] is True

    def test_frontmatter_to_properties_select(self):
        """Test converting frontmatter to select property."""
        fm = {"Status": "Active"}
        schema = {"Status": {"type": "select"}}
        result = frontmatter_to_properties(fm, schema)
        assert result["Status"]["select"]["name"] == "Active"


class TestRoundtrip:
    """Test roundtrip conversion (blocks -> markdown -> blocks)."""

    def test_simple_paragraph_roundtrip(self):
        """Test paragraph roundtrip."""
        original = [
            {
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "Hello world"}]},
            }
        ]
        md = blocks_to_markdown(original)
        blocks = markdown_to_blocks(md)

        assert blocks[0]["type"] == "paragraph"
        # Note: rich_text structure differs but content should match
        text = blocks[0]["paragraph"]["rich_text"][0]["text"]["content"]
        assert text == "Hello world"

    def test_heading_roundtrip(self):
        """Test heading roundtrip."""
        original = [{"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Title"}]}}]
        md = blocks_to_markdown(original)
        blocks = markdown_to_blocks(md)

        assert blocks[0]["type"] == "heading_1"

    def test_list_roundtrip(self):
        """Test list roundtrip."""
        original = [
            {
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"plain_text": "Item"}]},
            }
        ]
        md = blocks_to_markdown(original)
        blocks = markdown_to_blocks(md)

        assert blocks[0]["type"] == "bulleted_list_item"


class TestLinkResolver:
    """Tests for link resolver functionality in markdown conversion."""

    def test_local_link_resolved_to_link_to_page(self):
        """Test that local .md links become link_to_page when resolved."""

        def resolver(filename: str) -> tuple[str, bool] | None:
            if filename == "Child.md":
                return ("child-page-id", False)  # page
            return None

        md = "[Child Page](./Child.md)"
        blocks = markdown_to_blocks(md, link_resolver=resolver)

        assert len(blocks) == 1
        assert blocks[0]["type"] == "link_to_page"
        assert blocks[0]["link_to_page"]["type"] == "page_id"
        assert blocks[0]["link_to_page"]["page_id"] == "child-page-id"

    def test_local_link_resolved_to_database(self):
        """Test that local links to databases become link_to_page with database_id."""

        def resolver(filename: str) -> tuple[str, bool] | None:
            if filename == "Database.md":
                return ("db-id", True)  # database
            return None

        md = "[My Database](./Database.md)"
        blocks = markdown_to_blocks(md, link_resolver=resolver)

        assert len(blocks) == 1
        assert blocks[0]["type"] == "link_to_page"
        assert blocks[0]["link_to_page"]["type"] == "database_id"
        assert blocks[0]["link_to_page"]["database_id"] == "db-id"

    def test_local_link_unresolved_becomes_paragraph(self):
        """Test that unresolved local links remain as paragraphs."""

        def resolver(filename: str) -> tuple[str, bool] | None:
            return None  # Never resolve

        md = "[Unknown](./Unknown.md)"
        blocks = markdown_to_blocks(md, link_resolver=resolver)

        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"

    def test_external_link_unaffected(self):
        """Test that external links are unaffected by resolver."""

        def resolver(filename: str) -> tuple[str, bool] | None:
            return ("id", False)  # Would resolve if called

        md = "[External](https://example.com)"
        blocks = markdown_to_blocks(md, link_resolver=resolver)

        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"
        # External links stay as rich_text links, not link_to_page

    def test_no_resolver_keeps_paragraph(self):
        """Test that without resolver, local links stay as paragraphs."""
        md = "[Child](./Child.md)"
        blocks = markdown_to_blocks(md, link_resolver=None)

        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"

    def test_mixed_content_not_resolved(self):
        """Test that paragraphs with mixed content don't become link_to_page."""

        def resolver(filename: str) -> tuple[str, bool] | None:
            return ("id", False)

        # Link with surrounding text
        md = "See [Child](./Child.md) for more."
        blocks = markdown_to_blocks(md, link_resolver=resolver)

        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"  # Not link_to_page


class TestTextChunking:
    """Tests for long text chunking in rich_text conversion."""

    def test_short_text_unchanged(self):
        """Short text is not chunked."""
        text = "Hello world"
        chunks = _chunk_text(text)
        assert chunks == ["Hello world"]

    def test_text_at_limit_unchanged(self):
        """Text exactly at limit is not chunked."""
        text = "x" * MAX_RICH_TEXT_LENGTH
        chunks = _chunk_text(text)
        assert chunks == [text]

    def test_long_text_split_at_word_boundary(self):
        """Long text splits at word boundaries."""
        # Create text longer than limit with spaces
        words = ["word"] * 500  # More than 2000 chars
        text = " ".join(words)
        chunks = _chunk_text(text)

        # Should have multiple chunks
        assert len(chunks) > 1
        # Each chunk should be <= limit
        assert all(len(c) <= MAX_RICH_TEXT_LENGTH for c in chunks)
        # Reassembling should give original exactly (spaces included in chunks)
        reassembled = "".join(chunks)
        assert reassembled == text

    def test_long_text_without_spaces(self):
        """Long text without spaces is force-split at limit."""
        text = "x" * 3000  # 3000 chars, no spaces
        chunks = _chunk_text(text)

        assert len(chunks) == 2
        assert len(chunks[0]) == MAX_RICH_TEXT_LENGTH
        assert len(chunks[1]) == 1000

    def test_md_to_rich_text_chunks_long_content(self):
        """md_to_rich_text properly chunks long text."""
        # Create a very long plain text string
        text = " ".join(["paragraph"] * 300)  # ~3000 chars
        result = md_to_rich_text(text)

        # Should have multiple segments
        assert len(result) > 1
        # Each segment should be <= limit
        for item in result:
            assert len(item["text"]["content"]) <= MAX_RICH_TEXT_LENGTH

    def test_md_to_rich_text_preserves_formatting_when_chunked(self):
        """Formatting annotations are preserved on all chunks."""
        # Create long bold text
        text = "**" + " ".join(["bold"] * 500) + "**"
        result = md_to_rich_text(text)

        # All segments should have bold annotation
        assert len(result) > 1
        for item in result:
            assert item["annotations"]["bold"] is True
