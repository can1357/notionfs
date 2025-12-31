"""Direct Notion API client using notion-client SDK."""

from __future__ import annotations

import logging
import random
import time
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Callable

import httpx
import trio
from notion_client import AsyncClient
from notion_client.errors import APIResponseError

if TYPE_CHECKING:
   from notionfs.sync.progress import RequestInfo

logger = logging.getLogger(__name__)

# Type alias for request callback
RequestCallback = Callable[["RequestInfo"], None] | None

# Rate limiting configuration
REQUEST_CAPACITY = 3  # Max concurrent requests globally
DELETE_CAPACITY = 1   # Max concurrent deletes (more rate-limited by Notion)
MIN_REQUEST_INTERVAL = 0.33  # ~3 requests per second


def _create_timed_httpx_client(timeout: float) -> httpx.AsyncClient:
    """Create an httpx client with request timing logs."""
    request_times: dict[int, float] = {}

    async def log_request(request: httpx.Request) -> None:
        request_times[id(request)] = time.perf_counter()

    async def log_response(response: httpx.Response) -> None:
        request = response.request
        start = request_times.pop(id(request), None)
        if start is not None:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                f"{request.method} {request.url} → {response.status_code} ({elapsed_ms:.0f}ms)"
            )

    return httpx.AsyncClient(
        event_hooks={"request": [log_request], "response": [log_response]},
        timeout=httpx.Timeout(timeout),
    )

# HTTP status codes that warrant retry
# 409 = conflict_error: "Data collision or transactional conflict occurred; retry the request"
RETRYABLE_STATUS_CODES = {409, 429, 500, 502, 503, 504}

# Retry configuration
MAX_RETRIES = 5
BASE_DELAY = 1.0
MAX_DELAY = 60.0

# Notion API limit for children.append
MAX_BLOCKS_PER_REQUEST = 100


def _chunk_blocks(
    blocks: list[dict[str, Any]], max_size: int = MAX_BLOCKS_PER_REQUEST
) -> list[list[dict[str, Any]]]:
    """Split blocks into batches of at most max_size."""
    if not blocks:
        return []
    return [blocks[i : i + max_size] for i in range(0, len(blocks), max_size)]


