"""Watch mode for continuous local-first sync."""

import logging
import signal
from pathlib import Path
from typing import Callable

import trio
from watchfiles import Change, awatch

from notionfs.sync.engine import SyncEngine

logger = logging.getLogger(__name__)


class SyncWatcher:
   """Watches local files and polls remote for continuous sync."""

   def __init__(
      self,
      engine: SyncEngine,
      local_debounce: float = 2.0,
      remote_interval: float = 30.0,
      on_sync: Callable[[str, int, int], None] | None = None,
   ) -> None:
      """Initialize watcher.

      Args:
          engine: SyncEngine instance.
          local_debounce: Seconds to wait after last local change before syncing.
          remote_interval: Seconds between remote polling.
          on_sync: Callback(direction, changed, conflicts) for sync events.
      """
      self.engine = engine
      self.local_debounce = local_debounce
      self.remote_interval = remote_interval
      self.on_sync = on_sync
      self._shutdown_event: trio.Event | None = None
      self._pending_local_sync: trio.Event | None = None

   def _should_ignore(self, path: Path) -> bool:
      """Check if path should be ignored."""
      # Ignore .notionfs directory
      if ".notionfs" in path.parts:
         return True
      # Ignore hidden files/directories
      for part in path.parts:
         if part.startswith(".") and part != ".":
            return True
      # Only watch markdown files
      if path.suffix.lower() != ".md":
         return True
      return False

   async def run(self) -> None:
      """Run watch loop until signaled to stop."""
      self._shutdown_event = trio.Event()
      self._pending_local_sync = trio.Event()

      # Install signal handlers in a cross-platform way
      try:
         with trio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signal_aiter:

            async def signal_handler() -> None:
               async for sig in signal_aiter:
                  logger.info("Received signal %s, shutting down...", sig)
                  if self._shutdown_event:
                     self._shutdown_event.set()
                  break

            async with trio.open_nursery() as nursery:
               nursery.start_soon(signal_handler)
               nursery.start_soon(self._watch_local)
               nursery.start_soon(self._poll_remote)
               nursery.start_soon(self._local_sync_worker)

               # Wait for shutdown signal
               await self._shutdown_event.wait()
               nursery.cancel_scope.cancel()

      except Exception as e:
         logger.error("Watch loop error: %s", e)
         raise

   async def stop(self) -> None:
      """Signal the watcher to stop."""
      if self._shutdown_event:
         self._shutdown_event.set()

   async def _watch_local(self) -> None:
      """Watch local filesystem for changes."""
      logger.debug("Starting local file watcher on %s", self.engine.workspace_path)

      async for changes in awatch(
         self.engine.workspace_path,
         stop_event=self._shutdown_event,
         debounce=int(self.local_debounce * 1000),  # watchfiles uses milliseconds
         rust_timeout=1000,  # Check shutdown every second
      ):
         if self._shutdown_event and self._shutdown_event.is_set():
            break

         # Filter changes
         relevant_changes: list[tuple[Change, str]] = []
         for change_type, path_str in changes:
            path = Path(path_str)
            try:
               rel_path = path.relative_to(self.engine.workspace_path)
            except ValueError:
               continue
            if not self._should_ignore(rel_path):
               relevant_changes.append((change_type, str(rel_path)))

         if not relevant_changes:
            continue

         logger.debug("Local changes detected: %s", relevant_changes)

         # Trigger sync
         if self._pending_local_sync:
            self._pending_local_sync.set()

   async def _local_sync_worker(self) -> None:
      """Worker that processes pending local syncs."""
      while True:
         if self._shutdown_event and self._shutdown_event.is_set():
            break

         # Wait for a change or shutdown
         if self._pending_local_sync:
            await self._pending_local_sync.wait()
            # Clear by creating new event atomically before any await
            self._pending_local_sync = trio.Event()

         if self._shutdown_event and self._shutdown_event.is_set():
            break

         # Push local changes
         try:
            logger.info("Pushing local changes...")
            result = await self.engine.push()
            total_changes = len(result.created) + len(result.updated) + len(result.deleted)
            if total_changes > 0 or result.conflicts:
               logger.info(
                  "Push: +%d ~%d -%d conflicts=%d",
                  len(result.created),
                  len(result.updated),
                  len(result.deleted),
                  len(result.conflicts),
               )
               if self.on_sync:
                  self.on_sync("push", total_changes, len(result.conflicts))
            else:
               logger.debug("Push: no changes")
         except Exception as e:
            logger.error("Push failed: %s", e)

   async def _poll_remote(self) -> None:
      """Periodically poll remote for changes."""
      logger.debug("Starting remote polling (interval=%ds)", self.remote_interval)

      while True:
         if self._shutdown_event and self._shutdown_event.is_set():
            break

         # Wait for interval or shutdown
         with trio.move_on_after(self.remote_interval):
            if self._shutdown_event:
               await self._shutdown_event.wait()
               break  # Shutdown signaled

         if self._shutdown_event and self._shutdown_event.is_set():
            break

         # Pull remote changes
         try:
            logger.debug("Polling remote for changes...")
            result = await self.engine.pull()
            total_changes = len(result.created) + len(result.updated) + len(result.deleted)
            if total_changes > 0 or result.conflicts:
               logger.info(
                  "Pull: +%d ~%d -%d conflicts=%d",
                  len(result.created),
                  len(result.updated),
                  len(result.deleted),
                  len(result.conflicts),
               )
               if self.on_sync:
                  self.on_sync("pull", total_changes, len(result.conflicts))
            else:
               logger.debug("Pull: no changes")
         except Exception as e:
            logger.error("Pull failed: %s", e)
