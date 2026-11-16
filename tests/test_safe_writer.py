"""Unit tests for SafeWriter."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from src.core.safe_writer import (
    FileLockedError,
    HashConflictError,
    SafeWriter,
    VerificationError,
    compute_sha256,
)


class _DummyBackend:
    def __init__(self, fail_reopen: bool = False, pages: int = 1):
        self.fail_reopen = fail_reopen
        self.pages = pages

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "hwp_info":
            if self.fail_reopen and ".hwp-mcp-tmp-" in str(arguments.get("path", "")):
                raise RuntimeError("corrupt temp file")
            return {"pageCount": self.pages, "paraCount": 5}
        return {}


def test_atomic_replace_and_snapshot_created(tmp_path: Path):
    target = tmp_path / "doc.hwp"
    target.write_bytes(b"ORIGINAL_CONTENT")
    orig_sha = compute_sha256(target)

    writer = SafeWriter(_DummyBackend())

    async def write_fn(src: Path, tmp_out: Path) -> Dict[str, Any]:
        assert tmp_out.parent == target.parent
        assert tmp_out.suffix == ".hwp"
        tmp_out.write_bytes(b"UPDATED_CONTENT")
        return {"losses": []}

    res = asyncio.run(writer.execute_write(target, write_fn))
    assert res["changed"] is True
    assert res["previous_sha256"] == orig_sha
    assert res["new_sha256"] == compute_sha256(target)
    assert target.read_bytes() == b"UPDATED_CONTENT"

    snap = Path(res["snapshot_path"])
    assert snap.is_file()
    assert snap.read_bytes() == b"ORIGINAL_CONTENT"
    assert snap.parent == tmp_path / ".hwp-mcp-history" / "doc.hwp"
    assert orig_sha[:8] in snap.name


def test_hash_conflict_expected_and_last_seen(tmp_path: Path):
    target = tmp_path / "doc.hwp"
    target.write_bytes(b"V1")
    writer = SafeWriter(_DummyBackend())
    writer.record_seen(target)

    async def write_fn(src: Path, tmp_out: Path) -> Dict[str, Any]:
        tmp_out.write_bytes(b"V2")
        return {"losses": []}

    # 1. Wrong expected_sha256 rejected
    with pytest.raises(HashConflictError):
        asyncio.run(writer.execute_write(target, write_fn, expected_sha256="0" * 64))

    # 2. External modification detected via last_seen_hashes
    target.write_bytes(b"EXTERNAL_EDIT")
    with pytest.raises(HashConflictError):
        asyncio.run(writer.execute_write(target, write_fn))

    # 3. force=True bypasses conflict check
    res = asyncio.run(writer.execute_write(target, write_fn, force=True))
    assert res["changed"] is True
    assert target.read_bytes() == b"V2"


def test_snapshot_retention_limit_50(tmp_path: Path):
    target = tmp_path / "doc.hwp"
    target.write_bytes(b"DATA")
    writer = SafeWriter(_DummyBackend())

    for i in range(55):
        target.write_bytes(f"DATA_{i}".encode())
        snap = writer.create_snapshot(target)
        # Ensure distinct mtime_ns ordering
        os.utime(snap, ns=(1_700_000_000_000_000_000 + i * 1_000_000, 1_700_000_000_000_000_000 + i * 1_000_000))
        writer._prune_snapshots(writer.history_dir_for(target))

    snaps = writer.list_snapshots(target)
    assert len(snaps) == 50


def test_rollback_on_loss_and_reopen_failure(tmp_path: Path):
    target = tmp_path / "doc.hwp"
    target.write_bytes(b"SAFE_ORIGINAL")

    writer = SafeWriter(_DummyBackend())

    async def lossy_write(src: Path, tmp_out: Path) -> Dict[str, Any]:
        tmp_out.write_bytes(b"LOSSY_DATA")
        return {"losses": [{"kind": "shape", "count": 1}]}

    with pytest.raises(VerificationError):
        asyncio.run(writer.execute_write(target, lossy_write))
    assert target.read_bytes() == b"SAFE_ORIGINAL"
    assert not list(tmp_path.glob(".hwp-mcp-tmp-*"))

    # allow_loss=True permits it
    res = asyncio.run(writer.execute_write(target, lossy_write, allow_loss=True))
    assert res["changed"] is True
    assert target.read_bytes() == b"LOSSY_DATA"

    # Reopen failure rolls back
    target.write_bytes(b"SAFE_ORIGINAL_2")
    bad_writer = SafeWriter(_DummyBackend(fail_reopen=True))

    async def ok_write(src: Path, tmp_out: Path) -> Dict[str, Any]:
        tmp_out.write_bytes(b"CORRUPT")
        return {"losses": []}

    with pytest.raises(VerificationError):
        asyncio.run(bad_writer.execute_write(target, ok_write))
    assert target.read_bytes() == b"SAFE_ORIGINAL_2"
    assert not list(tmp_path.glob(".hwp-mcp-tmp-*"))


def test_lock_error_retries_and_clear_message(tmp_path: Path):
    target = tmp_path / "locked.hwp"
    target.write_bytes(b"LOCKED_ORIGINAL")
    writer = SafeWriter(_DummyBackend())

    async def write_fn(src: Path, tmp_out: Path) -> Dict[str, Any]:
        tmp_out.write_bytes(b"NEW_DATA")
        return {"losses": []}

    call_count = 0

    def fake_replace(src, dst):
        nonlocal call_count
        call_count += 1
        raise PermissionError(13, "Sharing violation")

    with patch("src.core.safe_writer.os.replace", side_effect=fake_replace):
        with pytest.raises(FileLockedError) as exc_info:
            asyncio.run(writer.execute_write(target, write_fn, retry_delay=0.01))

    assert call_count == 4  # 1 initial + 3 retries
    assert "편집기가 파일을 잠그고 있어" in str(exc_info.value)
    assert target.read_bytes() == b"LOCKED_ORIGINAL"
    assert not list(tmp_path.glob(".hwp-mcp-tmp-*"))
