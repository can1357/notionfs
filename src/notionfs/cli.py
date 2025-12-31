"""CLI entry point for notionfs - local-first Notion sync."""

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import click
import tomli_w
import trio
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from notionfs.notion.api_client import NotionAPIClient
from notionfs.sync.engine import SyncEngine, SyncResult, _compute_hash
from notionfs.sync.progress import SyncProgress, create_request_callback
from notionfs.sync.state import SyncState, SyncStatus
from notionfs.sync.watcher import SyncWatcher

# Load .env from current directory and parent directories
load_dotenv()

# Python 3.11+ has tomllib built-in
try:
   import tomllib
except ImportError:
   import tomli as tomllib  # type: ignore[import-not-found,no-redef]

console = Console(stderr=True)

# Config paths
GLOBAL_CONFIG_DIR = Path.home() / ".notionfs"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.toml"
WORKSPACE_CONFIG_DIR = ".notionfs"
WORKSPACE_CONFIG_FILE = "config.toml"
STATE_DB_FILE = "state.db"


def setup_logging(debug: bool = False) -> None:
   """Configure logging."""
   level = logging.DEBUG if debug else logging.WARNING
   logging.basicConfig(
      level=level,
      format="%(message)s",
      datefmt="[%X]",
      handlers=[RichHandler(console=console, rich_tracebacks=True)],
   )


def _extract_page_id(url_or_id: str) -> str:
   """Extract Notion page/database ID from URL or raw ID.

   Supports formats:
   - Full URL: https://www.notion.so/workspace/Page-Title-abc123def456...
   - Short URL: https://notion.so/abc123def456...
   - Raw ID: abc123def456... or abc123-def4-5678-...
   """
   # Strip whitespace
   url_or_id = url_or_id.strip()

   # If it looks like a URL, extract the ID from it
   if url_or_id.startswith("http"):
      # Extract last path segment, then the ID (32 hex chars, possibly with dashes)
      parts = url_or_id.rstrip("/").split("/")
      last_part = parts[-1]
      # Handle query params
      last_part = last_part.split("?")[0]
      # ID is typically at the end after a hyphen, or the whole segment
      # Format: Page-Title-abc123def456789012345678901234
      if "-" in last_part:
         # Try to find 32-char hex suffix
         match = re.search(r"([a-f0-9]{32})$", last_part.replace("-", ""))
         if match:
            return match.group(1)
         # Try with dashes (UUID format)
         match = re.search(
            r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$",
            last_part,
         )
         if match:
            return match.group(1).replace("-", "")
      # Maybe the whole segment is the ID
      clean = last_part.replace("-", "")
      if len(clean) == 32 and all(c in "0123456789abcdef" for c in clean.lower()):
         return clean.lower()
      raise click.ClickException(f"Could not extract page ID from URL: {url_or_id}")

   # Raw ID - normalize by removing dashes
   clean = url_or_id.replace("-", "").lower()
   if len(clean) == 32 and all(c in "0123456789abcdef" for c in clean):
      return clean
   raise click.ClickException(f"Invalid page ID format: {url_or_id}")


def _get_token() -> str | None:
   """Get Notion token from environment or global config."""
   # Environment variable takes precedence
   if token := os.environ.get("NOTION_TOKEN"):
      return token

   # Try global config
   if GLOBAL_CONFIG_FILE.exists():
      try:
         with open(GLOBAL_CONFIG_FILE, "rb") as f:
            config = tomllib.load(f)
         return config.get("token")
      except Exception:
         pass

   return None


def _require_token() -> str:
   """Get token or exit with error."""
   token = _get_token()
   if not token:
      console.print("[red]Error:[/red] No Notion token found.")
      console.print()
      console.print("Set the NOTION_TOKEN environment variable:")
      console.print("  [dim]export NOTION_TOKEN='secret_...'[/dim]")
      console.print()
      console.print("Or save it to global config:")
      console.print(f"  [dim]{GLOBAL_CONFIG_FILE}[/dim]")
      console.print("  [dim]token = 'secret_...'[/dim]")
      console.print()
      console.print("Get a token at: https://www.notion.so/my-integrations")
      sys.exit(1)
   return token


