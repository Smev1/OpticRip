"""End-to-end rip-pipeline tests against real ffmpeg.

These cover Phase 4's actual deliverable: a fixture goes in, a valid MKV
comes out, without re-encoding, with chapters and languages preserved.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from opticrip.core.config import Settings
from opticrip.core.errors import OutputExistsError, OutputExistsError as OEE
from opticrip.core.interfaces import ExtractionResult
from opticrip.core.models import (
    Drive,
    DriveKind,
    MediaType,
    RipJob,
    TitleSelection,
    new_id,
)
from opticrip.disc.dvd import DvdIfoAnalyzer
from opticrip.disc.fs import open_disc_filesystem
from opticrip.rip.engine import RipEngine
from opticrip.rip.validate import FfprobeValidator

pytest_plugins = []


@pytest.fixture
def analysed(dvd_fixture: str):
    fs = open_disc_filesystem(dvd_fixture)
    drive = Drive(path=dvd_fixture, kind=DriveKind.FOLDER,
                  media_type=MediaType.DVD_VIDEO)
    return drive, DvdIfoAnalyzer().analyze(drive, fs)


def make_job(drive: Drive, analysis, output_path: str, *,
             drive_kind: DriveKind = DriveKind.FOLDER) -> RipJob:
    title = analysis.titles[0]
    return RipJob(
        id=new_id(), drive_device=drive.path, title=title,
        selection=TitleSelection.all_streams(title),
        output_path=output_path,
        disc_label=analysis.disc.label, media_type=MediaType.DVD_VIDEO,
        drive_kind=drive_kind)


@pytest.mark.requires_ffmpeg
class TestFullPipeline:
    def test_rip_produces_valid_mkv(self, analysed, tmp_path, have_ffmpeg):
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        settings = Settings().normalise()
        settings.output_dir = str(tmp_path)
        settings.overwrite_policy = "overwrite"
        engine = RipEngine(settings)
        job = make_job(drive, analysis, str(tmp_path / "out.mkv"))
        result = engine.run(job)
        assert result.ok, f"job failed: {job.error}"
        assert os.path.exists(result.output_path)
        assert result.validation is not None and result.validation.ok

        # independent content check with ffprobe
        probe = subprocess.run(
            ["ffprobe", "-hide_banner", "-show_format", "-show_streams",
             "-show_chapters", "-print_format", "json", result.output_path],
            capture_output=True, text=True, timeout=60)
        data = json.loads(probe.stdout)
        assert data["format"]["format_name"].startswith("matroska")
        kinds = [s["codec_type"] for s in data["streams"]]
        assert "video" in kinds and "audio" in kinds
        audio = next(s for s in data["streams"]
                     if s["codec_type"] == "audio")
        assert (audio.get("tags") or {}).get("language") == "eng"
        # chapters from the IFO must survive into the MKV
        assert len(data["chapters"]) == 3
        assert float(data["format"]["duration"]) == pytest.approx(6.0, abs=0.6)

    def test_chapters_zero_based_in_output(self, analysed, tmp_path,
                                           have_ffmpeg):
        """Verify phase-5 skew behaviour: when the PGC's first chapter
        starts above 0, the MKV's chapter 1 must still report 0."""
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        settings = Settings().normalise()
        settings.output_dir = str(tmp_path)
        settings.overwrite_policy = "overwrite"
        engine = RipEngine(settings)
        # rewrite chapters to be intentionally skewed; the engine must shift
        title = analysis.titles[0]
        title.chapters[0].start_seconds = 1.5
        title.notes.append("test skew")
        job = RipJob(id=new_id(), drive_device=drive.path, title=title,
                     selection=TitleSelection.all_streams(title),
                     output_path=str(tmp_path / "skew.mkv"),
                     drive_kind=DriveKind.FOLDER,
                     media_type=MediaType.DVD_VIDEO)
        result = engine.run(job)
        assert result.ok, job.error
        import json, subprocess
        probe = subprocess.run(["ffprobe", "-show_chapters",
                                "-print_format", "json", result.output_path],
                               capture_output=True, text=True, timeout=60)
        chapters = json.loads(probe.stdout)["chapters"]
        assert chapters, "MKV must carry chapters"
        assert float(chapters[0]["start_time"].replace(",", "")) == 0.0

    def test_no_reencoding(self, analysed, tmp_path, have_ffmpeg):
        """Stream copy means: same codecs/parameters in the MKV as on disc."""
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        settings = Settings().normalise()
        settings.output_dir = str(tmp_path)
        settings.overwrite_policy = "overwrite"
        engine = RipEngine(settings)
        job = make_job(drive, analysis, str(tmp_path / "copy.mkv"))
        result = engine.run(job)
        assert result.ok
        from tests.synth import DvdFixtureSpec
        # fixture was encoded 25fps MPEG-2 at 352x288 with an AC-3 stream;
        # the MKV must carry exactly that, unchanged.
        probe = subprocess.run(
            ["ffprobe", "-print_format", "json", "-show_streams",
             str(tmp_path / "copy.mkv")],
            capture_output=True, text=True, timeout=60)
        assert probe.returncode == 0, probe.stderr[-300:]
        streams = json.loads(probe.stdout)["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        assert video["codec_name"] == "mpeg2video"
        assert (video["width"], video["height"]) == (352, 288)
        audio = next(s for s in streams if s["codec_type"] == "audio")
        assert audio["codec_name"] == "ac3"
        assert int(audio["sample_rate"]) == 48000

    def test_overwrite_ask_raises(self, analysed, tmp_path, have_ffmpeg):
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        settings = Settings().normalise()
        settings.output_dir = str(tmp_path)
        settings.overwrite_policy = "ask"
        engine = RipEngine(settings)
        Path(tmp_path / "exists.mkv").write_text("existing")
        job = make_job(drive, analysis, str(tmp_path / "exists.mkv"))
        with pytest.raises(OEE):
            engine.run(job)

    def test_overwrite_skip_marks_skipped(self, analysed, tmp_path,
                                          have_ffmpeg):
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        settings = Settings().normalise()
        settings.output_dir = str(tmp_path)
        settings.overwrite_policy = "skip"
        engine = RipEngine(settings)
        sentinel = tmp_path / "exists.mkv"
        sentinel.write_text("existing")
        job = make_job(drive, analysis, str(sentinel))
        result = engine.run(job)
        assert not result.ok
        assert job.status.label in ("Skipped", "Failed")
        assert sentinel.read_text() == "existing", "must not be touched"

    def test_cancel_produces_no_output(self, analysed, tmp_path, have_ffmpeg):
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        settings = Settings().normalise()
        settings.output_dir = str(tmp_path)
        engine = RipEngine(settings)
        engine.request_cancel  # noqa - we cancel BEFORE any work happens
        job = make_job(drive, analysis, str(tmp_path / "cancelled.mkv"))
        engine.request_cancel(job.id)
        result = engine.run(job)
        assert not result.ok
        assert job.status.label == "Cancelled"
        assert not (tmp_path / "cancelled.mkv").exists()

    def test_job_failure_is_reported_not_raised(self, analysed, tmp_path,
                                                have_ffmpeg):
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        settings = Settings().normalise()
        settings.output_dir = str(tmp_path)
        # point the job at a nonexistent output dir (mkvmerge/ffmpeg fail)
        engine = RipEngine(settings)
        bad_target = str(tmp_path / "no-such-dir" / "x.mkv")
        job = make_job(drive, analysis, bad_target,
                       drive_kind=DriveKind.IMAGE)  # unsupported route
        result = engine.run(job)
        assert not result.ok
        assert job.error, "an error message must be surfaced"
        assert job.error_title


# --------------------------------------------------------------------------- #
# Phase 10c: ISO images rip through our own extractor (no libdvdread needed)
# --------------------------------------------------------------------------- #
class TestIsoRipPipeline:
    def test_iso_rips_to_valid_mkv(self, tmp_path, dvd_fixture):
        """An .iso built from the synthetic DVD tree must rip end-to-end.

        The dvd-cell extractor addresses VOB extents through our ISO9660
        reader, so this works even where ffmpeg lacks the dvdvideo demuxer.
        """
        pytest.importorskip("pycdlib")
        import pycdlib
        from opticrip.core.config import Settings
        from opticrip.core.models import RipJob, TitleSelection, new_id
        from opticrip.disc.fs import open_disc_filesystem
        from opticrip.disc.dvd import DvdIfoAnalyzer
        from opticrip.drives.virtual import AutoPathProvider
        from opticrip.rip.engine import RipEngine

        # pack the existing synthetic tree into an ISO
        iso_path = str(tmp_path / "disc.iso")
        pkg = pycdlib.PyCdlib()
        pkg.new(interchange_level=3)
        pkg.add_directory("/VIDEO_TS")
        vts_dir = os.path.join(dvd_fixture, "VIDEO_TS")
        for name in sorted(os.listdir(vts_dir)):
            pkg.add_file(os.path.join(vts_dir, name),
                         "/VIDEO_TS/" + name.upper())
        pkg.write(iso_path)
        pkg.close()

        drive = AutoPathProvider([iso_path]).list_drives()[0]
        assert drive.kind.label == "Disc image"
        fs = open_disc_filesystem(drive.path)
        analysis = DvdIfoAnalyzer().analyze(drive, fs)
        assert analysis.titles, "fixture must expose at least one title"

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        title = analysis.titles[0]
        settings = Settings()
        settings.output_dir = str(out_dir)
        settings.overwrite_policy = "overwrite"
        job = RipJob(
            id=new_id(), drive_device=drive.path, title=title,
            selection=TitleSelection.all_streams(title),
            output_path=str(out_dir / "disc.mkv"),
            disc_label=analysis.disc.label,
            media_type=analysis.disc.media_type,
            drive_kind=drive.kind,
        )
        result = RipEngine(settings).run(job)
        assert result.ok, (job.error_title, job.error, job.error_hint)
        assert os.path.getsize(result.output_path) > 0


# --------------------------------------------------------------------------- #
# regression: real discs put a navigation pack at the start of every VOBU.
# The whole rip must survive that (it used to fail in the remux step with
# "Only audio, video, and subtitles are supported for Matroska").
# --------------------------------------------------------------------------- #
class TestFolderRipWithNavigationPacks:
    def test_folder_rip_with_nav_packs_produces_valid_mkv(self, tmp_path,
                                                          have_ffmpeg):
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        from tests.synth import DvdFixtureSpec, write_dvd_fixture
        write_dvd_fixture(str(tmp_path / "disc"), DvdFixtureSpec(
            cell_seconds=2.0, cell_count=2, audio_streams=1,
            subtitle_streams=1, nav_packs=True))
        root = str(tmp_path / "disc")
        fs = open_disc_filesystem(root)
        drive = Drive(path=root, kind=DriveKind.FOLDER,
                      media_type=MediaType.DVD_VIDEO)
        analysis = DvdIfoAnalyzer().analyze(drive, fs)
        settings = Settings().normalise()
        settings.output_dir = str(tmp_path)
        settings.overwrite_policy = "overwrite"
        job = make_job(drive, analysis, str(tmp_path / "nav.mkv"))
        result = RipEngine(settings).run(job)
        assert result.ok, (job.error_title, job.error, job.error_hint)

        probe = subprocess.run(
            ["ffprobe", "-hide_banner", "-loglevel", "error", "-show_streams",
             "-print_format", "json", result.output_path],
            capture_output=True, text=True, timeout=60)
        kinds = [s["codec_type"] for s in json.loads(probe.stdout)["streams"]]
        assert "video" in kinds and "audio" in kinds, kinds
        assert "data" not in kinds, kinds
        assert result.validation is not None and result.validation.ok
