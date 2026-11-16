"""Shared pytest fixtures for hwp-mcp tests."""

import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Real documents are personal, so they live in a git-ignored folder (or HWP_TEST_SAMPLES).
DEFAULT_SAMPLES_DIR = Path(__file__).resolve().parent / "local_samples"


def get_samples_dir() -> Path:
    return Path(os.environ.get("HWP_TEST_SAMPLES", str(DEFAULT_SAMPLES_DIR)))


@pytest.fixture
def sample_hwp_copy(tmp_path: Path) -> Path:
    samples_dir = get_samples_dir()
    if not samples_dir.is_dir():
        pytest.skip(f"HWP_TEST_SAMPLES directory not found: {samples_dir}")

    preferred = samples_dir / "포텐10팀_미션맵_주문신청서.hwp"
    if not preferred.is_file():
        hwp_files = sorted(samples_dir.glob("*.hwp"))
        if not hwp_files:
            pytest.skip(f"No .hwp sample files in: {samples_dir}")
        preferred = hwp_files[0]

    dst = tmp_path / preferred.name
    shutil.copy2(preferred, dst)
    return dst