def _find_workspace_root() -> Path | None:
   """Find workspace root by looking for .notionfs directory."""
   current = Path.cwd().resolve()
   while current != current.parent:
      if (current / WORKSPACE_CONFIG_DIR / WORKSPACE_CONFIG_FILE).exists():
         return current
      current = current.parent
   return None


def _require_workspace() -> tuple[Path, dict[str, Any]]:
   """Find workspace and load config, or exit with error."""
   workspace = _find_workspace_root()
   if not workspace:
      console.print("[red]Error:[/red] Not in a notionfs workspace.")
      console.print()
      console.print("Initialize a workspace with:")
      console.print("  [dim]notionfs init <notion-url>[/dim]")
      sys.exit(1)

   config_path = workspace / WORKSPACE_CONFIG_DIR / WORKSPACE_CONFIG_FILE
   try:
      with open(config_path, "rb") as f:
         config = tomllib.load(f)
   except Exception as e:
      console.print(f"[red]Error:[/red] Failed to read workspace config: {e}")
      sys.exit(1)

   if "root_id" not in config:
      console.print("[red]Error:[/red] Workspace config missing 'root_id'.")
      sys.exit(1)

   return workspace, config


def _save_global_config(config: dict[str, Any]) -> None:
   """Save global config to ~/.notionfs/config.toml."""
   GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
   with open(GLOBAL_CONFIG_FILE, "wb") as f:
      tomli_w.dump(config, f)


def _save_workspace_config(workspace: Path, config: dict[str, Any]) -> None:
   """Save workspace config to .notionfs/config.toml."""
   config_dir = workspace / WORKSPACE_CONFIG_DIR
   config_dir.mkdir(parents=True, exist_ok=True)
   config_path = config_dir / WORKSPACE_CONFIG_FILE
   with open(config_path, "wb") as f:
      tomli_w.dump(config, f)


def _load_workspaces() -> list[dict[str, Any]]:
   """Load list of known workspaces from global config."""
   if not GLOBAL_CONFIG_FILE.exists():
      return []
   try:
      with open(GLOBAL_CONFIG_FILE, "rb") as f:
         config = tomllib.load(f)
      workspaces: list[dict[str, Any]] = config.get("workspaces", [])
      return workspaces
   except Exception:
      return []


def _register_workspace(path: Path, root_id: str, title: str) -> None:
   """Register workspace in global config."""
   config: dict[str, Any] = {}
   if GLOBAL_CONFIG_FILE.exists():
      try:
         with open(GLOBAL_CONFIG_FILE, "rb") as f:
            config = tomllib.load(f)
      except Exception:
         pass

   workspaces = config.get("workspaces", [])

   # Remove existing entry for this path
   path_str = str(path.resolve())
   workspaces = [w for w in workspaces if w.get("path") != path_str]

   # Add new entry
   workspaces.append({
      "path": path_str,
      "root_id": root_id,
      "title": title,
   })

   config["workspaces"] = workspaces
   _save_global_config(config)


@click.group()
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.version_option()
@click.pass_context
def cli(ctx: click.Context, debug: bool) -> None:
   """notionfs: Sync Notion pages as local markdown files."""
   ctx.ensure_object(dict)
   ctx.obj["debug"] = debug
   setup_logging(debug)


def _extract_title_from_page(page: dict[str, Any]) -> str:
   """Extract title from a page object."""
   # Check for title property (pages)
   props = page.get("properties", {})
   for prop in props.values():
      if isinstance(prop, dict) and prop.get("type") == "title":
         title_arr = prop.get("title", [])
         if title_arr:
            return "".join(t.get("plain_text", "") for t in title_arr)

   # Check for title array (databases)
   title_arr = page.get("title", [])
   if title_arr:
      return "".join(t.get("plain_text", "") for t in title_arr)

   return "Untitled"