class NotionAPIClient:
    """Async Notion API client with retry logic respecting Retry-After headers."""

    # Default timeout for API calls (60 seconds)
    DEFAULT_TIMEOUT = 60.0

    def __init__(
        self,
        auth_token: str,
        timeout: float | None = None,
        request_callback: RequestCallback = None,
    ) -> None:
        """Initialize client with auth token.

        Args:
           auth_token: Notion API integration token
           timeout: Optional timeout in seconds (default: 60s)
           request_callback: Optional callback for request events (for progress UI)
        """
        timeout_val = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        # Disable httpx's default request logging (we log with timing instead)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        httpx_client = _create_timed_httpx_client(timeout_val)
        self._client = AsyncClient(
            auth=auth_token,
            client=httpx_client,
        )
        # Rate limiting
        self._request_limiter = trio.CapacityLimiter(REQUEST_CAPACITY)
        self._delete_limiter = trio.CapacityLimiter(DELETE_CAPACITY)
        self._last_request_time: float = 0.0
        self._request_lock = trio.Lock()
        self._request_callback = request_callback

    def set_request_callback(self, callback: RequestCallback) -> None:
        """Set or update the request callback for progress tracking."""
        self._request_callback = callback

    def _emit_request(
        self,
        method: str,
        endpoint: str,
        status: str = "pending",
        status_code: int | None = None,
        error: str | None = None,
    ) -> None:
        """Emit a request event to the callback if set."""
        if not self._request_callback:
            return
        from notionfs.sync.progress import RequestInfo, RequestStatus

        status_enum = RequestStatus(status)
        info = RequestInfo(
            method=method,
            endpoint=endpoint,
            status=status_enum,
            status_code=status_code,
            error=error,
        )
        self._request_callback(info)

    async def _enforce_request_spacing(self) -> None:
        """Enforce minimum interval between requests (~3 req/sec)."""
        async with self._request_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < MIN_REQUEST_INTERVAL:
                await trio.sleep(MIN_REQUEST_INTERVAL - elapsed)
            self._last_request_time = time.monotonic()

    def _extract_endpoint(self, func: Any, kwargs: dict[str, Any]) -> tuple[str, str]:
        """Extract method and endpoint from a notion-client SDK function.

        Returns:
           Tuple of (HTTP method, endpoint path)
        """
        # Use __qualname__ (e.g., "PagesEndpoint.retrieve", "BlocksChildrenEndpoint.list")
        # or fall back to __class__.__name__ for callable objects (e.g., "SearchEndpoint")
        func_name = getattr(func, "__name__", "")
        qualname = getattr(func, "__qualname__", "") or func.__class__.__name__

        method = "GET"
        endpoint = "/"

        # Extract IDs from kwargs for endpoint construction
        page_id = kwargs.get("page_id", "")
        block_id = kwargs.get("block_id", "")
        database_id = kwargs.get("database_id", "")

        # Map SDK endpoints to HTTP methods/paths based on qualname
        if "PagesEndpoint" in qualname:
            if func_name == "retrieve" and page_id:
                endpoint = f"/pages/{page_id[:8]}..."
            elif func_name == "update" and page_id:
                method = "PATCH"
                endpoint = f"/pages/{page_id[:8]}..."
            elif func_name == "create":
                method = "POST"
                endpoint = "/pages"

        elif "DatabasesEndpoint" in qualname:
            if func_name == "retrieve" and database_id:
                endpoint = f"/databases/{database_id[:8]}..."
            elif func_name == "query" and database_id:
                method = "POST"
                endpoint = f"/databases/{database_id[:8]}...query"
            elif func_name == "update" and database_id:
                method = "PATCH"
                endpoint = f"/databases/{database_id[:8]}..."

        elif "BlocksChildrenEndpoint" in qualname:
            if func_name == "list" and block_id:
                endpoint = f"/blocks/{block_id[:8]}...children"
            elif func_name == "append" and block_id:
                method = "PATCH"
                endpoint = f"/blocks/{block_id[:8]}...children"

        elif "BlocksEndpoint" in qualname:
            if func_name == "delete" and block_id:
                method = "DELETE"
                endpoint = f"/blocks/{block_id[:8]}..."
            elif func_name == "update" and block_id:
                method = "PATCH"
                endpoint = f"/blocks/{block_id[:8]}..."

        elif "SearchEndpoint" in qualname:
            method = "POST"
            endpoint = "/search"

        elif "CommentsEndpoint" in qualname:
            if func_name == "list" and block_id:
                endpoint = f"/comments?block_id={block_id[:8]}..."
            elif func_name == "create":
                method = "POST"
                endpoint = "/comments"

        elif func_name == "request":
            # Raw request: extract from kwargs
            path = kwargs.get("path", "")
            method = kwargs.get("method", "POST")
            endpoint = f"/{path}"

        return method, endpoint

    async def _call_with_retry(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute API call with exponential backoff retry on rate limits.

        Respects Retry-After header from 429 responses per Notion API guidelines.
        Enforces global concurrency limit and request spacing.

        Args:
           func: Async function to call
           *args: Positional arguments for func
           **kwargs: Keyword arguments for func

        Returns:
           Result from the API call

        Raises:
           APIResponseError: If all retries exhausted or non-retryable error
        """
        last_exception: Exception | None = None
        method, endpoint = self._extract_endpoint(func, kwargs)

        async with self._request_limiter:
            for attempt in range(MAX_RETRIES):
                await self._enforce_request_spacing()

                # Emit pending request event
                self._emit_request(method, endpoint, status="pending")

                try:
                    result = await func(*args, **kwargs)
                    # Emit success event
                    self._emit_request(method, endpoint, status="success", status_code=200)
                    return result
                except APIResponseError as e:
                    last_exception = e
                    status = e.status

                    if status not in RETRYABLE_STATUS_CODES:
                        logger.debug("Non-retryable error (status=%d): %s", status, str(e))
                        self._emit_request(
                            method, endpoint, status="error",
                            status_code=status, error=str(e.code)
                        )
                        raise

                    # Exponential backoff with jitter
                    delay = min(BASE_DELAY * (2**attempt) + random.uniform(0, 1), MAX_DELAY)

                    # Use Retry-After header if present (common for 429)
                    if status == 429 and hasattr(e, "headers") and e.headers:
                        retry_after = e.headers.get("Retry-After")
                        if retry_after:
                            try:
                                # Try parsing as seconds first
                                delay = max(delay, float(retry_after))
                            except (ValueError, TypeError):
                                # Try parsing as HTTP-date (RFC 7231)
                                try:
                                    retry_dt = parsedate_to_datetime(retry_after)
                                    import datetime

                                    now = datetime.datetime.now(datetime.timezone.utc)
                                    delta = (retry_dt - now).total_seconds()
                                    if delta > 0:
                                        delay = max(delay, delta)
                                except (ValueError, TypeError):
                                    pass

                    # Emit retrying event with error info
                    error_msg = f"{status} {e.code}" if hasattr(e, "code") else str(status)
                    self._emit_request(
                        method, endpoint, status="retrying",
                        status_code=status, error=error_msg
                    )

                    logger.debug(
                        "Retryable error (status=%d, attempt=%d/%d), retrying in %.1fs: %s",
                        status,
                        attempt + 1,
                        MAX_RETRIES,
                        delay,
                        str(e),
                    )
                    await trio.sleep(delay)
                except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
                    # Network-level errors are transient and should be retried
                    last_exception = e
                    delay = min(BASE_DELAY * (2**attempt) + random.uniform(0, 1), MAX_DELAY)

                    # Emit retrying event
                    error_msg = type(e).__name__
                    self._emit_request(
                        method, endpoint, status="retrying", error=error_msg
                    )

                    logger.debug(
                        "Network error (attempt=%d/%d), retrying in %.1fs: %s",
                        attempt + 1,
                        MAX_RETRIES,
                        delay,
                        str(e),
                    )
                    await trio.sleep(delay)

            # All retries exhausted
            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected: no exception but all retries exhausted")

    async def _delete_blocks_concurrent(self, block_ids: list[str]) -> None:
        """Delete multiple blocks concurrently with stricter rate limiting."""
        if not block_ids:
            return

        async def delete_one(block_id: str) -> None:
            async with self._delete_limiter:
                try:
                    await self._call_with_retry(self._client.blocks.delete, block_id=block_id)
                except APIResponseError as e:
                    if e.status == 404 or e.code == "object_not_found":
                        logger.debug("Block %s already deleted, skipping", block_id)
                    else:
                        raise

        async with trio.open_nursery() as nursery:
            for block_id in block_ids:
                nursery.start_soon(delete_one, block_id)

    async def get_page_metadata(self, page_id: str) -> dict[str, Any]:
        """Fetch page properties only (no content blocks).

        This is more efficient than get_page() when you only need metadata
        like last_edited_time for change detection.

        Args:
           page_id: Notion page ID

        Returns:
           Page object with properties (no blocks)
        """
        logger.debug("Fetching page metadata: %s", page_id)
        result = await self._call_with_retry(self._client.pages.retrieve, page_id=page_id)
        return dict(result)

    async def get_page(self, page_id: str) -> dict[str, Any]:
        """Fetch page properties and all content blocks.

        Args:
           page_id: Notion page ID

        Returns:
           Dict with 'page' (properties) and 'blocks' (content) keys
        """
        logger.debug("Fetching page: %s", page_id)

        # Fetch page properties
        page = await self._call_with_retry(self._client.pages.retrieve, page_id=page_id)

        # Fetch all content blocks
        blocks = await self.get_block_children(page_id)

        return {"page": page, "blocks": blocks}

    async def get_database(self, database_id: str) -> dict[str, Any]:
        """Fetch database schema.

        Args:
           database_id: Notion database ID

        Returns:
           Database object with schema
        """
        logger.debug("Fetching database: %s", database_id)
        result = await self._call_with_retry(
            self._client.databases.retrieve, database_id=database_id
        )
        return dict(result)

    async def search(
        self,
        query: str = "",
        filter_type: str | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Search for pages and databases the integration has access to.

        Args:
           query: Search query string (empty for all accessible items)
           filter_type: Filter by object type ('page' or 'database')
           page_size: Number of results per page (max 100)

        Returns:
           List of page/database objects
        """
        logger.debug("Searching: query=%r, filter=%s", query, filter_type)

        results: list[dict[str, Any]] = []
        start_cursor: str | None = None

        while True:
            kwargs: dict[str, Any] = {"page_size": min(page_size, 100)}
            if query:
                kwargs["query"] = query
            if filter_type:
                kwargs["filter"] = {"value": filter_type, "property": "object"}
            if start_cursor:
                kwargs["start_cursor"] = start_cursor

            response = await self._call_with_retry(self._client.search, **kwargs)
            results.extend(response.get("results", []))

            if not response.get("has_more"):
                break
            start_cursor = response.get("next_cursor")

        return results

    async def update_database(
        self, database_id: str, title: str | None = None, properties: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Update database metadata.

        Args:
           database_id: Notion database ID
           title: New title for the database (optional)
           properties: Schema changes (optional)

        Returns:
           Updated database object
        """
        logger.debug("Updating database: %s (title=%s)", database_id, title)
        kwargs: dict[str, Any] = {"database_id": database_id}
        if title is not None:
            kwargs["title"] = [{"type": "text", "text": {"content": title}}]
        if properties is not None:
            kwargs["properties"] = properties
        result = await self._call_with_retry(self._client.databases.update, **kwargs)
        return dict(result)

    async def query_database(self, database_id: str) -> list[dict[str, Any]]:
        """List all rows in a database with pagination.

        Args:
           database_id: Notion database ID

        Returns:
           List of all page objects (database rows)
        """
        logger.debug("Querying database: %s", database_id)
        results: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "database_id": database_id,
                "page_size": 100,
            }
            if cursor:
                kwargs["start_cursor"] = cursor

            response = await self._call_with_retry(self._client.databases.query, **kwargs)

            results.extend(response.get("results", []))

            cursor = response.get("next_cursor")
            if not response.get("has_more") or cursor is None:
                break

        logger.debug("Queried database %s: %d rows", database_id, len(results))
        return results

    async def get_block_children(self, block_id: str) -> list[dict[str, Any]]:
        """List all child blocks with pagination.

        Args:
           block_id: Parent block/page ID

        Returns:
           List of all child block objects
        """
        logger.debug("Fetching block children: %s", block_id)
        results: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "block_id": block_id,
                "page_size": 100,
            }
            if cursor:
                kwargs["start_cursor"] = cursor

            response = await self._call_with_retry(self._client.blocks.children.list, **kwargs)

            results.extend(response.get("results", []))

            cursor = response.get("next_cursor")
            if not response.get("has_more") or cursor is None:
                break

        logger.debug("Fetched %d blocks from %s", len(results), block_id)
        return results

    async def update_page_properties(self, page_id: str, properties: dict[str, Any]) -> None:
        """Update page properties.

        Args:
           page_id: Notion page ID
           properties: Property name -> value mapping in Notion API format
        """
        logger.debug("Updating page properties: %s", page_id)
        await self._call_with_retry(
            self._client.pages.update, page_id=page_id, properties=properties
        )

    async def update_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        """Replace page content by erasing existing children and appending new blocks.

        Uses the erase_content flag for efficient bulk deletion instead of
        deleting blocks individually. For diff-based updates, use update_blocks_diff().

        Args:
           page_id: Notion page ID
           blocks: New block objects in Notion API format
        """
        logger.debug("Updating blocks for page: %s", page_id)

        # Erase all existing content in one API call (much more efficient than
        # deleting each block individually). Note: this "cannot be reversed using the API"
        await self._call_with_retry(
            self._client.pages.update, page_id=page_id, erase_content=True
        )

        # Append new blocks in chunks (Notion limits to 100 blocks per request)
        if blocks:
            for batch in _chunk_blocks(blocks):
                logger.debug("Appending %d blocks to %s", len(batch), page_id)
                await self._call_with_retry(
                    self._client.blocks.children.append,
                    block_id=page_id,
                    children=batch,
                )

    async def update_blocks_diff(
        self,
        page_id: str,
        to_delete: list[str],
        to_update: list[dict[str, Any]],
        to_append: list[dict[str, Any]],
    ) -> None:
        """Apply diff-based block updates.

        This is more efficient than update_blocks() as it preserves
        unchanged blocks and their IDs (important for comments, synced blocks).

        Args:
           page_id: Notion page ID
           to_delete: Block IDs to delete
           to_update: Blocks to update (must include 'id' key)
           to_append: New blocks to append at end
        """
        logger.debug(
            "Diff update for %s: delete=%d, update=%d, append=%d",
            page_id,
            len(to_delete),
            len(to_update),
            len(to_append),
        )

        # Delete removed blocks concurrently
        await self._delete_blocks_concurrent(to_delete)

        # Update modified blocks
        for block in to_update:
            upd_block_id: str | None = block.get("id")
            if not upd_block_id:
                logger.warning("Update block missing ID, skipping: %s", block)
                continue

            block_type: str | None = block.get("type")
            if not block_type:
                logger.warning("Update block missing type, skipping: %s", block)
                continue

            # Build update payload (exclude id and type from content)
            content = block.get(block_type, {})
            logger.debug("Updating block %s (%s)", upd_block_id, block_type)

            await self._call_with_retry(
                self._client.blocks.update, block_id=upd_block_id, **{block_type: content}
            )

        # Append new blocks in chunks (Notion limits to 100 blocks per request)
        if to_append:
            for batch in _chunk_blocks(to_append):
                logger.debug("Appending %d blocks to %s", len(batch), page_id)
                await self._call_with_retry(
                    self._client.blocks.children.append,
                    block_id=page_id,
                    children=batch,
                )

    async def delete_block(self, block_id: str) -> None:
        """Delete a single block.

        Args:
           block_id: Block ID to delete
        """
        logger.debug("Deleting block: %s", block_id)
        await self._call_with_retry(self._client.blocks.delete, block_id=block_id)

    async def update_block(self, block_id: str, block_type: str, content: dict[str, Any]) -> None:
        """Update a single block's content.

        Args:
           block_id: Block ID to update
           block_type: Block type (e.g., 'paragraph', 'heading_1')
           content: Block content in Notion API format
        """
        logger.debug("Updating block %s (%s)", block_id, block_type)
        await self._call_with_retry(
            self._client.blocks.update, block_id=block_id, **{block_type: content}
        )

    async def append_blocks(
        self, parent_id: str, blocks: list[dict[str, Any]], after: str | None = None
    ) -> None:
        """Append blocks to a parent (page or block).

        Args:
           parent_id: Parent page or block ID
           blocks: Block objects to append
           after: Optional block ID to insert after (instead of at end)
        """
        if not blocks:
            return
        # Chunk blocks to stay within Notion's 100-block limit
        for i, batch in enumerate(_chunk_blocks(blocks)):
            logger.debug("Appending %d blocks to %s", len(batch), parent_id)
            kwargs: dict[str, Any] = {"block_id": parent_id, "children": batch}
            # Only use 'after' for first chunk; subsequent chunks append after previous
            if after and i == 0:
                kwargs["after"] = after
            await self._call_with_retry(
                self._client.blocks.children.append,
                **kwargs,
            )

    async def create_page(
        self,
        parent_id: str,
        properties: dict[str, Any],
        children: list[dict[str, Any]] | None = None,
        parent_type: str = "page_id",
    ) -> str:
        """Create a new page.

        Args:
           parent_id: Parent page or database ID
           properties: Page properties in Notion API format
           children: Optional initial content blocks
           parent_type: 'page_id' or 'database_id'

        Returns:
           ID of the newly created page

        Raises:
           RuntimeError: If page creation response is missing ID
        """
        logger.debug("Creating page under %s (%s)", parent_id, parent_type)

        kwargs: dict[str, Any] = {
            "parent": {parent_type: parent_id},
            "properties": properties,
        }

        # Chunk children to respect Notion's 100-block limit per request
        children_chunks = _chunk_blocks(children or [])
        if children_chunks:
            kwargs["children"] = children_chunks[0]

        response = await self._call_with_retry(self._client.pages.create, **kwargs)
        new_id: str | None = response.get("id")
        if not new_id:
            raise RuntimeError(f"Page creation response missing 'id': {response}")
        logger.debug("Created page: %s", new_id)

        # Append remaining children chunks if any
        for chunk in children_chunks[1:]:
            logger.debug("Appending %d additional blocks to %s", len(chunk), new_id)
            await self._call_with_retry(
                self._client.blocks.children.append,
                block_id=new_id,
                children=chunk,
            )

        return new_id

    async def archive_page(self, page_id: str) -> None:
        """Archive (soft-delete) a page.

        Args:
           page_id: Notion page ID
        """
        logger.debug("Archiving page: %s", page_id)
        await self._call_with_retry(self._client.pages.update, page_id=page_id, archived=True)

    async def move_page(
        self, page_id: str, new_parent_id: str, parent_type: str = "page_id"
    ) -> None:
        """Move a page to a new parent.

        Uses the dedicated POST /pages/{page_id}/move endpoint.
        Note: pages.update cannot change parent ("A page's parent cannot be changed").

        Args:
           page_id: Notion page ID to move
           new_parent_id: New parent page or database ID
           parent_type: 'page_id' for pages, 'data_source_id' for databases
        """
        logger.debug("Moving page %s to %s (%s)", page_id, new_parent_id, parent_type)
        # SDK doesn't have move(), use raw request
        await self._call_with_retry(
            self._client.request,
            path=f"pages/{page_id}/move",
            method="POST",
            body={"parent": {"type": parent_type, parent_type: new_parent_id}},
        )

    async def list_comments(self, page_id: str) -> list[dict[str, Any]]:
        """List all comments on a page with pagination.

        Args:
           page_id: Notion page ID

        Returns:
           List of comment objects
        """
        logger.debug("Listing comments for page: %s", page_id)
        results: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "block_id": page_id,
                "page_size": 100,
            }
            if cursor:
                kwargs["start_cursor"] = cursor

            response = await self._call_with_retry(self._client.comments.list, **kwargs)

            results.extend(response.get("results", []))

            cursor = response.get("next_cursor")
            if not response.get("has_more") or cursor is None:
                break

        logger.debug("Listed %d comments for page %s", len(results), page_id)
        return results

    async def create_comment(self, page_id: str, text: str) -> dict[str, Any]:
        """Create a page-level comment.

        Args:
           page_id: Notion page ID
           text: Comment text content

        Returns:
           Created comment object
        """
        logger.debug("Creating comment on page: %s", page_id)
        response = await self._call_with_retry(
            self._client.comments.create,
            parent={"page_id": page_id},
            rich_text=[{"type": "text", "text": {"content": text}}],
        )
        return dict(response)

    async def create_reply(self, discussion_id: str, text: str) -> dict[str, Any]:
        """Reply to an existing discussion thread.

        Args:
           discussion_id: Discussion thread ID
           text: Reply text content

        Returns:
           Created comment object
        """
        logger.debug("Creating reply to discussion: %s", discussion_id)
        response = await self._call_with_retry(
            self._client.comments.create,
            discussion_id=discussion_id,
            rich_text=[{"type": "text", "text": {"content": text}}],
        )
        return dict(response)
