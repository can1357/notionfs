"""Pytest configuration and fixtures."""

from pathlib import Path

import pytest


@pytest.fixture
def tmp_cache_dir(tmp_path: Path) -> Path:
    """Create a temporary cache directory."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    return cache_dir