def _select_root_interactively(token: str) -> tuple[str, str]:
   """Fetch accessible pages/databases and let user select one interactively.

   Returns:
      Tuple of (selected_id, object_type) where object_type is "page" or "database"
   """
   import questionary

   console.print("[dim]Fetching available pages and databases...[/dim]")

   results: list[dict[str, Any]] = []

   async def fetch_all() -> list[dict[str, Any]]:
      api_client = NotionAPIClient(token)
      return await api_client.search()

   results = trio.run(fetch_all)

   if not results:
      console.print("[red]Error:[/red] No pages or databases found.")
      console.print()
      console.print("Make sure your integration has access to at least one page.")
      console.print("Share pages with your integration: page → ... → Connections → Add")
      sys.exit(1)

   # Filter to root-level items only (parent is workspace, not nested under another page)
   root_items = [
      item for item in results
      if item.get("parent", {}).get("type") == "workspace"
   ]

   if not root_items:
      console.print("[red]Error:[/red] No root-level pages or databases found.")
      console.print()
      console.print("All accessible items are nested under other pages.")
      console.print("Share a top-level page with your integration, or provide a URL directly.")
      sys.exit(1)

   # Build choices list - value is (id, type) tuple
   choices: list[questionary.Choice] = []
   for item in root_items:
      obj_type = item.get("object", "page")
      title = _extract_title_from_page(item)
      item_id = item.get("id", "").replace("-", "")

      # Format: [type] Title (truncated ID)
      type_badge = "📄" if obj_type == "page" else "📊"
      label = f"{type_badge} {title}"
      if len(label) > 60:
         label = label[:57] + "..."

      choices.append(questionary.Choice(title=label, value=(item_id, obj_type)))

   console.print(f"[dim]Found {len(choices)} root-level items[/dim]")
   console.print()

   selected: tuple[str, str] | None = questionary.select(
      "Select a page or database to sync:",
      choices=choices,
      use_indicator=True,
      use_shortcuts=len(choices) <= 36,
   ).ask()

   if not selected:
      console.print("[yellow]Cancelled.[/yellow]")
      sys.exit(0)

   return selected


async def _detect_root_type(api_client: NotionAPIClient, root_id: str) -> str:
   """Detect whether a root ID is a page or database by trying both APIs.

   Args:
      api_client: NotionAPIClient instance.
      root_id: Notion page or database ID.

   Returns:
      "page" or "database"

   Raises:
      click.ClickException: If neither API succeeds.
   """
   # Try as page first (more common)
   try:
      await api_client.get_page(root_id)
      return "page"
   except Exception:
      pass

   # Try as database
   try:
      await api_client.get_database(root_id)
      return "database"
   except Exception:
      pass

   raise click.ClickException(
      f"Could not access '{root_id}' as page or database. "
      "Make sure it's shared with your integration."
   )


@cli.command()
@click.argument("url", required=False)
@click.option(
   "--path",
   "-p",
   type=click.Path(),
   default=None,
   help="Directory to create (default: derived from page title)",
)
@click.pass_context
def init(ctx: click.Context, url: str | None, path: str | None) -> None:
   """Initialize workspace from a Notion page URL.

   URL can be a full Notion URL, just the page/database ID, or omitted
   to select interactively from accessible pages.

   Creates a local directory and performs initial sync.
   """
   token = _require_token()

   # root_type: "page" or "database"
   root_type: str | None = None

   if url:
      root_id = _extract_page_id(url)
   else:
      root_id, root_type = _select_root_interactively(token)

   console.print(f"[dim]Initializing from ID: {root_id}[/dim]")

   async def do_init() -> None:
      nonlocal root_type

      # Set up progress UI
      progress = SyncProgress(console)
      api_client = NotionAPIClient(token, request_callback=create_request_callback(progress))

      # Detect type if not known (URL-based init)
      if root_type is None:
         root_type = await _detect_root_type(api_client, root_id)

      # Fetch metadata based on type
      try:
         if root_type == "database":
            db_data = await api_client.get_database(root_id)
            title = _extract_title_from_page(db_data)
            notion_url = db_data.get("url", "")
         else:
            page_data = await api_client.get_page(root_id)
            page = page_data["page"]
            title = _extract_title_from_page(page)
            notion_url = page.get("url", "")
      except Exception as e:
         console.print(f"[red]Error:[/red] Failed to fetch {root_type}: {e}")
         console.print()
         console.print(f"Make sure the {root_type} is shared with your integration.")
         sys.exit(1)

      # Determine workspace path
      if path:
         workspace_path = Path(path).resolve()
      else:
         # Sanitize title for directory name
         safe_title = re.sub(r'[<>:"/\\|?*]', "_", title)
         workspace_path = Path.cwd() / safe_title

      # Check if directory exists
      if workspace_path.exists():
         if (workspace_path / WORKSPACE_CONFIG_DIR).exists():
            console.print(
               f"[red]Error:[/red] Directory already initialized: {workspace_path}"
            )
            sys.exit(1)
         if any(workspace_path.iterdir()):
            console.print(
               f"[red]Error:[/red] Directory not empty: {workspace_path}"
            )
            sys.exit(1)

      # Create directory structure
      workspace_path.mkdir(parents=True, exist_ok=True)
      config_dir = workspace_path / WORKSPACE_CONFIG_DIR
      config_dir.mkdir(exist_ok=True)

      # Save workspace config (include root_type)
      workspace_config = {
         "root_id": root_id,
         "root_type": root_type,
         "notion_url": notion_url,
      }
      _save_workspace_config(workspace_path, workspace_config)

      # Initialize state database
      state_path = config_dir / STATE_DB_FILE
      state = SyncState(state_path)
      await state.initialize()

      # Create sync engine and do initial pull
      engine = SyncEngine(workspace_path, root_id, api_client, state, root_type=root_type)
      engine.set_progress(progress)

      console.print(f"[bold]Syncing: {title}[/bold]")
      progress.start("Syncing", total=1)
      try:
         result = await engine.pull()
      finally:
         progress.stop()

      await state.close()

      # Check for errors during pull
      if result.errors:
         console.print()
         console.print("[red]✗[/red] Initialization failed:")
         for error in result.errors:
            console.print(f"  {error}")
         sys.exit(1)

      # Register in global config
      _register_workspace(workspace_path, root_id, title)

      console.print()
      console.print(f"[green]✓[/green] Workspace initialized: {workspace_path}")
      console.print(f"  Created: {len(result.created)} files")
      if result.conflicts:
         console.print(f"  [yellow]Conflicts: {len(result.conflicts)}[/yellow]")

   trio.run(do_init)


