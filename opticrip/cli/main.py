"""opticrip command line interface.

Two subcommands today:

    inspect <src> ...   analyse a drive/image/folder, list titles/tracks
    rip <src> ...       (phase 4) actually rip selected content to MKV

``src`` may be a device (``/dev/sr0``), an ``.iso`` image, or a directory
containing ``VIDEO_TS``/``BDMV``. All three route through the same engine.
"""

from __future__ import annotations

import argparse
import os
import sys

from ..core import errors as E
from ..core.config import Settings
from ..core.logging_bus import LogBus, records_to_text
from ..core.models import (
    Drive,
    DriveKind,
    MediaType,
    StreamKind,
    TitleSelection,
    format_bytes,
    format_duration,
)
from ..disc.fs import open_disc_filesystem
from ..disc.dvd import DvdIfoAnalyzer
from ..disc.bd import BluRayAnalyzer
from ..drives.virtual import AutoPathProvider
from ..rip.tools import Toolchain


def _pick_analyzer(fs, drive: Drive, log: LogBus):
    for analyzer, labelled in ((BluRayAnalyzer(log=log), "Blu-ray (BDMV)"),
                               (DvdIfoAnalyzer(log=log), "DVD-Video (VIDEO_TS)")):
        try:
            ok = analyzer.can_handle(drive, fs)
        except Exception:
            ok = False
        if ok:
            log.info(f"media: {labelled}", context="cli")
            return analyzer
    raise E.UnsupportedMediaError(
        "no supported disc structure found at this source",
        hint="OpticRip reads unencrypted BDMV folder trees, VIDEO_TS "
             "folders, devices and .iso images.  Nested VIDEO_TS/BDMV "
             "inside a wrapper folder is handled too.  If the image is "
             "encrypted, install libdvdcss (see docs/INSTALL.md).")


def cmd_inspect(args, log: LogBus) -> int:
    settings = Settings.load()
    tc = Toolchain.detect(
        ffmpeg_path=settings.ffmpeg_path, ffprobe_path=settings.ffprobe_path,
        extra_paths=settings.tool_search_paths, log=log)
    css = tc.dvdcss_path()
    if css:
        log.info(f"CSS: libdvdcss found ({os.path.basename(css)}) - encrypted "
                 "DVDs decrypt via its key derivation", context="cli")
    else:
        log.info("CSS: no libdvdcss installed - unencrypted discs work; "
                 "encrypted discs error honestly", context="cli")

    for capability, ok, detail in tc.capabilities_report():
        if not ok:
            log.warning(f"missing capability: {capability} ({detail})", context="cli")

    provider = AutoPathProvider(args.sources)
    drives = provider.list_drives()
    if not drives:
        log.error("no readable source found among: " + ", ".join(args.sources))
        return 2

    exit_code = 0
    for drive in drives:
        log.info(f"source: {drive.display_name} ({drive.kind.label}) "
                 f"path={drive.path} media={drive.media_type.label}")
        if drive.status_detail:
            log.warning(f"  {drive.status_detail}")
            exit_code = exit_code or 3
            continue
        try:
            fs = open_disc_filesystem(drive.path)
        except E.DiscStructureError as exc:
            log.error(str(exc))
            exit_code = exit_code or 3
            continue
        try:
            analyzer = _pick_analyzer(fs, drive, log)
            analysis = analyzer.analyze(drive, fs)
        except E.OpticRipError as exc:
            log.error(f"{exc.title}: {exc.message}")
            if exc.hint:
                log.error(f"  hint: {exc.hint}")
            exit_code = exit_code or 3
            continue
        _print_analysis(analysis, log)
    return exit_code


def _print_analysis(analysis, log: LogBus) -> None:
    disc = analysis.disc
    log.info(f"disc: {disc.label or '(unlabelled)'} | {disc.format_label or disc.media_type.label}"
             f" | {analysis.analyzer} | {len(analysis.titles)} title(s)")
    for warning in analysis.warnings:
        log.warning("  " + warning)
    for title in analysis.titles:
        flag = " *main feature*" if title.is_main_feature else ""
        log.info(
            f"{title.index:>3}. {title.display_name:<24} {title.duration_label:>8} "
            f"{title.chapter_count:>3} ch {format_bytes(title.size_bytes):>10}{flag}")
        for note in title.notes:
            log.info(f"     note: {note}")
        for stream in title.streams:
            log.info(f"     [{stream.index}] {stream.summary()}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opticrip-cli",
        description="OpticRip - inspect and rip optical discs to MKV.")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser(
        "inspect", help="analyse sources and list titles/tracks")
    inspect.add_argument("sources", nargs="+",
                         help="devices, .iso images, or VIDEO_TS/BDMV folders")
    inspect.set_defaults(func=cmd_inspect)

    rip = sub.add_parser(
        "rip", help="rip selected titles to MKV (Phase 4)")
    rip.add_argument("sources", nargs="+")
    rip.add_argument("--title", type=int, action="append",
                     help="titles to rip, 1-based; repeat for many")
    rip.add_argument("--audio", type=int, action="append",
                     help="audio stream indices (from inspect) to include")
    rip.add_argument("--subtitle", type=int, action="append")
    rip.add_argument("--output", "-o",
                     help="output MKV path (or directory with --template)")
    rip.add_argument("--template",
                     help="output filename template; default from settings")
    rip.add_argument("--overwrite", choices=("ask", "overwrite", "skip", "rename", "error"),
                     help="behaviour when the output file already exists")
    rip.add_argument("--yes", action="store_true",
                     help="assume yes for prompts (scripting)")
    rip.set_defaults(func=cmd_rip)
    return parser


