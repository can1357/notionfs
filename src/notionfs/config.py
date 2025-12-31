"""Configuration management."""

import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path


@dataclass
class CacheTTL:
    page_content: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    database_schema: timedelta = field(default_factory=lambda: timedelta(hours=1))
    directory_listing: timedelta = field(default_factory=lambda: timedelta(minutes=2))
    poll_interval: timedelta = field(default_factory=lambda: timedelta(minutes=1))
    write_retry_delay: timedelta = field(default_factory=lambda: timedelta(seconds=30))
    max_retries: int = 5


@dataclass
class Config:
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "notionfs")
    mcp_command: str = "bunx"
    mcp_args: list[str] = field(
        default_factory=lambda: ["-y", "mcp-remote", "https://mcp.notion.com/mcp"]
    )
    ttl: CacheTTL = field(default_factory=CacheTTL)
    debug: bool = False

    def save(self, path: Path | None = None) -> None:
        path = path or (Path.home() / ".config" / "notionfs" / "config.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cache_dir": str(self.cache_dir),
            "mcp_command": self.mcp_command,
            "mcp_args": self.mcp_args,
            "debug": self.debug,
            "ttl": {
                "page_content": self.ttl.page_content.total_seconds(),
                "database_schema": self.ttl.database_schema.total_seconds(),
                "directory_listing": self.ttl.directory_listing.total_seconds(),
                "poll_interval": self.ttl.poll_interval.total_seconds(),
                "write_retry_delay": self.ttl.write_retry_delay.total_seconds(),
                "max_retries": self.ttl.max_retries,
            },
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or (Path.home() / ".config" / "notionfs" / "config.json")
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return cls()

        ttl_data = data.get("ttl", {})
        ttl = CacheTTL(
            page_content=timedelta(seconds=ttl_data.get("page_content", 300)),
            database_schema=timedelta(seconds=ttl_data.get("database_schema", 3600)),
            directory_listing=timedelta(seconds=ttl_data.get("directory_listing", 120)),
            poll_interval=timedelta(seconds=ttl_data.get("poll_interval", 60)),
            write_retry_delay=timedelta(seconds=ttl_data.get("write_retry_delay", 30)),
            max_retries=ttl_data.get("max_retries", 5),
        )

        return cls(
            cache_dir=Path(data.get("cache_dir", str(Path.home() / ".cache" / "notionfs"))),
            mcp_command=data.get("mcp_command", "bunx"),
            mcp_args=data.get("mcp_args", ["-y", "mcp-remote", "https://mcp.notion.com/mcp"]),
            ttl=ttl,
            debug=data.get("debug", False),
        )
