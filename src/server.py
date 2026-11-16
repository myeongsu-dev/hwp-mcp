"""Headless HWP/HWPX MCP server backed by rhwp and SafeWriter."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import ErrorData, JSONRPCError, JSONRPCMessage, JSONRPCRequest, METHOD_NOT_FOUND

from src.core.rhwp_backend import RhwpBackend, RhwpError
from src.core.safe_writer import SafeWriteError, SafeWriter
from src.tools.hwp_reader import read_hwp

logger = logging.getLogger("hwp-mcp.server")

WRITE_TOOL_PREFIXES = (
    "hwp_set_",
    "hwp_insert_",
    "hwp_delete_",
    "hwp_merge_",
    "hwp_split_",
    "hwp_replace_",
    "hwp_fill_",
    "hwp_apply_",
    "hwp_fit_",
    "hwp_resize_",
    "hwp_move_",
    "hwp_transpose_",
)
WRITE_TOOL_NAMES = {
    "hwp_csv_to_table",
    "hwp_run_plan",
    "hwp_redact",
    "hwp_sanitize",
    "hwp_table_from_csv",
}


def _is_write_tool(tool_name: str, is_write_flag: bool) -> bool:
    if is_write_flag:
        return True
    if tool_name in WRITE_TOOL_NAMES:
        return True
    return tool_name.startswith(WRITE_TOOL_PREFIXES)


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _resolve_existing_file(file_path: str) -> Path:
    p = Path(file_path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {p}")
    return p


def create_server(backend: Optional[RhwpBackend] = None) -> tuple[FastMCP, RhwpBackend, SafeWriter]:
    """Create and configure the FastMCP server, backend, and SafeWriter."""
    rhwp = backend or RhwpBackend()
    writer = SafeWriter(rhwp)

    mcp = FastMCP(
        "hwp-mcp",
        instructions=(
            "한컴오피스 GUI/COM 없이 rhwp 엔진으로 .hwp/.hwpx 문서를 안전하게 읽고 편집하는 "
            "헤드리스 MCP 서버입니다. 모든 도구는 file_path 를 필수로 받으며, 모든 쓰기 작업은 "
            "SafeWriter(해시 충돌 검사, .hwp-mcp-history 스냅샷, 재열기 무손실 검증, 원자 교체)를 경유합니다."
        ),
    )

    @mcp.tool(
        description="HWP/HWPX 문서의 메타데이터(형식, 페이지 수, 문단 수, 글꼴, SHA-256 해시 등)를 조회합니다."
    )
    async def hwp_info(file_path: str, password: Optional[str] = None) -> str:
        target = _resolve_existing_file(file_path)
        sha = writer.record_seen(target)
        info = await rhwp.call_tool("hwp_info", {"path": str(target), "password": password})
        if isinstance(info, dict):
            info["file_path"] = str(target)
            info["sha256"] = sha
        return _json_dumps(info)

    @mcp.tool(
        description="HWP/HWPX 문서의 본문 텍스트를 추출합니다. format='text' 또는 'markdown'을 지원하며, rhwp 실패 시 .hwp 에 대해 내장 OLE 리더로 폴백합니다."
    )
    async def hwp_get_text(
        file_path: str,
        format: str = "text",
        page: Optional[int] = None,
        max_chars: Optional[int] = None,
        password: Optional[str] = None,
    ) -> str:
        target = _resolve_existing_file(file_path)
        writer.record_seen(target)
        fmt = format.lower().strip()
        if fmt not in ("text", "markdown"):
            raise ValueError("format 은 'text' 또는 'markdown' 이어야 합니다.")

        try:
            if fmt == "markdown":
                with tempfile.TemporaryDirectory() as td:
                    res = await rhwp.call_tool(
                        "hwp_export_markdown",
                        {"path": str(target), "output": td, "password": password},
                    )
                    pages_meta = res.get("pages", []) if isinstance(res, dict) else []
                    chunks: List[str] = []
                    if pages_meta:
                        for pm in sorted(pages_meta, key=lambda x: x.get("page", 0)):
                            if page is not None and pm.get("page") != page:
                                continue
                            md_p = Path(pm["path"])
                            if md_p.is_file():
                                chunks.append(md_p.read_text(encoding="utf-8"))
                    else:
                        for md_p in sorted(Path(td).glob("*.md")):
                            chunks.append(md_p.read_text(encoding="utf-8"))
                    content = "\n\n".join(chunks)
                    if max_chars is not None and max_chars > 0:
                        content = content[:max_chars]
                    return _json_dumps(
                        {
                            "file_path": str(target),
                            "format": "markdown",
                            "pageCount": res.get("pageCount") if isinstance(res, dict) else None,
                            "content": content,
                        }
                    )
            else:
                res = await rhwp.call_tool(
                    "hwp_export_text",
                    {
                        "path": str(target),
                        "page": page,
                        "maxChars": max_chars,
                        "password": password,
                    },
                )
                if isinstance(res, dict):
                    res["file_path"] = str(target)
                    res["format"] = "text"
                return _json_dumps(res)
        except Exception as exc:
            if target.suffix.lower() == ".hwp" and not password:
                logger.warning("rhwp text extraction failed (%s), falling back to olefile reader", exc)
                fallback_text = read_hwp(str(target))
                if max_chars is not None and max_chars > 0:
                    fallback_text = fallback_text[:max_chars]
                return _json_dumps(
                    {
                        "file_path": str(target),
                        "format": fmt,
                        "fallback": "olefile_hwp_reader",
                        "rhwp_error": str(exc),
                        "content": fallback_text,
                    }
                )
            raise

    @mcp.tool(
        description="문서 내에서 특정 문자열을 검색하여 페이지 번호, 문맥, 표 셀 좌표를 반환합니다."
    )
    async def hwp_search(
        file_path: str,
        query: str,
        context: Optional[int] = None,
        password: Optional[str] = None,
    ) -> str:
        target = _resolve_existing_file(file_path)
        writer.record_seen(target)
        res = await rhwp.call_tool(
            "hwp_search",
            {"path": str(target), "query": query, "context": context, "password": password},
        )
        return _json_dumps(res)

    @mcp.tool(
        description="문서 내 표(Table)의 행/열 크기, 병합 정보, 각 셀의 좌표(row, col) 및 텍스트를 반환합니다. table_index(0부터 시작)로 특정 표만 조회할 수 있습니다."
    )
    async def hwp_table_view(
        file_path: str,
        table_index: Optional[int] = None,
        password: Optional[str] = None,
    ) -> str:
        target = _resolve_existing_file(file_path)
        writer.record_seen(target)
        res = await rhwp.call_tool(
            "hwp_export_tables",
            {"path": str(target), "password": password},
        )
        if table_index is not None and isinstance(res, dict):
            tables = res.get("tables", [])
            if table_index < 0 or table_index >= len(tables):
                raise ValueError(
                    f"table_index={table_index} 가 범위를 벗어났습니다 (전체 표 개수: {len(tables)})."
                )
            res = {
                "file_path": str(target),
                "tableCount": len(tables),
                "table_index": table_index,
                "table": tables[table_index],
            }
        return _json_dumps(res)

    @mcp.tool(
        description=(
            "표의 셀 값을 일괄 채웁니다. 각 항목은 좌표 기준({'row': 0, 'col': 1, 'text': '값'}) "
            "또는 라벨 기준({'label': '성명', 'text': '홍길동', 'direction': 'right'|'below'})을 지원합니다. "
            "라벨 기준에서 표 번호('table' 또는 table_index)를 생략하면 모든 표를 앞에서부터 찾아 첫 일치를 씁니다. "
            "좌표 기준에서 표 번호를 생략하면 0번 표입니다. "
            "SafeWriter를 통해 스냅샷 백업 및 무손실 검증 후 원자 교체합니다."
        )
    )
    async def hwp_fill_cells(
        file_path: str,
        cells: List[Dict[str, Any]],
        table_index: Optional[int] = None,
        expected_sha256: Optional[str] = None,
        force: bool = False,
        allow_loss: bool = False,
        password: Optional[str] = None,
    ) -> str:
        target = _resolve_existing_file(file_path)
        if not cells:
            raise ValueError("cells 목록이 비어 있습니다.")

        needs_label_lookup = any("label" in c for c in cells)
        tables_data: Optional[List[Dict[str, Any]]] = None
        if needs_label_lookup:
            exported = await rhwp.call_tool(
                "hwp_export_tables", {"path": str(target), "password": password}
            )
            tables_data = exported.get("tables", []) if isinstance(exported, dict) else []

        resolved_ops: List[Dict[str, Any]] = []
        for item in cells:
            explicit_tbl = item.get("table", table_index)
            tbl = int(explicit_tbl) if explicit_tbl is not None else 0
            text = str(item["text"])
            keep_style = bool(item.get("keep_style", True))

            if "label" in item:
                assert tables_data is not None
                label = str(item["label"])
                direction = str(item.get("direction", "right")).lower()
                if direction not in ("right", "below"):
                    raise ValueError("direction 은 'right' 또는 'below' 이어야 합니다.")
                occ = int(item.get("occurrence", 0))
                if explicit_tbl is not None:
                    if tbl < 0 or tbl >= len(tables_data):
                        raise ValueError(
                            f"table={tbl} 이 범위를 벗어났습니다 (전체 표 개수: {len(tables_data)})."
                        )
                    search_order = [tbl]
                else:
                    search_order = list(range(len(tables_data)))

                # Exact text matches win over substring matches, across every searched table.
                def _matches(exact: bool) -> List[Any]:
                    found = []
                    for t in search_order:
                        for c in tables_data[t].get("cells", []):
                            cell_text = c.get("text", "")
                            hit = cell_text.strip() == label.strip() if exact else label in cell_text
                            if hit:
                                found.append((t, c))
                    return found

                matches = _matches(True) or _matches(False)
                if occ >= len(matches):
                    where = f"표 {tbl}" if explicit_tbl is not None else "모든 표"
                    raise ValueError(
                        f"{where}에서 라벨 '{label}'(occurrence={occ})을 찾을 수 없습니다."
                    )
                tbl, anchor = matches[occ]
                a_row = int(anchor["row"])
                a_col = int(anchor["col"])
                row_span = int(anchor.get("rowSpan", 1))
                col_span = int(anchor.get("colSpan", 1))
                t_row = a_row if direction == "right" else a_row + row_span
                t_col = a_col + col_span if direction == "right" else a_col
            else:
                if "row" not in item or "col" not in item:
                    raise ValueError("각 셀 항목에는 ('row', 'col') 또는 'label' 이 필요합니다.")
                t_row = int(item["row"])
                t_col = int(item["col"])

            resolved_ops.append(
                {
                    "table": tbl,
                    "row": t_row,
                    "col": t_col,
                    "text": text,
                    "keepStyle": keep_style,
                }
            )

        async def _do_write(src: Path, tmp_out: Path) -> Dict[str, Any]:
            if len(resolved_ops) == 1:
                op = resolved_ops[0]
                return await rhwp.call_tool(
                    "hwp_set_cell",
                    {
                        "path": str(src),
                        "output": str(tmp_out),
                        "table": op["table"],
                        "row": op["row"],
                        "col": op["col"],
                        "text": op["text"],
                        "password": password,
                    },
                )
            plan = {
                "planVersion": 1,
                "input": str(src),
                "output": str(tmp_out),
                "steps": [
                    {
                        "action": "set_cell",
                        "table": op["table"],
                        "row": op["row"],
                        "col": op["col"],
                        "text": op["text"],
                        "keepStyle": op["keepStyle"],
                    }
                    for op in resolved_ops
                ],
            }
            return await rhwp.call_tool("hwp_run_plan", {"plan": plan})

        result = await writer.execute_write(
            target,
            _do_write,
            expected_sha256=expected_sha256,
            force=force,
            allow_loss=allow_loss,
        )
        result["resolved_cells"] = resolved_ops
        return _json_dumps(result)

    @mcp.tool(
        description="문서 본문 및 표 셀 내의 텍스트를 찾아 일괄 치환합니다. SafeWriter를 경유합니다."
    )
    async def hwp_replace_text(
        file_path: str,
        find: str,
        replace: str,
        expected_sha256: Optional[str] = None,
        force: bool = False,
        allow_loss: bool = False,
        password: Optional[str] = None,
    ) -> str:
        target = _resolve_existing_file(file_path)

        async def _do_write(src: Path, tmp_out: Path) -> Dict[str, Any]:
            return await rhwp.call_tool(
                "hwp_replace_text",
                {
                    "path": str(src),
                    "output": str(tmp_out),
                    "find": find,
                    "replace": replace,
                    "password": password,
                },
            )

        result = await writer.execute_write(
            target,
            _do_write,
            expected_sha256=expected_sha256,
            force=force,
            allow_loss=allow_loss,
        )
        return _json_dumps(result)

    @mcp.tool(
        description="문서 내 누름틀(필드) 값을 이름 기준으로 채웁니다. SafeWriter를 경유합니다."
    )
    async def hwp_fill_fields(
        file_path: str,
        data: Dict[str, str],
        expected_sha256: Optional[str] = None,
        force: bool = False,
        allow_loss: bool = False,
        password: Optional[str] = None,
    ) -> str:
        target = _resolve_existing_file(file_path)

        async def _do_write(src: Path, tmp_out: Path) -> Dict[str, Any]:
            return await rhwp.call_tool(
                "hwp_fill_fields",
                {
                    "path": str(src),
                    "output": str(tmp_out),
                    "data": data,
                    "password": password,
                },
            )

        result = await writer.execute_write(
            target,
            _do_write,
            expected_sha256=expected_sha256,
            force=force,
            allow_loss=allow_loss,
        )
        return _json_dumps(result)

    @mcp.tool(
        description="지정한 섹션/문단/문자 오프셋 위치에 텍스트를 삽입합니다. SafeWriter를 경유합니다."
    )
    async def hwp_insert_text(
        file_path: str,
        text: str,
        section: int = 0,
        paragraph: int = 0,
        offset: int = 0,
        expected_sha256: Optional[str] = None,
        force: bool = False,
        allow_loss: bool = False,
    ) -> str:
        target = _resolve_existing_file(file_path)

        async def _do_write(src: Path, tmp_out: Path) -> Dict[str, Any]:
            return await rhwp.call_tool(
                "hwp_insert_text",
                {
                    "path": str(src),
                    "output": str(tmp_out),
                    "text": text,
                    "section": section,
                    "paragraph": paragraph,
                    "offset": offset,
                },
            )

        result = await writer.execute_write(
            target,
            _do_write,
            expected_sha256=expected_sha256,
            force=force,
            allow_loss=allow_loss,
        )
        return _json_dumps(result)

    @mcp.tool(
        description="rhwp 선언적 편집 계획서(plan JSON)를 일괄 실행합니다. input/output 경로는 SafeWriter가 안전하게 바인딩합니다."
    )
    async def hwp_run_plan(
        file_path: str,
        plan: Dict[str, Any],
        expected_sha256: Optional[str] = None,
        force: bool = False,
        allow_loss: bool = False,
    ) -> str:
        target = _resolve_existing_file(file_path)

        async def _do_write(src: Path, tmp_out: Path) -> Dict[str, Any]:
            bound_plan = dict(plan)
            bound_plan.setdefault("planVersion", 1)
            bound_plan["input"] = str(src)
            bound_plan["output"] = str(tmp_out)
            bound_plan["dryRun"] = False
            return await rhwp.call_tool("hwp_run_plan", {"plan": bound_plan})

        result = await writer.execute_write(
            target,
            _do_write,
            expected_sha256=expected_sha256,
            force=force,
            allow_loss=allow_loss,
        )
        return _json_dumps(result)

    @mcp.tool(
        description="문서 페이지를 PNG 또는 SVG 이미지 파일로 렌더링하여 저장 경로를 반환합니다(시각 확인용). page는 0부터 시작하며, -1이면 전체 페이지를 렌더합니다."
    )
    async def hwp_render_page(
        file_path: str,
        page: int = 0,
        format: str = "png",
        output_dir: Optional[str] = None,
    ) -> str:
        target = _resolve_existing_file(file_path)
        fmt = format.lower().strip()
        if fmt not in ("png", "svg"):
            raise ValueError("format 은 'png' 또는 'svg' 이어야 합니다.")

        out_path = (
            Path(output_dir).expanduser().resolve()
            if output_dir
            else (target.parent / ".hwp-mcp-render" / target.stem)
        )
        out_path.mkdir(parents=True, exist_ok=True)

        cmd = "export-png" if fmt == "png" else "export-svg"
        args = [cmd, str(target), "-o", str(out_path), "--json"]
        if page >= 0:
            args.extend(["-p", str(page)])

        res = await rhwp.run_cli(args, timeout=60.0)
        rendered_files = sorted(str(p) for p in out_path.glob(f"*.{fmt}"))
        return _json_dumps(
            {
                "file_path": str(target),
                "format": fmt,
                "page": page,
                "output_dir": str(out_path),
                "files": rendered_files,
                "rhwp_result": res,
            }
        )

    @mcp.tool(
        description="문서의 SafeWriter 스냅샷 히스토리(.hwp-mcp-history) 목록을 최신순으로 조회합니다."
    )
    async def hwp_history(file_path: str) -> str:
        target = _resolve_existing_file(file_path)
        snapshots = writer.list_snapshots(target)
        return _json_dumps(
            {
                "file_path": str(target),
                "history_dir": str(writer.history_dir_for(target)),
                "count": len(snapshots),
                "snapshots": snapshots,
            }
        )

    @mcp.tool(
        description="hwp_history에서 확인한 snapshot_id 로 문서를 복원합니다. 복원 전 현재 파일도 스냅샷으로 백업한 뒤 원자 교체합니다."
    )
    async def hwp_restore(
        file_path: str,
        snapshot_id: str,
        expected_sha256: Optional[str] = None,
        force: bool = False,
    ) -> str:
        target = _resolve_existing_file(file_path)
        snap_path = writer.resolve_snapshot(target, snapshot_id)

        async def _do_restore(_src: Path, tmp_out: Path) -> Dict[str, Any]:
            shutil.copy2(snap_path, tmp_out)
            return {"restored_from": str(snap_path), "losses": []}

        result = await writer.execute_write(
            target,
            _do_restore,
            expected_sha256=expected_sha256,
            force=force,
            allow_loss=False,
        )
        return _json_dumps(result)

    @mcp.tool(
        description="두 문서 파일(file_path_a vs file_path_b) 또는 현재 문서와 스냅샷(snapshot_id) 간의 텍스트 unified diff 를 반환합니다."
    )
    async def hwp_diff(
        file_path_a: str,
        file_path_b: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        password: Optional[str] = None,
    ) -> str:
        path_a = _resolve_existing_file(file_path_a)
        if file_path_b:
            path_b = _resolve_existing_file(file_path_b)
            label_b = str(path_b)
        elif snapshot_id:
            path_b = writer.resolve_snapshot(path_a, snapshot_id)
            label_b = f"snapshot:{snapshot_id}"
        else:
            raise ValueError("file_path_b 또는 snapshot_id 중 하나를 지정해야 합니다.")

        async def _extract_lines(p: Path) -> List[str]:
            res = await rhwp.call_tool("hwp_export_text", {"path": str(p), "password": password})
            if isinstance(res, dict):
                if "pages" in res and isinstance(res["pages"], list):
                    txt = "\n".join(pg.get("text", "") for pg in res["pages"])
                else:
                    txt = res.get("text", "")
            else:
                txt = str(res)
            return txt.splitlines()

        lines_a = await _extract_lines(path_a)
        lines_b = await _extract_lines(path_b)
        diff_lines = list(
            difflib.unified_diff(
                lines_b,
                lines_a,
                fromfile=label_b,
                tofile=str(path_a),
                lineterm="",
            )
        )
        return _json_dumps(
            {
                "file_path_a": str(path_a),
                "compared_with": label_b,
                "identical": len(diff_lines) == 0,
                "diff": "\n".join(diff_lines),
            }
        )

    @mcp.tool(
        description=(
            "rhwp 가 제공하는 180여 개 전체 도구 중 하나를 직접 호출하는 탈출구입니다. "
            "쓰기 계열 도구(is_write=True 또는 hwp_set_*/hwp_insert_*/hwp_delete_*/hwp_replace_* 등)는 "
            "반드시 SafeWriter를 경유해 임시 파일 출력·검증·원자 교체를 수행합니다."
        )
    )
    async def hwp_rhwp_call(
        file_path: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        is_write: bool = False,
        expected_sha256: Optional[str] = None,
        force: bool = False,
        allow_loss: bool = False,
    ) -> str:
        target = _resolve_existing_file(file_path)
        args = dict(arguments or {})

        if _is_write_tool(tool_name, is_write):
            async def _do_write(src: Path, tmp_out: Path) -> Dict[str, Any]:
                call_args = dict(args)
                call_args["path"] = str(src)
                call_args["output"] = str(tmp_out)
                res = await rhwp.call_tool(tool_name, call_args)
                return res if isinstance(res, dict) else {"result": res}

            result = await writer.execute_write(
                target,
                _do_write,
                expected_sha256=expected_sha256,
                force=force,
                allow_loss=allow_loss,
            )
            return _json_dumps(result)

        writer.record_seen(target)
        args.setdefault("path", str(target))
        res = await rhwp.call_tool(tool_name, args)
        return _json_dumps(res)

    return mcp, rhwp, writer


async def run_stdio_server(mcp: FastMCP, rhwp: RhwpBackend) -> None:
    """Run FastMCP over stdio while cleanly handling `server/discover` requests."""
    try:
        async with stdio_server() as (raw_read, write_stream):
            filtered_send, filtered_recv = anyio.create_memory_object_stream(16)

            async def _filter_incoming() -> None:
                async with filtered_send:
                    async for msg in raw_read:
                        if (
                            isinstance(msg, SessionMessage)
                            and isinstance(msg.message.root, JSONRPCRequest)
                            and msg.message.root.method == "server/discover"
                        ):
                            err = JSONRPCError(
                                jsonrpc="2.0",
                                id=msg.message.root.id,
                                error=ErrorData(
                                    code=METHOD_NOT_FOUND,
                                    message="Method not found: server/discover",
                                ),
                            )
                            await write_stream.send(SessionMessage(message=JSONRPCMessage(err)))
                            continue
                        await filtered_send.send(msg)

            async with anyio.create_task_group() as tg:
                tg.start_soon(_filter_incoming)
                await mcp._mcp_server.run(
                    filtered_recv,
                    write_stream,
                    mcp._mcp_server.create_initialization_options(),
                )
    finally:
        await rhwp.close()


def main() -> None:
    mcp, rhwp, _ = create_server()
    asyncio.run(run_stdio_server(mcp, rhwp))