def cmd_rip(args, log: LogBus) -> int:
    from ..core.models import new_id, RipJob
    from ..rip.engine import RipEngine
    settings = Settings.load()
    if args.overwrite:
        settings.overwrite_policy = args.overwrite
    if args.template:
        settings.filename_template = args.template
    if args.output and os.path.isdir(args.output):
        settings.output_dir = args.output

    provider = AutoPathProvider(args.sources)
    drives = provider.list_drives()
    wanted_titles = [t for t in (args.title or []) if t > 0]
    wanted = set(wanted_titles)
    jobs = []
    for drive in drives:
        try:
            fs = open_disc_filesystem(drive.path)
        except E.DiscStructureError as exc:
            log.error(str(exc)); continue
        try:
            analysis = _pick_analyzer(fs, drive, log).analyze(drive, fs)
        except E.OpticRipError as exc:
            log.error(f"{exc.title}: {exc.message}"); continue
        wanted_here = [t for t in wanted_titles if t <= len(analysis.titles)] \
            or (([t.index for t in analysis.titles]) if not wanted_titles else [])
        titles_by_index = {t.index: t for t in analysis.titles}
        for tindex in wanted_here:
            title = titles_by_index.get(tindex)
            if title is None:
                continue
            selection = TitleSelection.all_streams(title)
            if args.audio:
                selection.audio_indices = tuple(i for i in selection.audio_indices
                                                if i in set(args.audio))
            if args.subtitle:
                selection.subtitle_indices = tuple(
                    i for i in selection.subtitle_indices
                    if i in set(args.subtitle))
            out_dir = args.output if (args.output and
                                      not os.path.isdir(args.output)) \
                else settings.output_dir
            job = RipJob(
                id=new_id(), drive_device=drive.path,
                title=title, selection=selection,
                output_path=os.path.join(out_dir, "dummy") if out_dir else "",
                disc_label=drive.volume_label or analysis.disc.label,
                media_type=analysis.disc.media_type,
                drive_kind=drive.kind)
            # exact filename is resolved by the engine's template logic
            from ..core.naming import render_filename
            leaf = render_filename(settings.filename_template, title,
                                   selection, disc_label=job.disc_label)
            job.output_path = os.path.join(out_dir or settings.output_dir, leaf)
            jobs.append(job)

    if not jobs:
        log.error("nothing selected to rip")
        return 2
    log.info(f"ripping {len(jobs)} job(s)")
    exit_code = 0
    from ..rip.queue import JobQueue, QueueEvent

    def on_event(ev: QueueEvent) -> None:
        nonlocal exit_code
        if ev.kind == "progress" and ev.update is not None:
            u = ev.update
            log.info(f"  [{u.phase.label:>10}] {u.message}"
                     + (f" {u.percent}%" if u.percent is not None else ""))
        elif ev.kind == "result" and ev.result is not None:
            r = ev.result
            if r.ok:
                log.info(f"  -> {r.output_path} "
                         f"({format_bytes(r.size_bytes)})")
            else:
                exit_code = 3
                detail = "; ".join(r.warnings) if r.warnings else "no detail"
                log.error(f"  job failed: {detail}")

    queue = JobQueue(settings)
    queue.subscribe(on_event)
    queue.start()
    try:
        for job in jobs:
            queue.add(job)
        if not queue.join(timeout=None) and exit_code == 0:
            exit_code = 3
    except KeyboardInterrupt:
        for job in jobs:
            queue.cancel(job.id)
        raise
    finally:
        queue.stop()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    bus = LogBus()
    bus.subscribe(lambda record: print(record.to_line()))
    if not hasattr(args, "func") or args.func is None:
        parser.print_help()
        print("\nThe 'rip' subcommand arrives with Phase 4.", file=sys.stderr)
        return 1
    try:
        return args.func(args, bus)
    except E.OpticRipError as exc:
        print(f"{exc.title}: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        if os.environ.get("OPTICRIP_DEBUG") and exc.detail:
            print(exc.detail, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