@cli.command()
@click.option("--force", "-f", is_flag=True, help="Overwrite local changes on conflict")
@click.pass_context
def pull(ctx: click.Context, force: bool) -> None:
   """Pull remote changes from Notion.

   Downloads new and updated pages from Notion to local files.
   Conflicts are marked but not overwritten unless --force is used.
   """
   token = _require_token()
   workspace, config = _require_workspace()

   async def do_pull() -> None:
      state_path = workspace / WORKSPACE_CONFIG_DIR / STATE_DB_FILE
      state = SyncState(state_path)
      await state.initialize()

      # Set up progress UI
      progress = SyncProgress(console)
      api_client = NotionAPIClient(token, request_callback=create_request_callback(progress))
      root_type = config.get("root_type", "page")
      engine = SyncEngine(workspace, config["root_id"], api_client, state, root_type=root_type)
      engine.set_progress(progress)

      # Start with initial estimate (will be updated once tree is fetched)
      progress.start("Pulling", total=1)
      try:
         result = await engine.pull(force=force)
      finally:
         progress.stop()

      await state.close()

      if not any([result.created, result.updated, result.deleted, result.conflicts]):
         console.print("[green]✓[/green] Already up to date")
         return

      if result.created:
         console.print(f"[green]Created:[/green] {len(result.created)} files")
         for p in result.created[:5]:
            console.print(f"  [green]+[/green] {p}")
         if len(result.created) > 5:
            console.print(f"  ... and {len(result.created) - 5} more")

      if result.updated:
         console.print(f"[blue]Updated:[/blue] {len(result.updated)} files")
         for p in result.updated[:5]:
            console.print(f"  [blue]~[/blue] {p}")
         if len(result.updated) > 5:
            console.print(f"  ... and {len(result.updated) - 5} more")

      if result.deleted:
         console.print(f"[red]Deleted:[/red] {len(result.deleted)} files")
         for p in result.deleted[:5]:
            console.print(f"  [red]-[/red] {p}")
         if len(result.deleted) > 5:
            console.print(f"  ... and {len(result.deleted) - 5} more")

      if result.conflicts:
         console.print(f"[yellow]Conflicts:[/yellow] {len(result.conflicts)} files")
         for p in result.conflicts:
            console.print(f"  [yellow]![/yellow] {p}")
         console.print()
         console.print("Resolve conflicts with:")
         console.print("  [dim]notionfs resolve <path> --keep-local|--keep-remote[/dim]")

   trio.run(do_pull)


