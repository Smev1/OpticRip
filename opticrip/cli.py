"""Console front-end (``makemkvcon``-shaped).

Exit codes follow CLONE_SPEC §19.3: 0 ok, 1 usage/key, 2 firmware tool,
10 source, 11 save, 127 unknown command.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import BANNER, PRODUCT, __version__
from .engine import Engine
from .profiles import ProfileError, builtin_default, load_named
from .robot import HumanWriter, RobotWriter
from .settings import Settings

USAGE = """Use: opticrip [switches] Command [Parameters]

Commands:
  info <source>            prints info about disc
  mkv <source> <title id> <destination folder>   saves a single title to mkv file
  backup <source> <destination folder>           backs up disc to a hard drive
  f <args>                 run universal firmware tool
  reg <key string or file name>                  enter registration key into program

Source specification:
  iso:<FileName>    - open iso image <FileName>
  file:<FolderName> - open files in folder <FolderName>
  disc:<DiscId>     - open disc with id <DiscId> (see list Command)
  dev:<DeviceName>  - open disc with OS device name <DeviceName>

Switches:
  -r --robot        - turn on "robot" mode, see the automation documentation
"""

COMMANDS = {"info", "mkv", "backup", "f", "reg", "guiserver"}


def _parse(argv: list[str]) -> tuple[bool, list[str], int | None]:
    robot = False
    rest: list[str] = []
    for arg in argv:
        if arg in ("-r", "--robot"):
            robot = True
        elif arg.startswith("-") and arg not in ("-", "--"):
            name = arg.lstrip("-")[:1]
            sys.stderr.write(f"{PRODUCT.lower()}: unrecognized option: {name}\n")
            sys.stdout.write(USAGE)
            return robot, rest, 1
        else:
            rest.append(arg)
    return robot, rest, None


def _writer(robot: bool):
    return RobotWriter() if robot else HumanWriter()


def _engine(robot: bool) -> Engine:
    settings = Settings()
    name = settings.get_str("app_DefaultProfileName", "default")
    try:
        profile = load_named(name)
    except ProfileError:
        profile = builtin_default()
    return Engine(_writer(robot), settings, profile)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    robot, args, early = _parse(argv)
    if early is not None:
        return early
    if not args:
        sys.stdout.write(USAGE)
        return 1
    command = args[0].lower()
    if command == "guiserver":
        return _engine(True).serve()
    if command not in COMMANDS:
        # Verified quirk: `list` is advertised but rejected (R-CLI-02).
        sys.stdout.write(f"Invalid command: {args[0]}\n")
        return 127
    engine = _engine(robot)
    if command != "reg":
        engine.startup()
    try:
        if command == "info":
            if len(args) < 2:
                sys.stdout.write(USAGE)
                return 1
            result = engine.open_source(args[1], emit_info=True)
            return result.code
        if command == "mkv":
            if len(args) < 4:
                sys.stdout.write(USAGE)
                return 1
            result = engine.open_source(args[1], emit_info=False)
            if result.disc is None:
                engine.message(5010)
                return 11
            try:
                title_id = int(args[2])
            except ValueError:
                engine.message(9014, args[2])
                return 11
            if not (0 <= title_id < len(result.disc.titles)):
                engine.message(9014, args[2])
                return 11
            title = result.disc.titles[title_id]
            destination = Path(args[3])
            engine.message(5014, 1, str(destination))
            path = engine.save_title(title, destination)
            if path is None:
                return 11
            engine.message(5005, 1)
            engine.message(5036, 1)
            return 0
        if command == "backup":
            if len(args) < 3:
                sys.stdout.write(USAGE)
                return 1
            ok, code = engine.backup(args[1], Path(args[2]))
            return code if not ok else 0
        if command == "reg":
            if len(args) < 2:
                sys.stdout.write(USAGE)
                return 1
            _ok, code = engine.register(args[1])
            return code
        if command == "f":
            return engine.firmware(args[1:])
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
