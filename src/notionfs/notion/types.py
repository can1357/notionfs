"""Data types for Notion objects."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class NotionBlock:
    id: str
    type: str
    content: dict[str, Any]
    children: list["NotionBlock"] = field(default_factory=list)


@dataclass
class NotionPage:
    id: str
    title: str
    parent_id: str | None
    parent_type: str  # 'page_id', 'database_id', 'workspace'
    properties: dict[str, Any]
    content_markdown: str
    created_time: datetime
    last_edited_time: datetime
    url: str
    is_database: bool = False
    has_children: bool = False


@dataclass
class NotionDatabase:
    id: str
    title: str
    parent_id: str | None
    parent_type: str
    schema: dict[str, Any]  # property definitions
    url: str


@dataclass
class NotionSearchResult:
    id: str
    title: str
    object_type: str  # 'page' or 'database'
    url: str
    parent_id: str | None = None
