"""End-to-end MCP stdio tests for hwp_mcp_stdio_server.py."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import JSONRPCRequest, JSONRPCError
from mcp.shared.message import SessionMessage

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = REPO_ROOT / "hwp_mcp_stdio_server.py"

EXPECTED_TOOLS = {
    "hwp_info",
    "hwp_get_text",
    "hwp_search",
    "hwp_table_view",
    "hwp_fill_cells",
    "hwp_replace_text",
    "hwp_fill_fields",
    "hwp_insert_text",
    "hwp_run_plan",
    "hwp_render_page",
    "hwp_history",
    "hwp_restore",
    "hwp_diff",
    "hwp_rhwp_call",
}


def test_mcp_e2e_tools_and_edit_workflow(sample_hwp_copy: Path, tmp_path: Path):
    async def _run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_SCRIPT)],
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                # 1. Verify tools/list
                tools_res = await session.list_tools()
                tool_names = {t.name for t in tools_res.tools}
                assert EXPECTED_TOOLS.issubset(tool_names)
                assert "hwp_create" not in tool_names

                # 2. hwp_info
                res_info = await session.call_tool("hwp_info", {"file_path": str(sample_hwp_copy)})
                assert not res_info.isError
                info_data = json.loads(res_info.content[0].text)
                assert info_data["format"] == "hwp5"
                sha_initial = info_data["sha256"]

                # 3. hwp_table_view
                res_tbl = await session.call_tool(
                    "hwp_table_view", {"file_path": str(sample_hwp_copy), "table_index": 0}
                )
                assert not res_tbl.isError
                tbl_data = json.loads(res_tbl.content[0].text)
                assert tbl_data["tableCount"] >= 1

                # 4. hwp_fill_cells (coordinate-based)
                res_fill = await session.call_tool(
                    "hwp_fill_cells",
                    {
                        "file_path": str(sample_hwp_copy),
                        "table_index": 0,
                        "cells": [{"row": 0, "col": 1, "text": "E2E테스트팀"}],
                        "expected_sha256": sha_initial,
                    },
                )
                assert not res_fill.isError
                fill_data = json.loads(res_fill.content[0].text)
                assert fill_data["changed"] is True
                assert fill_data["new_sha256"] != sha_initial

                # 4b. hwp_fill_cells (label-based)
                res_label = await session.call_tool(
                    "hwp_fill_cells",
                    {
                        "file_path": str(sample_hwp_copy),
                        "table_index": 0,
                        "cells": [{"label": "팀명(팀장 이름)", "text": "라벨테스트팀", "direction": "right"}],
                    },
                )
                assert not res_label.isError
                label_data = json.loads(res_label.content[0].text)
                assert label_data["changed"] is True

                # 4b-2. label lookup with no table given searches every table
                all_tables = json.loads(
                    (await session.call_tool("hwp_table_view", {"file_path": str(sample_hwp_copy)})).content[0].text
                )["tables"]
                first_texts = {c.get("text", "").strip() for c in all_tables[0].get("cells", [])}
                other_label = next(
                    c["text"].strip()
                    for t in all_tables[1:]
                    for c in t.get("cells", [])
                    if c.get("text", "").strip() and c["text"].strip() not in first_texts
                )
                res_any = await session.call_tool(
                    "hwp_fill_cells",
                    {"file_path": str(sample_hwp_copy), "cells": [{"label": other_label, "text": "전체표검색"}]},
                )
                assert not res_any.isError, res_any.content[0].text

                # 4c. hwp_rhwp_call (read escape hatch)
                res_wc = await session.call_tool(
                    "hwp_rhwp_call",
                    {"file_path": str(sample_hwp_copy), "tool_name": "hwp_word_count"},
                )
                assert not res_wc.isError

                # 5. hwp_get_text confirms edit
                res_txt = await session.call_tool(
                    "hwp_get_text", {"file_path": str(sample_hwp_copy), "format": "text"}
                )
                assert not res_txt.isError
                assert "라벨테스트팀" in res_txt.content[0].text

                # 6. hwp_history & hwp_diff against snapshot
                res_hist = await session.call_tool("hwp_history", {"file_path": str(sample_hwp_copy)})
                hist_data = json.loads(res_hist.content[0].text)
                assert hist_data["count"] >= 2
                # Oldest snapshot is initial state
                snap_id = hist_data["snapshots"][-1]["snapshot_id"]

                res_diff = await session.call_tool(
                    "hwp_diff",
                    {"file_path_a": str(sample_hwp_copy), "snapshot_id": snap_id},
                )
                diff_data = json.loads(res_diff.content[0].text)
                assert diff_data["identical"] is False
                assert "라벨테스트팀" in diff_data["diff"]

                # 7. hwp_render_page (svg)
                render_dir = tmp_path / "render_out"
                res_ren = await session.call_tool(
                    "hwp_render_page",
                    {
                        "file_path": str(sample_hwp_copy),
                        "page": 0,
                        "format": "svg",
                        "output_dir": str(render_dir),
                    },
                )
                assert not res_ren.isError
                ren_data = json.loads(res_ren.content[0].text)
                assert len(ren_data["files"]) >= 1

                # 8. hwp_restore back to initial snapshot
                res_rest = await session.call_tool(
                    "hwp_restore",
                    {"file_path": str(sample_hwp_copy), "snapshot_id": snap_id},
                )
                assert not res_rest.isError
                rest_data = json.loads(res_rest.content[0].text)
                assert rest_data["new_sha256"] == sha_initial

    asyncio.run(_run())


def test_server_discover_returns_method_not_found_without_validation_error():
    async def _run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_SCRIPT)],
        )
        async with stdio_client(params) as (read_stream, write_stream):
            req = SessionMessage(
                message=__import__("mcp.types", fromlist=["JSONRPCMessage"]).JSONRPCMessage(
                    JSONRPCRequest(jsonrpc="2.0", id=999, method="server/discover")
                )
            )
            await write_stream.send(req)
            resp = await asyncio.wait_for(read_stream.receive(), timeout=10.0)
            root = resp.message.root
            assert isinstance(root, JSONRPCError)
            assert root.id == 999
            assert root.error.code == -32601

    asyncio.run(_run())
