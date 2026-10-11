"""Phase 8: Blu-ray MPLS/CLPI parser + analyzer + pipeline tests.

Fixtures are synthetic, spec-accurate, unencrypted BDMV trees built by
``tests.bdsynth`` and *cross-validated against ffmpeg's own libbluray*
(the demuxer must accept the tree and report the right playlist length).
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from opticrip.core import errors as E
from opticrip.core.models import (
    Drive, DriveKind, MediaType, StreamKind, TitleSelection,
)
from opticrip.disc.bd import (
    BluRayAnalyzer, parse_clpi, parse_mpls, ticks_to_seconds,
)


@pytest.fixture
def bdmv(tmp_path):
    """Synthetic unencrypted BDMV tree with a ~3s clip (light fixture).

    ffmpeg's bluray demuxer filters playlists under 180 s, so the libbluray
    conformance test below catches that deliberately and asserts on the
    *parse* errors instead of the title counter.
    """
    dest = tmp_path / "bd"
    from tests.bdsynth import write_bdmv_fixture
    write_bdmv_fixture(str(dest), seconds=3.0, chapters=2)
    return str(dest)


# NOTE: ffmpeg's bluray demuxer requires a *real* 180+ s playlist to count
# it.  For a cheap-but-real conformance probe we build ONE long fixture
# locally in the dedicated test (see the skip logic), not per test.
def _long_fixture(path: str) -> None:
    from tests.bdsynth import write_bdmv_fixture
    write_bdmv_fixture(path, seconds=181.0, chapters=3)


# --------------------------------------------------------------------------- #
# Parser units
# --------------------------------------------------------------------------- #
class TestParsers:
    def test_mpls_roundtrip_marks_and_playitem(self, bdmv):
        m = parse_mpls(open(os.path.join(bdmv, "BDMV/PLAYLIST/00001.mpls"),
                            "rb").read(), name="00001.mpls")
        assert len(m.play_items) == 1
        pi = m.play_items[0]
        assert pi.clip_id == "00001" and pi.clip_codec == "M2TS"
        assert not pi.is_multi_angle
        assert pi.in_time_ticks == 0
        assert pi.out_time_ticks == 135000
        assert pi.duration_seconds == 3.0
        kinds = {e.kind for e in pi.stn}
        assert "video" in kinds and "audio" in kinds
        audio = next(e for e in pi.stn if e.kind == "audio")
        assert audio.pid == 0x1100 and audio.language == "eng"
        assert video_pid(pi) == 0x1011
        assert [mk["time_ticks"] for mk in m.marks] == [0, 67500]

    def test_clpi_stcs(self, bdmv):
        c = parse_clpi(open(os.path.join(bdmv, "BDMV/CLIPINF/00001.clpi"),
                            "rb").read(), path="00001")
        assert c.stc_sequences, "need at least one STC sequence"
        stc = c.stc_sequences[0]
        assert stc.pcr_pid == 0x1001
        assert stc.start_ticks == 0 and stc.end_ticks == 135000

    def test_bad_type_indicator_raises(self, tmp_path):
        bad = tmp_path / "bad.mpls"
        bad.write_bytes(b"JUNK" + bytes(60))
        with pytest.raises(E.DiscStructureError):
            parse_mpls(bad.read_bytes())

    def test_ticks_to_seconds(self):
        assert ticks_to_seconds(45000) == 1.0
        assert ticks_to_seconds(90) == pytest.approx(0.002)


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #
class TestAnalyzer:
    def _analyze(self, root: str):
        from opticrip.disc.fs import open_disc_filesystem
        return BluRayAnalyzer().analyze(
            None, open_disc_filesystem(root))

    def test_analyzer_titles_streams_chapters(self, bdmv):
        a = self._analyze(bdmv)
        assert a.analyzer == "bd-mpls"
        assert a.disc.media_type is MediaType.BLURAY
        t = a.titles[0]
        assert t.duration_seconds == 3.0
        assert [(c.index, c.start_seconds) for c in t.chapters] == \
            [(1, 0.0), (2, 1.5)]
        video = next(s for s in t.streams
                     if s.kind is StreamKind.VIDEO)
        audio = next(s for s in t.streams if s.kind is StreamKind.AUDIO)
        assert video.codec == "h264"
        assert audio.codec == "ac3" and audio.language == "eng"

    def test_can_handle_bdmv_root(self, bdmv):
        from opticrip.disc.fs import open_disc_filesystem
        fs = open_disc_filesystem(bdmv)
        assert BluRayAnalyzer().can_handle(None, fs)

    def test_playlist_reference_in_source_ref(self, bdmv):
        a = self._analyze(bdmv)
        assert a.titles[0].source_ref == {
            "kind": "bd-playlist", "playlist": "00001.mpls"}


# --------------------------------------------------------------------------- #
# libbluray conformance: ffmpeg must read our fixture structurally
# --------------------------------------------------------------------------- #
@pytest.mark.requires_ffmpeg
class TestLibblurayConformance:
    def test_ffmpeg_accepts_tree(self, tmp_path, have_ffmpeg):
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        dest = tmp_path / "long"
        _long_fixture(str(dest))
        proc = subprocess.run(
            ["ffprobe", "-hide_banner", "bluray:" + str(dest)],
            capture_output=True, text=True, timeout=90)
        out = proc.stderr
        assert "1 usable playlists" in out, out[-800:]
        assert "playlist 00001.mpls (0:03:01)" in out, out[-800:]


def video_pid(pi):
    return pi.stn[0].pid


@pytest.mark.requires_ffmpeg
class TestPipeline:
    def test_blu_ray_rip_produces_valid_mkv(self, tmp_path, have_ffmpeg):
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        from opticrip.disc.fs import open_disc_filesystem
        from opticrip.rip.engine import RipEngine
        from opticrip.core.config import Settings

        dest = tmp_path / "bd2"
        _long_fixture(str(dest))
        analysis = BluRayAnalyzer().analyze(
            None, open_disc_filesystem(str(dest)))
        title = analysis.titles[0]
        out = tmp_path / "res" / f"t{title.index}.mkv"
        out.parent.mkdir(exist_ok=True)
        job = RipJobSpec(title, disc_root=dest, out_dir=out.parent)
        engine = RipEngine(Settings().normalise())
        result = engine.run(job)
        assert result.ok, (result.warnings, job.error)

        # independent structural validation of the deliverable
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format",
             str(result.output_path if hasattr(result, "output_path")
                 else out)],
            capture_output=True, text=True, timeout=60)
        assert "h264" in probe.stdout, probe.stdout
        assert "ac3" in probe.stdout

    def test_encrypted_marker_classifies_honestly(self):
        from opticrip.rip.extract import classify_ffmpeg_failure
        with pytest.raises(E.EncryptedDiscError):
            raise_it = classify_ffmpeg_failure(
                "aacs: cannot decrypt protected content")
            raise raise_it


def RipJobSpec(title, disc_root, out_dir):
    """A full-stream selection job onto a folder source.

    ``disc_root`` drives extraction (read from that tree's BDMV); the file
    lands in ``out_dir``.
    """
    from opticrip.core.models import RipJob, new_id
    return RipJob(
        id=f"bd-{new_id()}", drive_device=str(disc_root),
        title=title, selection=TitleSelection.all_streams(title),
        output_path=str(out_dir / f"t{title.index}.mkv"),
        media_type=MediaType.BLURAY,
        drive_kind=DriveKind.FOLDER, disc_label="bd")