@cli.command()
@click.option("--force", "-f", is_flag=True, help="Overwrite remote changes on conflict")
@click.pass_context
def push(ctx: click.Context, force: bool) -> None:
   """Push local changes to Notion.

   Uploads new and modified local files to Notion.
   Conflicts are marked but not pushed unless --force is used.
   """
   token = _require_token()
   workspace, config = _require_workspace()

   async def do_push() -> None:
      state_path = workspace / WORKSPACE_CONFIG_DIR / STATE_DB_FILE
      state = SyncState(state_path)
      await state.initialize()

      # Set up progress UI
      progress = SyncProgress(console)
      api_client = NotionAPIClient(token, request_callback=create_request_callback(progress))
      root_type = config.get("root_type", "page")
      engine = SyncEngine(workspace, config["root_id"], api_client, state, root_type=root_type)
      engine.set_progress(progress)

      # Start with initial estimate (will be updated once files are scanned)
      progress.start("Pushing", total=1)
      try:
         result = await engine.push(force=force)
      finally:
         progress.stop()

      await state.close()

      if not any([result.created, result.updated, result.deleted, result.conflicts]):
         console.print("[green]✓[/green] Nothing to push")
         return

      if result.created:
         console.print(f"[green]Created:[/green] {len(result.created)} pages")
         for p in result.created[:5]:
            console.print(f"  [green]+[/green] {p}")
         if len(result.created) > 5:
            console.print(f"  ... and {len(result.created) - 5} more")

      if result.updated:
         console.print(f"[blue]Updated:[/blue] {len(result.updated)} pages")
         for p in result.updated[:5]:
            console.print(f"  [blue]~[/blue] {p}")
         if len(result.updated) > 5:
            console.print(f"  ... and {len(result.updated) - 5} more")

      if result.deleted:
         console.print(f"[red]Archived:[/red] {len(result.deleted)} pages")
         for p in result.deleted[:5]:
            console.print(f"  [red]-[/red] {p}")
         if len(result.deleted) > 5:
            console.print(f"  ... and {len(result.deleted) - 5} more")

      if result.conflicts:
         console.print(f"[yellow]Conflicts:[/yellow] {len(result.conflicts)} files")
         for p in result.conflicts:
            console.print(f"  [yellow]![/yellow] {p}")
         console.print()
         console.print("Resolve conflicts with:")
         console.print("  [dim]notionfs resolve <path> --keep-local|--keep-remote[/dim]")

   trio.run(do_push)


@cli.command()
@click.pass_context
def sync(ctx: click.Context) -> None:
   """Bidirectional sync: pull then push.

   First pulls remote changes, then pushes local changes.
   Conflicts must be resolved before they can be synced.
   """
   token = _require_token()
   workspace, config = _require_workspace()

   async def do_sync() -> None:
      state_path = workspace / WORKSPACE_CONFIG_DIR / STATE_DB_FILE
      state = SyncState(state_path)
      await state.initialize()

      # Set up progress UI
      progress = SyncProgress(console)
      api_client = NotionAPIClient(token, request_callback=create_request_callback(progress))
      root_type = config.get("root_type", "page")
      engine = SyncEngine(workspace, config["root_id"], api_client, state, root_type=root_type)
      engine.set_progress(progress)

      # Pull phase
      progress.start("Pulling", total=1)
      try:
         pull_result = await engine.pull()
      finally:
         progress.stop()

      # Push phase
      progress.start("Pushing", total=1)
      try:
         push_result = await engine.push()
      finally:
         progress.stop()

      result = SyncResult(pull=pull_result, push=push_result)
      await state.close()

      # Summarize results
      total_changes = (
         len(result.pull.created) + len(result.pull.updated) + len(result.pull.deleted) +
         len(result.push.created) + len(result.push.updated) + len(result.push.deleted)
      )
      all_conflicts = set(result.pull.conflicts) | set(result.push.conflicts)

      if total_changes == 0 and not all_conflicts:
         console.print("[green]✓[/green] Already in sync")
         return

      if result.pull.created or result.pull.updated or result.pull.deleted:
         console.print("[bold]Pulled:[/bold]")
         if result.pull.created:
            console.print(f"  [green]+{len(result.pull.created)}[/green] created")
         if result.pull.updated:
            console.print(f"  [blue]~{len(result.pull.updated)}[/blue] updated")
         if result.pull.deleted:
            console.print(f"  [red]-{len(result.pull.deleted)}[/red] deleted")

      if result.push.created or result.push.updated or result.push.deleted:
         console.print("[bold]Pushed:[/bold]")
         if result.push.created:
            console.print(f"  [green]+{len(result.push.created)}[/green] created")
         if result.push.updated:
            console.print(f"  [blue]~{len(result.push.updated)}[/blue] updated")
         if result.push.deleted:
            console.print(f"  [red]-{len(result.push.deleted)}[/red] archived")

      if all_conflicts:
         console.print()
         console.print(f"[yellow]Conflicts ({len(all_conflicts)}):[/yellow]")
         for p in sorted(all_conflicts):
            console.print(f"  [yellow]![/yellow] {p}")
         console.print()
         console.print(
            "Resolve with: [dim]notionfs resolve <path> --keep-local|--keep-remote[/dim]"
         )

   trio.run(do_sync)


