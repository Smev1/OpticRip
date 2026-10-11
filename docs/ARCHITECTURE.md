# OpticRip architecture

## Goal

A MakeMKV-class ripper with its **own** disc engine: OpticRip parses the disc
structures itself and drives lossless remuxing; it is not a wrapper around
MakeMKV or `makemkvcon`.

## Layering

```
            +------------------------------------------------------+
            |  opticrip.gui (PySide6)      opticrip.cli (argparse) |
            |  presentation only - no OS calls, no parsing         |
            +-----------------------------+------------------------+
                                          |  domain models + events
            +-----------------------------v------------------------+
            |  opticrip.core                                       |
            |    models      enums/dataclasses (Title, StreamInfo, |
            |                DiscAnalysis, RipJob, ProgressUpdate)  |
            |    interfaces  ABCs every backend implements          |
            |    config      Settings (+ JSON persistence)          |
            |    naming      output filename templating/sanitising  |
            |    diskutil    free-space checks                      |
            |    logging_bus in-process log fan-out                 |
            +-----------------------------+------------------------+
                                          |
    +-----------------+-------------------+-------------------+--------------+
    |  drives         |  disc             |  rip              |              |
    |  linux sysfs/   |  identify         |  extract          |              |
    |  udev discovery |  dvd/ifo  parser  |  ffmpeg remux     |              |
    |                 |  bd/mpls  parser  |  mkvmerge remux   |              |
    |                 |  bd/clpi  parser  |  validate         |              |
    |                 |  fs (dir, iso9660)|  engine/queue     |              |
    +-----------------+-------------------+-------------------+--------------+
```

Every component below `core` implements an interface from
`opticrip/core/interfaces.py`:

| Interface         | Responsibility                                     | Implementations |
|-------------------|----------------------------------------------------|-----------------|
| `DriveProvider`   | enumerate media sources                            | `drives.linux.LinuxDriveProvider`, `drives.virtual.FolderProvider` |
| `BlockDevice`     | random-access 2048-byte-sector reader              | `disc.fs.FileBlockDevice` (image/device), `disc.fs.BytesBlockDevice` (tests) |
| `DiscFileSystem`  | read files off a disc without mounting             | `disc.fs.DirectoryDisc`, `disc.fs.Iso9660Disc` |
| `DiscAnalyzer`    | drive/folder -> `DiscAnalysis`                     | `disc.dvd.DvdIfoAnalyzer`, `disc.bd.BlurayMplsAnalyzer`, `disc.ffprobe.FfprobeAnalyzer` |
| `StreamExtractor` | copy a title's streams out of the source           | `rip.extract.DvdCellExtractor`, `rip.extract.BlurayPlaylistExtractor`, `rip.extract.PassthroughExtractor` |
| `Remuxer`         | write exactly the chosen streams into MKV          | `rip.ffmpeg.FFmpegRemuxer`, `rip.mkvmerge.MkvmergeRemuxer` |
| `OutputValidator` | re-open the result and verify it                   | `rip.validate.FfprobeValidator` |
| `DiskSpaceChecker`| free-space accounting                              | `core.diskutil.OsDiskSpaceChecker` |
| `JobQueue`        | ordered, cancellable job list                      | `rip.queue.JobQueue` |

Because the GUI only touches `core` types and calls into the engine through
these interfaces, any single backend can be swapped (e.g. add a Windows
`DriveProvider`, or an external decryptor behind `StreamExtractor`) without
touching the rest of the application.

## The three sources a "drive" can be

Everything downstream treats a source identically, which is what makes the
engine testable without hardware:

* `DriveKind.RAW` - a real block device such as `/dev/sr0`.
* `DriveKind.IMAGE` - an `.iso`/`.img` file.
* `DriveKind.FOLDER` - an already-mounted `VIDEO_TS/` or `BDMV/` directory.

## End-to-end rip pipeline

1. **Discover** (`drives`) - enumerate `/dev/sr*`, read sysfs/udev attributes,
   classify the inserted media, report state.
2. **Identify** (`disc.identify`) - look for `VIDEO_TS`, `BDMV`, audio-CD or
   data-disc signatures without mounting.
3. **Analyse** (`disc.*`) - parse `VTS_xx_0.IFO` (DVD) or `MPLS`/`CLPI`
   (Blu-ray) into `Title`/`StreamInfo`/`Chapter` objects; fall back to
   `ffprobe` when a structure is malformed.
4. **Select** - the user picks titles, video/audio/subtitle streams and
   chapter ranges; `core.models.validate_selection` guarantees no requested
   track is silently dropped.
5. **Extract** (`rip.extract`) - DVD: resolve the title's PGC -> cells, map
   each cell to a byte range in its `VTS_xx_y.VOB`, and stream those ranges
   into a local MPEG-PS file. Blu-ray: hand the playlist to ffmpeg via the
   `bluray:` protocol, or concatenate the clips.
6. **Remux** (`rip.ffmpeg` / `rip.mkvmerge`) - `-map` the selected streams and
   `-c copy` into Matroska. No pixel or sample is ever re-encoded.
7. **Validate** (`rip.validate`) - re-probe the output, compare the stream
   list, duration and chapter count against what was requested.
8. **Report** - progress/ETA/log events flow to the GUI through
   `ProgressUpdate` and the `LogBus`.

## Cancellation, retries and error recovery

Every long operation receives a `cancelled()` callable and a `progress()`
callable. Cancellation is cooperative: extractors and the muxer check it
between chunks and terminate the child process group, then delete partial
output unless `Settings.keep_failed_outputs` is set. `rip.engine` performs
bounded retries for transient `DiscReadError`s and tracks free space before
each job.

## What is deliberately *not* here

No decryption. CSS/CPPM (DVD) and AACS/BD+ (Blu-ray) are out of scope for the
initial release; see `docs/PROTECTED_DISCS.md`. The engine exposes a clean
`StreamExtractor` seam where a lawful decryptor could be plugged in later.
