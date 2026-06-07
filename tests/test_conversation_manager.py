"""
Unit tests for conversation metadata integrity.

Tests corrupt JSON recovery, atomic writes, and concurrency safety
without requiring a real agy installation.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from modules.services.conversation_manager import (
    _load_metadata,
    _save_metadata,
)


@pytest.fixture
def tmp_metadata(tmp_path, monkeypatch):
    """Redirect METADATA_FILE to a temp directory for isolation."""
    meta_file = tmp_path / "mcp_metadata.json"
    monkeypatch.setattr(
        "modules.services.conversation_manager.METADATA_FILE", meta_file
    )
    monkeypatch.setattr(
        "modules.services.conversation_manager.CONVERSATIONS_DIR", tmp_path / "conversations"
    )
    return meta_file


class TestLoadMetadata:

    def test_returns_empty_when_no_file(self, tmp_metadata):
        assert _load_metadata() == {}

    def test_loads_valid_json(self, tmp_metadata):
        data = {"conv_1": {"title": "Test"}}
        tmp_metadata.write_text(json.dumps(data))
        assert _load_metadata() == data

    def test_corrupt_json_creates_backup(self, tmp_metadata):
        tmp_metadata.write_text("{invalid json!!")
        result = _load_metadata()
        assert result == {}
        backups = list(tmp_metadata.parent.glob("*.corrupt.*.json"))
        assert len(backups) == 1

    def test_corrupt_backup_preserves_content(self, tmp_metadata):
        corrupt_data = "{broken: true"
        tmp_metadata.write_text(corrupt_data)
        _load_metadata()
        backups = list(tmp_metadata.parent.glob("*.corrupt.*.json"))
        assert backups[0].read_text() == corrupt_data


class TestSaveMetadata:

    def test_saves_valid_json(self, tmp_metadata):
        data = {"conv_1": {"title": "Test"}}
        assert _save_metadata(data) is True
        loaded = json.loads(tmp_metadata.read_text())
        assert loaded == data

    def test_atomic_write_no_temp_file_left(self, tmp_metadata):
        _save_metadata({"key": "val"})
        tmp_files = list(tmp_metadata.parent.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_returns_false_on_write_error(self, tmp_metadata, monkeypatch):
        monkeypatch.setattr(
            "modules.services.conversation_manager.METADATA_FILE",
            Path("/nonexistent/path/metadata.json"),
        )
        assert _save_metadata({"key": "val"}) is False

    def test_roundtrip_preserves_data(self, tmp_metadata):
        data = {
            "conv_abc": {
                "title": "Hello",
                "tags": ["a", "b"],
                "created_at": 1234567890.0,
            }
        }
        _save_metadata(data)
        assert _load_metadata() == data


class TestConcurrency:

    @pytest.mark.asyncio
    async def test_concurrent_saves_no_corruption(self, tmp_metadata):
        """Multiple concurrent save operations should not corrupt the file."""
        _save_metadata({})

        async def write_entry(i: int):
            from modules.services.conversation_manager import _get_metadata_lock
            async with _get_metadata_lock():
                data = _load_metadata()
                data[f"conv_{i}"] = {"title": f"Conv {i}"}
                _save_metadata(data)

        await asyncio.gather(*[write_entry(i) for i in range(10)])

        final = _load_metadata()
        assert len(final) == 10
        for i in range(10):
            assert f"conv_{i}" in final
