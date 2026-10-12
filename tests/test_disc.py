"""DVD IFO parser + fixture conformance tests.

Fixtures are built by tests/synth.py and (when ffmpeg is present) also
validated through ffmpeg's libdvdread-based `dvdvideo` demuxer, so our
parser and the reference implementation must agree.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from opticrip.disc.dvd import DvdIfoAnalyzer
from opticrip.disc.fs import open_disc_filesystem
from opticrip.disc.identify import (
    LinuxMediaProbe,
    classify_root_names,
    is_iso9660_pvd,
    iso9660_volume_label,
    looks_encrypted_dvd_pack,
)
from opticrip.core.models import Drive, DriveKind, MediaType, StreamKind


@pytest.fixture
def analysis(dvd_fixture: str):
    fs = open_disc_filesystem(dvd_fixture)
    drive = Drive(path=dvd_fixture, kind=DriveKind.FOLDER)
    return DvdIfoAnalyzer().analyze(drive, fs), dvd_fixture


class TestMediaIdentify:
    def test_classify_root(self):
        assert classify_root_names(["VIDEO_TS", "AUDIO_TS"]) is MediaType.DVD_VIDEO
        assert classify_root_names(["BDMV"]) is MediaType.BLURAY
        assert classify_root_names(["menu.gif"]) is MediaType.DATA

    def test_pack_encryption_heuristic(self):
        clean = b"\x00\x00\x01\xba" + bytes(60)
        assert looks_encrypted_dvd_pack(clean) is False
        assert looks_encrypted_dvd_pack(b"\x99\x88" + bytes(60)) is True


class TestDvdAnalysis:
    def test_finds_one_title(self, analysis):
        result, _ = analysis
        assert len(result.titles) == 1
        assert result.analyzer == "dvd-ifo"

    def test_streams_ordered(self, analysis):
        result, _ = analysis
        title = result.titles[0]
        kinds = [s.kind for s in title.streams]
        assert kinds == [StreamKind.VIDEO, StreamKind.AUDIO,
                         StreamKind.SUBTITLE]

    def test_audio_language_and_channels(self, analysis):
        result, _ = analysis
        audio = [s for s in result.titles[0].streams
                 if s.kind is StreamKind.AUDIO][0]
        assert audio.language in ("eng", "en")
        assert audio.channels == 2
        assert audio.sample_rate == 48000

    def test_duration_matches_cells(self, analysis):
        result, _ = analysis
        title = result.titles[0]
        assert title.duration_seconds == pytest.approx(6.0, abs=0.5)
        assert title.chapter_count == 3

    def test_source_ref_carries_cells(self, analysis):
        result, _ = analysis
        cells = result.titles[0].source_ref["cells"]
        assert len(cells) == 3
        # contiguous cell sector ranges
        assert cells[1]["first_sector"] == cells[0]["last_sector"] + 1
        assert cells[2]["first_sector"] == cells[1]["last_sector"] + 1
        # positive sizes
        for cell in cells:
            assert cell["last_sector"] >= cell["first_sector"]


@pytest.mark.requires_ffmpeg
class TestFfprobeConformance:
    """Our fixture must be readable by ffmpeg's libdvdread as well
    (independent reference) and agree on structure."""

    def test_ffprobe_sees_same_structure(self, dvd_fixture: str, have_ffmpeg):
        """Cross-engine conformance: libdvdread must parse the fixture and
        agree with our parser on stream kinds and the audio language."""
        if not have_ffmpeg:
            pytest.skip("no ffmpeg available")
        from conftest import ffmpeg_supports_demuxer
        if not ffmpeg_supports_demuxer("dvdvideo"):
            pytest.skip("ffmpeg build lacks libdvdread dvdvideo demuxer")
        proc = subprocess.run(
            ["ffprobe", "-hide_banner", "-f", "dvdvideo",
             "-show_streams", "-show_format", "-print_format", "json",
             dvd_fixture],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-500:]
        data = json.loads(proc.stdout)
        streams = data.get("streams", [])
        types = {s.get("codec_type") for s in streams}
        assert types == {"video", "audio", "subtitle"}, types
        audio = next(s for s in streams if s.get("codec_type") == "audio")
        assert (audio.get("tags") or {}).get("language", "") in ("eng", "en")
        video = next(s for s in streams if s.get("codec_type") == "video")
        assert video.get("codec_name") == "mpeg2video"
