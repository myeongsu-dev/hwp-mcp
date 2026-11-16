"""SafeWriter: hash-checked, snapshot-backed, verified atomic writer for HWP/HWPX."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional


class SafeWriteError(RuntimeError):
    """Base error for SafeWriter failures."""


class HashConflictError(SafeWriteError):
    """Raised when file hash does not match expected_sha256 or last seen hash."""


class VerificationError(SafeWriteError):
    """Raised when post-edit verification or lossless check fails."""


class FileLockedError(SafeWriteError):
    """Raised when os.replace fails with PermissionError after retries."""


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_loss_count(result: Any) -> int:
    if not isinstance(result, dict):
        return 0
    if isinstance(result.get("lossCount"), int):
        return result["lossCount"]
    losses = result.get("losses")
    if isinstance(losses, list):
        return len(losses)
    content_loss = result.get("contentLoss")
    if isinstance(content_loss, dict):
        if isinstance(content_loss.get("count"), int):
            return content_loss["count"]
        if isinstance(content_loss.get("losses"), list):
            return len(content_loss["losses"])
    elif isinstance(content_loss, list):
        return len(content_loss)
    return 0


class SafeWriter:
    """Coordinates safe atomic writes with snapshots, verification, and lock retries."""

    MAX_SNAPSHOTS = 50

    def __init__(self, backend: Any, max_snapshots: int = MAX_SNAPSHOTS):
        self.backend = backend
        self.max_snapshots = max_snapshots
        self._last_seen_hashes: Dict[str, str] = {}

    def record_seen(self, file_path: str | Path) -> str:
        """Compute and record the current SHA-256 of a file."""
        p = Path(file_path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {p}")
        sha = compute_sha256(p)
        self._last_seen_hashes[str(p)] = sha
        return sha

    def get_last_seen(self, file_path: str | Path) -> Optional[str]:
        return self._last_seen_hashes.get(str(Path(file_path).resolve()))

    def history_dir_for(self, file_path: str | Path) -> Path:
        p = Path(file_path).resolve()
        return p.parent / ".hwp-mcp-history" / p.name

    def create_snapshot(self, file_path: str | Path, current_sha: Optional[str] = None) -> Path:
        """Copy original file into <doc_dir>/.hwp-mcp-history/<filename>/<YYYYmmdd-HHMMSS>_<sha8>.<ext>."""
        p = Path(file_path).resolve()
        sha = current_sha or compute_sha256(p)
        hist_dir = self.history_dir_for(p)
        hist_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        ext = p.suffix
        base_name = f"{ts}_{sha[:8]}"
        snap_path = hist_dir / f"{base_name}{ext}"
        idx = 1
        while snap_path.exists():
            snap_path = hist_dir / f"{base_name}_{idx}{ext}"
            idx += 1

        shutil.copy2(p, snap_path)
        self._prune_snapshots(hist_dir)
        return snap_path

    def _prune_snapshots(self, hist_dir: Path) -> None:
        files = [f for f in hist_dir.iterdir() if f.is_file()]
        if len(files) <= self.max_snapshots:
            return
        files.sort(key=lambda f: (f.stat().st_mtime_ns, f.name), reverse=True)
        for old in files[self.max_snapshots :]:
            try:
                old.unlink()
            except OSError:
                pass

    def list_snapshots(self, file_path: str | Path) -> List[Dict[str, Any]]:
        hist_dir = self.history_dir_for(file_path)
        if not hist_dir.is_dir():
            return []
        files = [f for f in hist_dir.iterdir() if f.is_file()]
        files.sort(key=lambda f: (f.stat().st_mtime_ns, f.name), reverse=True)
        items: List[Dict[str, Any]] = []
        for f in files:
            st = f.stat()
            items.append(
                {
                    "snapshot_id": f.name,
                    "path": str(f),
                    "size_bytes": st.st_size,
                    "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                }
            )
        return items

    def resolve_snapshot(self, file_path: str | Path, snapshot_id: str) -> Path:
        hist_dir = self.history_dir_for(file_path)
        cand = (hist_dir / snapshot_id).resolve()
        if cand.parent != hist_dir.resolve() or not cand.is_file():
            raise FileNotFoundError(f"스냅샷을 찾을 수 없습니다: {snapshot_id}")
        return cand

    async def execute_write(
        self,
        file_path: str | Path,
        write_fn: Callable[[Path, Path], Awaitable[Dict[str, Any]]],
        *,
        expected_sha256: Optional[str] = None,
        force: bool = False,
        allow_loss: bool = False,
        verify_with_rhwp: bool = True,
        retry_delay: float = 0.2,
    ) -> Dict[str, Any]:
        """Execute a full SafeWriter transaction on `file_path`.

        `write_fn(source_path, tmp_output_path)` must write the edited document
        to `tmp_output_path` (preserving extension) and return the rhwp result dict.
        """
        target = Path(file_path).resolve()
        if not target.is_file():
            raise FileNotFoundError(f"대상 파일을 찾을 수 없습니다: {target}")

        # 1. Compute current SHA-256
        current_sha = compute_sha256(target)

        # 2. Check expected_sha256 and last_seen_hashes
        last_seen = self._last_seen_hashes.get(str(target))
        if not force:
            if expected_sha256 and expected_sha256.lower() != current_sha.lower():
                raise HashConflictError(
                    f"파일 해시가 expected_sha256 과 일치하지 않습니다 "
                    f"(expected={expected_sha256[:12]}, actual={current_sha[:12]}). "
                    f"최신 내용을 다시 읽거나 force=True 를 지정하세요."
                )
            if last_seen and last_seen.lower() != current_sha.lower():
                raise HashConflictError(
                    f"서버가 마지막으로 확인한 이후 파일이 외부(편집기 등)에서 변경되었습니다 "
                    f"(last_seen={last_seen[:12]}, actual={current_sha[:12]}). "
                    f"최신 내용을 다시 읽거나 force=True 를 지정하세요."
                )

        # Capture baseline info if verifying
        orig_info: Optional[Dict[str, Any]] = None
        if verify_with_rhwp:
            try:
                orig_info = await self.backend.call_tool("hwp_info", {"path": str(target)})
            except Exception:
                orig_info = None

        # 3. Create snapshot
        snap_path = self.create_snapshot(target, current_sha=current_sha)

        # 4. Run rhwp write into a temp file in the SAME directory
        tmp_path = target.parent / f".hwp-mcp-tmp-{uuid.uuid4().hex[:12]}{target.suffix}"
        try:
            rhwp_result = await write_fn(target, tmp_path)

            # Check for explicit no-op (e.g. replace-text with replacedCount == 0)
            if not tmp_path.exists():
                if isinstance(rhwp_result, dict) and rhwp_result.get("replacedCount") == 0:
                    self._last_seen_hashes[str(target)] = current_sha
                    return {
                        "changed": False,
                        "file_path": str(target),
                        "previous_sha256": current_sha,
                        "new_sha256": current_sha,
                        "snapshot_path": str(snap_path),
                        "rhwp_result": rhwp_result,
                        "message": "일치하는 대상이 없어 파일이 변경되지 않았습니다.",
                    }
                raise VerificationError("rhwp 가 출력 임시 파일을 생성하지 못했습니다.")

            if tmp_path.stat().st_size == 0:
                raise VerificationError("생성된 임시 파일이 0바이트입니다.")

            # 5. Check loss report and reopen temp file with rhwp
            loss_count = _extract_loss_count(rhwp_result)
            if loss_count > 0 and not allow_loss:
                raise VerificationError(
                    f"rhwp 저장 중 데이터 손실이 {loss_count}건 보고되어 저장을 거부했습니다 "
                    f"(allow_loss=True 로 강제 가능): {rhwp_result}"
                )

            reopen_info: Optional[Dict[str, Any]] = None
            if verify_with_rhwp:
                try:
                    reopen_info = await self.backend.call_tool("hwp_info", {"path": str(tmp_path)})
                except Exception as exc:
                    raise VerificationError(f"임시 파일 재열기 검증 실패: {exc}") from exc

                if isinstance(reopen_info, dict) and isinstance(orig_info, dict):
                    orig_pages = orig_info.get("pageCount")
                    new_pages = reopen_info.get("pageCount")
                    if isinstance(orig_pages, int) and orig_pages > 0 and new_pages == 0:
                        raise VerificationError(
                            f"검증 실패: 페이지 수가 {orig_pages}에서 0으로 감소했습니다."
                        )

            # 6. Atomic replace with PermissionError retry (3 retries)
            last_perm_err: Optional[PermissionError] = None
            for attempt in range(4):
                try:
                    os.replace(tmp_path, target)
                    last_perm_err = None
                    break
                except PermissionError as exc:
                    last_perm_err = exc
                    if attempt < 3:
                        await asyncio.sleep(retry_delay)

            if last_perm_err is not None:
                raise FileLockedError(
                    f"편집기가 파일을 잠그고 있어 교체할 수 없습니다: {target} "
                    f"(원본 스냅샷 보존됨: {snap_path})"
                ) from last_perm_err

            # 7. Update last_seen_hashes and return summary
            new_sha = compute_sha256(target)
            self._last_seen_hashes[str(target)] = new_sha
            return {
                "changed": True,
                "file_path": str(target),
                "previous_sha256": current_sha,
                "new_sha256": new_sha,
                "snapshot_path": str(snap_path),
                "loss_count": loss_count,
                "reopen_info": reopen_info,
                "rhwp_result": rhwp_result,
            }
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
