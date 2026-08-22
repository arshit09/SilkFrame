#!/usr/bin/env python3
"""Package the app so it runs on a machine with no Python on it.

    build.bat              (or: python build.py)
    build.bat nozip        skip the zip step while iterating

Produces dist\\SilkFrame\\SilkFrame.exe with ffmpeg, the model weights and everything
else beside it, plus dist\\SilkFrame-windows.zip to hand over.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
WORK = ROOT / "build"
NAME = "SilkFrame"
# A statically linked ffmpeg is ~100 MB; anything tiny is a launcher shim or a
# dynamically linked build, and neither survives being copied on its own.
STANDALONE = 5 * 2**20


def ensure(module, package=None):
    try:
        __import__(module)
    except ImportError:
        package = package or module
        print(f"installing {package}")
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "--disable-pip-version-check", package], check=True)


def real_tool(name):
    """Locate a self-contained ffmpeg binary.

    Package managers put a small launcher on PATH that re-executes the real
    binary from its own install tree, so copying what ``which`` returns yields
    a stub that cannot find anything.
    """
    found = shutil.which(name)
    if found and Path(found).stat().st_size >= STANDALONE:
        return Path(found)
    roots = [Path(os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey")) / "lib",
             Path.home() / "scoop" / "apps",
             Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"]
    for root in roots:
        if root.is_dir():
            for candidate in root.rglob(f"{name}.exe"):
                if candidate.stat().st_size >= STANDALONE:
                    return candidate
    return None


def size_of(path):
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("nozip", nargs="?", choices=["nozip"], help="skip the zip step")
    skip_zip = parser.parse_args().nozip == "nozip"

    if sys.platform != "win32":
        raise SystemExit("this script builds the windows package; run it on windows")

    ensure("PyInstaller", "pyinstaller")
    ensure("webview", "pywebview")

    ffmpeg, ffprobe = real_tool("ffmpeg"), real_tool("ffprobe")
    if not (ffmpeg and ffprobe):
        raise SystemExit("no self-contained ffmpeg.exe and ffprobe.exe were found. Install a "
                         "static build (the gyan.dev release that chocolatey, scoop and winget "
                         "all ship) and make sure it is on PATH.")

    from rife.model import DEFAULT_MODEL, fetch_weights
    weights = fetch_weights(DEFAULT_MODEL)  # downloads once, then ships inside the build

    print(f"packaging with\n  ffmpeg  {ffmpeg}\n  ffprobe {ffprobe}\n  weights {weights}")
    started = time.monotonic()
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", NAME,
        "--distpath", str(DIST), "--workpath", str(WORK), "--specpath", str(WORK),
        # pywebview and pythonnet register their own PyInstaller hooks; clr_loader
        # has none, and its ClrLoader.dll is what starts the .NET runtime.
        "--collect-all", "clr_loader",
        "--hidden-import", "silkframe",
        "--add-binary", f"{ffmpeg};ffmpeg",
        "--add-binary", f"{ffprobe};ffmpeg",
        "--add-data", f"{weights};weights",
        "--add-data", f"{ROOT / 'web'};web",
        str(ROOT / "gui.py"),
    ], check=True, cwd=ROOT)

    application = DIST / NAME
    print(f"\nbuilt {application} in {(time.monotonic() - started) / 60:.1f} min, "
          f"{size_of(application) / 2**30:.2f} GiB")

    if not skip_zip:
        archive = DIST / f"{NAME}-windows.zip"
        archive.unlink(missing_ok=True)
        print("zipping, this takes a while for a build this size")
        started = time.monotonic()
        subprocess.run(["tar", "-a", "-c", "-f", str(archive), "-C", str(DIST), NAME], check=True)
        print(f"packed {archive} in {(time.monotonic() - started) / 60:.1f} min, "
              f"{size_of(archive) / 2**30:.2f} GiB")

    print(f"\nShare the zip. Your friend unpacks it and runs {NAME}.exe from the folder -\n"
          f"no python, no ffmpeg, no model download needed. Windows will show a\n"
          f"SmartScreen warning for an unsigned app: More info -> Run anyway.")


if __name__ == "__main__":
    main()
