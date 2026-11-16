"""Integration tests for RhwpBackend + SafeWriter on real HWP sample copies."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Dict

import olefile
import pytest

from src.core.rhwp_backend import RhwpBackend
from src.core.safe_writer import SafeWriter


def _bindata_hashes(hwp_path: Path) -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    with olefile.OleFileIO(str(hwp_path)) as ole:
        for entry in ole.listdir():
            if entry and entry[0] == "BinData":
                key = "/".join(entry)
                data = ole.openstream(entry).read()
                hashes[key] = hashlib.sha256(data).hexdigest()
    return hashes


def test_replace_text_and_set_cell_preserve_bindata(sample_hwp_copy: Path):
    orig_bindata = _bindata_hashes(sample_hwp_copy)

    async def _run():
        backend = RhwpBackend()
        writer = SafeWriter(backend)
        try:
            info_before = await backend.call_tool("hwp_info", {"path": str(sample_hwp_copy)})
            assert info_before["format"] == "hwp5"

            # 1. Replace text via SafeWriter
            res1 = await writer.execute_write(
                sample_hwp_copy,
                lambda src, tmp: backend.call_tool(
                    "hwp_replace_text",
                    {
                        "path": str(src),
                        "output": str(tmp),
                        "find": "주문 신청서",
                        "replace": "주문 확인서",
                    },
                ),
            )
            assert res1["loss_count"] == 0

            # 2. Set cell via SafeWriter
            res2 = await writer.execute_write(
                sample_hwp_copy,
                lambda src, tmp: backend.call_tool(
                    "hwp_set_cell",
                    {
                        "path": str(src),
                        "output": str(tmp),
                        "table": 0,
                        "row": 0,
                        "col": 1,
                        "text": "테스트팀(홍길동)",
                    },
                ),
            )
            assert res2["changed"] is True
            assert res2["loss_count"] == 0

            # 3. Reopen and verify text & tables
            txt = await backend.call_tool("hwp_export_text", {"path": str(sample_hwp_copy)})
            full_text = "\n".join(p.get("text", "") for p in txt.get("pages", []))
            assert "주문 확인서" in full_text
            assert "테스트팀(홍길동)" in full_text
        finally:
            await backend.close()

    asyncio.run(_run())

    # 4. Verify OLE2 BinData streams are byte-identical
    assert olefile.isOleFile(str(sample_hwp_copy))
    new_bindata = _bindata_hashes(sample_hwp_copy)
    assert new_bindata == orig_bindata