@cli.command()
@click.option(
   "--interval",
   "-i",
   type=float,
   default=30.0,
   help="Remote polling interval in seconds (default: 30)",
)
@click.option(
   "--debounce",
   "-d",
   type=float,
   default=2.0,
   help="Local change debounce in seconds (default: 2)",
)
@click.pass_context
def watch(ctx: click.Context, interval: float, debounce: float) -> None:
   """Watch for changes and sync continuously.

   Watches local files for changes and polls Notion for remote updates.
   Press Ctrl+C to stop.
   """
   token = _require_token()
   workspace, config = _require_workspace()

   console.print(f"[bold]Watching:[/bold] {workspace}")
   console.print(f"  Local debounce:    {debounce}s")
   console.print(f"  Remote interval:   {interval}s")
   console.print()
   console.print("[dim]Press Ctrl+C to stop[/dim]")
   console.print()

   sync_count = {"push": 0, "pull": 0, "conflicts": 0}

   def on_sync(direction: str, changed: int, conflicts: int) -> None:
      """Callback for sync events."""
      from datetime import datetime

      timestamp = datetime.now().strftime("%H:%M:%S")
      sync_count[direction] += changed
      sync_count["conflicts"] = conflicts

      if direction == "push":
         console.print(f"[dim]{timestamp}[/dim] [blue]↑ Push:[/blue] {changed} changes")
      else:
         console.print(f"[dim]{timestamp}[/dim] [cyan]↓ Pull:[/cyan] {changed} changes")

      if conflicts > 0:
         console.print(f"         [yellow]⚠ {conflicts} conflicts[/yellow]")

   async def do_watch() -> None:
      state_path = workspace / WORKSPACE_CONFIG_DIR / STATE_DB_FILE
      state = SyncState(state_path)
      await state.initialize()

      api_client = NotionAPIClient(token)
      root_type = config.get("root_type", "page")
      engine = SyncEngine(workspace, config["root_id"], api_client, state, root_type=root_type)

      watcher = SyncWatcher(
         engine=engine,
         local_debounce=debounce,
         remote_interval=interval,
         on_sync=on_sync,
      )

      try:
         await watcher.run()
      finally:
         await state.close()

      console.print()
      console.print("[bold]Summary:[/bold]")
      console.print(f"  Pushed: {sync_count['push']} changes")
      console.print(f"  Pulled: {sync_count['pull']} changes")
      if sync_count["conflicts"] > 0:
         console.print(f"  [yellow]Conflicts: {sync_count['conflicts']}[/yellow]")

   trio.run(do_watch)


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
   """Show pending changes and conflicts.

   Displays files that have been modified locally or remotely,
   and any conflicts that need resolution.
   """
   workspace, config = _require_workspace()

   async def do_status() -> None:
      state_path = workspace / WORKSPACE_CONFIG_DIR / STATE_DB_FILE
      state = SyncState(state_path)
      await state.initialize()

      # Refresh status by comparing current file hashes with stored hashes
      entries = await state.list_entries()
      for entry in entries:
         abs_path = workspace / entry.path
         if not abs_path.exists():
            if entry.status not in (SyncStatus.deleted_local, SyncStatus.deleted_remote):
               entry.status = SyncStatus.deleted_local
               await state.set_entry(entry)
         else:
            local_content = abs_path.read_text(encoding="utf-8")
            local_hash = _compute_hash(local_content)
            if local_hash != entry.local_hash and entry.status == SyncStatus.clean:
               entry.status = SyncStatus.local_modified
               await state.set_entry(entry)

      # Re-fetch after refresh
      entries = await state.list_entries()
      await state.close()

      # Categorize entries
      local_modified = []
      remote_modified = []
      conflicts = []
      deleted_local = []
      deleted_remote = []

      for entry in entries:
         if entry.status == SyncStatus.local_modified:
            local_modified.append(entry.path)
         elif entry.status == SyncStatus.remote_modified:
            remote_modified.append(entry.path)
         elif entry.status == SyncStatus.conflict:
            conflicts.append(entry.path)
         elif entry.status == SyncStatus.deleted_local:
            deleted_local.append(entry.path)
         elif entry.status == SyncStatus.deleted_remote:
            deleted_remote.append(entry.path)

      total = len(local_modified) + len(remote_modified) + len(conflicts) + \
              len(deleted_local) + len(deleted_remote)

      if total == 0:
         console.print("[green]✓[/green] Workspace is clean")
         console.print(f"  {len(entries)} files tracked")
         return

      # Summary
      console.print("[bold]Status:[/bold]")
      if local_modified:
         console.print(f"  Modified locally:  [blue]{len(local_modified)}[/blue]")
      if remote_modified:
         console.print(f"  Modified remotely: [cyan]{len(remote_modified)}[/cyan]")
      if conflicts:
         console.print(f"  Conflicts:         [yellow]{len(conflicts)}[/yellow]")
      if deleted_local:
         console.print(f"  Deleted locally:   [red]{len(deleted_local)}[/red]")
      if deleted_remote:
         console.print(f"  Deleted remotely:  [magenta]{len(deleted_remote)}[/magenta]")
      console.print()

      # Details
      if local_modified:
         console.print("[bold]Local changes (push to sync):[/bold]")
         for p in local_modified:
            console.print(f"  [blue]M[/blue] {p}")

      if remote_modified:
         console.print("[bold]Remote changes (pull to sync):[/bold]")
         for p in remote_modified:
            console.print(f"  [cyan]M[/cyan] {p}")

      if conflicts:
         console.print("[bold]Conflicts (resolve manually):[/bold]")
         for p in conflicts:
            console.print(f"  [yellow]C[/yellow] {p}")

      if deleted_local:
         console.print("[bold]Deleted locally:[/bold]")
         for p in deleted_local:
            console.print(f"  [red]D[/red] {p}")

      if deleted_remote:
         console.print("[bold]Deleted remotely:[/bold]")
         for p in deleted_remote:
            console.print(f"  [magenta]D[/magenta] {p}")

   trio.run(do_status)


