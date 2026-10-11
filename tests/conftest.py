"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# make `tests.synth` and the project importable when running from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="session")
def have_ffmpeg() -> bool:
    """True when a real ffmpeg/ffprobe is importable/available."""
    try:
        import static_ffmpeg  # type: ignore
        static_ffmpeg.add_paths()
        import shutil
        return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    except ImportError:
        import shutil
        return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture
def dvd_fixture(tmp_path: Path):
    """A synthetic, spec-accurate, unencrypted DVD-Video tree."""
    from tests.synth import DvdFixtureSpec, write_dvd_fixture
    video_ts = write_dvd_fixture(str(tmp_path / "disc"),
                                 DvdFixtureSpec(cell_seconds=2.0, cell_count=3,
                                                audio_streams=1))
    # return the DISC ROOT (parent of VIDEO_TS), matching the documented
    # OpticRip source convention (directory containing VIDEO_TS/BDMV)
    return str(Path(video_ts).parent)
