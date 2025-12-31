"""Rich progress UI for sync operations."""

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from rich.console import Console, Group
from rich.live import Live
from rich.progress import BarColumn, Progress, TaskID, TextColumn
from rich.text import Text


class RequestStatus(Enum):
   """Status of an API request."""

   PENDING = "pending"
   SUCCESS = "success"
   RETRYING = "retrying"
   ERROR = "error"


@dataclass
class RequestInfo:
   """Information about an API request."""

   method: str
   endpoint: str
   status: RequestStatus = RequestStatus.PENDING
   status_code: int | None = None
   error: str | None = None

   def format_status(self) -> Text:
      """Format status for display."""
      if self.status == RequestStatus.SUCCESS:
         return Text(f"{self.status_code} OK", style="green")
      elif self.status == RequestStatus.RETRYING:
         error_text = f" ({self.error})" if self.error else ""
         return Text(f"Retrying...{error_text}", style="yellow")
      elif self.status == RequestStatus.ERROR:
         return Text(f"Error: {self.error}", style="red")
      else:
         return Text("...", style="dim")


# Type alias for request callback
RequestCallback = Callable[[RequestInfo], None] | None


class SyncProgress:
   """Progress UI for sync operations.

   Displays:
   - Progress bar showing entries processed
   - Current API request being made
   - Status of last request
   """

   def __init__(self, console: Console | None = None) -> None:
      self.console = console or Console(stderr=True)
      self._progress = Progress(
         TextColumn("[progress.description]{task.description}"),
         BarColumn(bar_width=20),
         TextColumn("{task.completed}/{task.total} entries"),
         console=self.console,
         expand=False,
      )
      self._task_id: TaskID | None = None
      self._last_request: RequestInfo | None = None
      self._live: Live | None = None
      self._operation: str = "Syncing"

   def _make_renderable(self) -> Group:
      """Build the combined renderable for Live display."""
      from rich.console import RenderableType

      renderables: list[RenderableType] = []

      # Progress bar
      renderables.append(self._progress)

      # Request info
      if self._last_request:
         req = self._last_request
         request_line = Text()
         request_line.append("Request: ", style="dim")
         request_line.append(f"{req.method} ", style="bold")
         request_line.append(req.endpoint, style="cyan")
         renderables.append(request_line)

         status_line = Text()
         status_line.append("Status:  ", style="dim")
         status_line.append_text(req.format_status())
         renderables.append(status_line)

      return Group(*renderables)

   def start(self, operation: str, total: int) -> None:
      """Start progress display.

      Args:
         operation: Description of the operation (e.g., "Pulling", "Pushing")
         total: Total number of entries to process
      """
      self._operation = operation
      self._task_id = self._progress.add_task(operation, total=total)
      self._live = Live(
         self._make_renderable(),
         console=self.console,
         refresh_per_second=4,
         transient=True,
      )
      self._live.start()

   def stop(self) -> None:
      """Stop progress display."""
      if self._live:
         self._live.stop()
         self._live = None

   def advance(self, count: int = 1) -> None:
      """Advance progress by count entries."""
      if self._task_id is not None:
         self._progress.advance(self._task_id, count)
         self._refresh()

   def set_total(self, total: int) -> None:
      """Update total entry count (for dynamic discovery)."""
      if self._task_id is not None:
         self._progress.update(self._task_id, total=total)
         self._refresh()

   def on_request(self, info: RequestInfo) -> None:
      """Handle request event from API client."""
      self._last_request = info
      self._refresh()

   def _refresh(self) -> None:
      """Refresh the live display."""
      if self._live:
         self._live.update(self._make_renderable())

   def __enter__(self) -> "SyncProgress":
      return self

   def __exit__(self, *args: object) -> None:
      self.stop()


def create_request_callback(progress: SyncProgress) -> RequestCallback:
   """Create a request callback bound to a SyncProgress instance."""
   return progress.on_request