@cli.command()
@click.argument("path", type=click.Path())
@click.option("--keep-local", "resolution", flag_value="local", help="Keep local version")
@click.option("--keep-remote", "resolution", flag_value="remote", help="Keep remote version")
@click.option(
   "--keep-both", "resolution", flag_value="both", help="Keep both (creates .conflict copy)"
)
@click.pass_context
def resolve(ctx: click.Context, path: str, resolution: str | None) -> None:
   """Resolve a sync conflict.

   PATH is the file with a conflict.

   Resolution options:
     --keep-local   Use local version, overwrite remote
     --keep-remote  Use remote version, overwrite local
     --keep-both    Keep local as .conflict file, pull remote
   """
   if not resolution:
      console.print("[red]Error:[/red] Must specify resolution:")
      console.print("  --keep-local   Keep local version")
      console.print("  --keep-remote  Keep remote version")
      console.print("  --keep-both    Keep both versions")
      sys.exit(1)

   token = _require_token()
   workspace, config = _require_workspace()

   # Normalize path
   try:
      file_path = Path(path)
      if file_path.is_absolute():
         rel_path = str(file_path.relative_to(workspace))
      else:
         rel_path = str(file_path)
   except ValueError:
      console.print(f"[red]Error:[/red] Path not in workspace: {path}")
      sys.exit(1)

   async def do_resolve() -> None:
      state_path = workspace / WORKSPACE_CONFIG_DIR / STATE_DB_FILE
      state = SyncState(state_path)
      await state.initialize()

      entry = await state.get_entry(rel_path)
      if not entry:
         console.print(f"[red]Error:[/red] File not tracked: {rel_path}")
         await state.close()
         sys.exit(1)

      conflict_statuses = (SyncStatus.conflict, SyncStatus.deleted_local, SyncStatus.deleted_remote)
      if entry.status not in conflict_statuses:
         console.print(f"[yellow]Warning:[/yellow] File has no conflict: {rel_path}")
         console.print(f"  Status: {entry.status.value}")
         await state.close()
         return

      abs_path = workspace / rel_path

      if resolution == "local":
         # Mark as local_modified so push will overwrite remote
         await state.update_status(rel_path, SyncStatus.local_modified)
         console.print(f"[green]✓[/green] Will keep local version: {rel_path}")
         console.print("  Run [dim]notionfs push[/dim] to apply")

      elif resolution == "remote":
         # Pull fresh from remote
         api_client = NotionAPIClient(token)
         root_type = config.get("root_type", "page")
         engine = SyncEngine(workspace, config["root_id"], api_client, state, root_type=root_type)

         # Fetch and overwrite
         result = await engine.pull(force=True)

         if rel_path in result.updated or rel_path in result.created:
            console.print(f"[green]✓[/green] Restored remote version: {rel_path}")
         else:
            # Entry may have been resolved during pull
            console.print(f"[green]✓[/green] Resolved: {rel_path}")

      elif resolution == "both":
         # Create .conflict copy of local, then pull remote
         if abs_path.exists():
            conflict_path = abs_path.with_suffix(f".conflict{abs_path.suffix}")
            import shutil
            shutil.copy2(abs_path, conflict_path)
            console.print(f"[dim]Saved local to: {conflict_path.name}[/dim]")

         api_client = NotionAPIClient(token)
         root_type = config.get("root_type", "page")
         engine = SyncEngine(workspace, config["root_id"], api_client, state, root_type=root_type)
         await engine.pull(force=True)

         console.print(f"[green]✓[/green] Resolved: {rel_path}")
         console.print(f"  Local saved as: {conflict_path.name}")

      await state.close()

   trio.run(do_resolve)


