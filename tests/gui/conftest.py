"""GUI test fixtures (sub-package needs its own conftest for dvd_fixture)."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def dvd_fixture(tmp_path: Path):
    """A synthetic, spec-accurate, unencrypted DVD-Video tree."""
    from tests.synth import DvdFixtureSpec, write_dvd_fixture
    video_ts = write_dvd_fixture(str(tmp_path / "disc"),
                                 DvdFixtureSpec(cell_seconds=2.0,
                                                cell_count=3,
                                                audio_streams=1))
    return str(Path(video_ts).parent)
