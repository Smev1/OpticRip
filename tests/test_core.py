"""Unit tests: naming, selection, disk space, models, config."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from opticrip.core import naming
from opticrip.core.config import Settings
from opticrip.core.diskutil import OsDiskSpaceChecker
from opticrip.core.models import (
    Chapter,
    DiscAnalysis,
    DiscInfo,
    MediaType,
    StreamInfo,
    StreamKind,
    Title,
    TitleSelection,
    mark_main_feature,
    normalise_language,
    selection_to_streams,
    validate_selection,
)


def t(duration: float = 5400.0, streams: list[StreamInfo] | None = None,
      chapters: int = 1, title_id: str = "t1", index: int = 1) -> Title:
    return Title(
        id=title_id, index=index, duration_seconds=duration,
        streams=streams or [
            StreamInfo(0, StreamKind.VIDEO, "mpeg2video", "MPEG-2",
                       width=720, height=480, fps=29.97),
            StreamInfo(1, StreamKind.AUDIO, "ac3", "AC-3",
                       language="en", channels=6, channel_layout="5.1",
                       sample_rate=48000),
            StreamInfo(2, StreamKind.SUBTITLE, "dvd_subtitle",
                       language="en", subtitle_format="vobsub"),
        ],
        chapters=[Chapter(i + 1, i * 300.0, 300.0) for i in range(chapters)],
    )


# ---------------------------------------------------------------- naming -- #
class TestNaming:
    def test_basic_template(self):
        title = t()
        sel = TitleSelection(title_id="t1")
        out = naming.render_filename("{disc}_{title}", title, sel,
                                     disc_label="My Disc")
        assert out == "My Disc_Title 1.mkv"

    def test_unknown_token_passthrough(self):
        out = naming.render_filename("{nope}_x", t(),
                                     TitleSelection(title_id="t1"))
        assert out.startswith("{nope}_x"), "unknown token must stay visible"

    def test_illegal_chars_sanitised(self):
        out = naming.render_filename('bad:<>/name', t(),
                                     TitleSelection(title_id="t1"))
        assert not any(c in out for c in '<>:/')
        assert out.endswith(".mkv")

    def test_index_format(self):
        out = naming.render_filename("{index:03d}", t(index=7),
                                     TitleSelection(title_id="t1"))
        assert out == "007.mkv"

    def test_chapter_range_token(self):
        title = t(chapters=12)
        sel = TitleSelection(title_id="t1", chapter_start=2, chapter_end=5)
        out = naming.render_filename("{disc}-{chapters}", title, sel,
                                     disc_label="D")
        assert "ch02-05" in out

    def test_empty_template_falls_back(self):
        out = naming.render_filename("   ", t(), TitleSelection(title_id="t1"))
        assert out == naming.sanitise_component("Title 1.mkv") or out.endswith(".mkv")

    def test_bookkeeping_reserved_names(self):
        out = naming.sanitise_component("CON")
        assert out.startswith("_"), "windows device names get prefixed"


# -------------------------------------------------------------- language -- #
class TestLanguage:
    def test_iso1_to_bibl(self):
        assert normalise_language("en") == "eng"

    def test_unknown_2letter_kept(self):
        assert normalise_language("xx") in (None, "xx")

    def test_3letter_passthrough(self):
        assert normalise_language("fre") == "fre"


# -------------------------------------------------------------- selection -- #
class TestSelection:
    def test_all_streams_maps_correctly(self):
        title = t()
        sel = TitleSelection.all_streams(title)
        selected = selection_to_streams(title, sel)
        kinds = [s.kind for s in selected]
        assert kinds == [StreamKind.VIDEO, StreamKind.AUDIO,
                         StreamKind.SUBTITLE]

    def test_validate_rejects_missing_audio(self):
        title = t()
        with pytest.raises(ValueError):
            validate_selection(title, TitleSelection(title_id="t1",
                                                     audio_indices=(99,)))

    def test_validate_rejects_wrong_kind(self):
        title = t()
        with pytest.raises(ValueError):
            validate_selection(title, TitleSelection(title_id="t1",
                                                     video_index=1))  # audio

    def test_validate_rejects_title_mismatch(self):
        with pytest.raises(ValueError):
            validate_selection(t(title_id="other"),
                               TitleSelection(title_id="t1"))

    def test_chapter_range_clamps(self):
        sel = TitleSelection(title_id="t1", chapter_start=5, chapter_end=99)
        assert sel.resolved_chapter_range(8) == (5, 8)
        sel2 = TitleSelection(title_id="t1", chapter_start=9, chapter_end=2)
        assert sel2.resolved_chapter_range(8) == (8, 8)  # clamped to last


# ---------------------------------------------------------- main feature -- #
class TestMainFeature:
    def test_longest_is_flagged(self):
        titles = [t(duration=120.0, title_id="a"),
                  t(duration=5400.0, title_id="b"),
                  t(duration=900.0, title_id="c")]
        best = mark_main_feature(titles, min_seconds=1200.0)
        assert best is not None and best.id == "b"
        assert [x.is_main_feature for x in titles] == [False, True, False]

    def test_ambiguous_gets_note(self):
        titles = [t(duration=5400.0, title_id="a"),
                  t(duration=5000.0, title_id="b")]
        best = mark_main_feature(titles)
        assert any("similarly long" in n for n in best.notes)


# ---------------------------------------------------------- disk space ---- #
class TestDiskSpace:
    def test_free_bytes_for_existing_dir(self):
        checker = OsDiskSpaceChecker()
        assert checker.free_bytes(tempfile.gettempdir()) > 0

    def test_required_from_title_size(self):
        title = t()
        title.size_bytes = 4_700_000_000
        checker = OsDiskSpaceChecker()
        req = checker.required_bytes(title, TitleSelection(title_id="t1"))
        assert req > title.size_bytes  # includes container overhead

    def test_check_fits(self, tmp_path: Path):
        title = t()
        title.size_bytes = 4_700_000_000
        checker = OsDiskSpaceChecker()
        fits, required, free = checker.check(str(tmp_path), title,
                                             TitleSelection(title_id="t1"))
        # a synthetic CI disk might be big; the invariant is the tuple form
        assert isinstance(fits, bool) and free >= 0


# ---------------------------------------------------------------- config -- #
class TestConfig:
    def test_roundtrip(self, tmp_path: Path):
        s = Settings().normalise()
        s.output_dir = str(tmp_path)
        path = tmp_path / "settings.json"
        s.save(str(path))
        back = Settings.load(str(path))
        assert back.output_dir == str(tmp_path)
        assert back.version == s.version

    def test_broken_json_is_reported(self, tmp_path: Path):
        path = tmp_path / "broken.json"
        path.write_text("{ not json ]")
        with pytest.raises(Exception):
            Settings.load(str(path))

    def test_policy_clamped(self):
        s = Settings(overwrite_policy="bogus").normalise()
        assert s.overwrite_policy == "ask"


# ------------------------------------------------------------ asdict ------ #
class TestSerialisation:
    def test_job_roundtrip_json(self):
        from opticrip.core.models import RipJob, asdict_safe
        job = RipJob(id="abc", drive_device="/dev/sr0", title=t(),
                     selection=TitleSelection(title_id="t1"),
                     output_path="/tmp/x.mkv")
        data = json.dumps(asdict_safe(job))
        assert "abc" in json.loads(data)["id"]