@cli.command("list")
@click.pass_context
def list_workspaces(ctx: click.Context) -> None:
   """List initialized workspaces."""
   workspaces = _load_workspaces()

   if not workspaces:
      console.print("[dim]No workspaces registered.[/dim]")
      console.print()
      console.print("Initialize a workspace with:")
      console.print("  [dim]notionfs init <notion-url>[/dim]")
      return

   table = Table(title="Workspaces")
   table.add_column("Title", style="bold")
   table.add_column("Path")
   table.add_column("Status", justify="center")

   for ws in workspaces:
      path = Path(ws.get("path", ""))
      exists = path.exists() and (path / WORKSPACE_CONFIG_DIR).exists()
      status = "[green]✓[/green]" if exists else "[red]missing[/red]"
      table.add_row(
         ws.get("title", "Untitled"),
         str(path),
         status,
      )

   console.print(table)


@cli.command()
@click.argument("token_value", required=False)
@click.pass_context
def auth(ctx: click.Context, token_value: str | None) -> None:
   """Set or show Notion API token.

   If TOKEN is provided, saves it to global config.
   Otherwise, shows current token status.
   """
   if token_value:
      # Save token
      config: dict[str, Any] = {}
      if GLOBAL_CONFIG_FILE.exists():
         try:
            with open(GLOBAL_CONFIG_FILE, "rb") as f:
               config = tomllib.load(f)
         except Exception:
            pass

      config["token"] = token_value
      _save_global_config(config)
      console.print(f"[green]✓[/green] Token saved to {GLOBAL_CONFIG_FILE}")

   else:
      # Show status
      token = _get_token()
      if token:
         masked = token[:10] + "..." + token[-4:] if len(token) > 14 else "***"
         source = "environment" if os.environ.get("NOTION_TOKEN") else "config file"
         console.print(f"[green]✓[/green] Token configured ({source}): {masked}")
      else:
         console.print("[yellow]No token configured.[/yellow]")
         console.print()
         console.print("Set token with:")
         console.print("  [dim]notionfs auth secret_...[/dim]")
         console.print()
         console.print("Or set NOTION_TOKEN environment variable.")
         console.print()
         console.print("Get a token at: https://www.notion.so/my-integrations")


if __name__ == "__main__":
   cli()
