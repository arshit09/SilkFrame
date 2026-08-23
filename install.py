#!/usr/bin/env python3
"""First run setup for the packaged build, and the launcher after that.

The shipped exe is small. Everything heavy - CPython, PyTorch, ffmpeg and the
RIFE weights - is fetched on first launch from the project that publishes it,
so no large file has to be hosted alongside the release. It all lands in
%LOCALAPPDATA%\\SilkFrame and later launches go straight to the window.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
import zipfile
from pathlib import Path
from tkinter import ttk

from version import __version__

HOME = Path(os.environ.get("SILKFRAME_HOME") or
            Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SilkFrame")
MARKER = HOME / "installed.json"
LOG = HOME / "setup.log"

PYTHON = "3.13.9"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON}/python-{PYTHON}-embed-amd64.zip"
PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-gpl.zip")
TORCH_INDEX = {"gpu": "https://download.pytorch.org/whl/cu128",
               "cpu": "https://download.pytorch.org/whl/cpu"}
# What each choice pulls down in total, for the radio buttons to be honest about.
DOWNLOAD_SIZE = {"gpu": "about 3 GB", "cpu": "about 600 MB"}

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def interpreter(windowed=False):
    return HOME / "python" / ("pythonw.exe" if windowed else "python.exe")


def child_env():
    """The environment every installed tool and the app itself runs under.

    ffmpeg is put on PATH, which is where a source checkout finds it too. The
    python settings matter more than they look: a machine with its own python
    has a per-user site-packages that our interpreter would otherwise import
    from, and pip would then call a torch that is already there - the wrong
    build, in the wrong place - reason enough to install nothing. Clearing them
    here rather than passing flags covers the workers the app starts as well.
    """
    env = dict(os.environ)
    env["PATH"] = f"{HOME / 'ffmpeg'}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONNOUSERSITE"] = "1"
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"):
        env.pop(name, None)
    return env


def bundled():
    """Where the app's own source sits: inside the exe, or beside this file."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def log(message):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as out:
        out.write(message.rstrip() + "\n")


def run(*command):
    """Run one of the installed tools, keeping its output for the log file."""
    log(f"$ {' '.join(str(part) for part in command)}")
    result = subprocess.run([str(part) for part in command], cwd=HOME, env=child_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", creationflags=NO_WINDOW)
    log(result.stdout)
    if result.returncode:
        tail = "\n".join(result.stdout.strip().splitlines()[-3:])
        raise RuntimeError(f"{Path(command[0]).name} failed:\n{tail}")


def download(url, destination, report):
    """Fetch a file, reporting the fraction done as it goes."""
    log(f"GET {url}")
    request = urllib.request.Request(url, headers={"User-Agent": f"SilkFrame/{__version__}"})
    part = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        if not total:
            report(None)
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                report(done / total)
    shutil.move(part, destination)


def unpack(archive, destination, prefix=None):
    """Extract an archive, optionally only the members under one directory."""
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.namelist():
            if prefix and not member.startswith(prefix):
                continue
            name = member[len(prefix):] if prefix else member
            if not name or name.endswith("/"):
                continue
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)


def install_python(report):
    """Unpack the embeddable CPython and give it pip.

    The embeddable build ships with its import paths frozen into a ._pth file
    and site disabled, which is what keeps pip's installs off sys.path, so both
    have to be turned back on before anything can be installed into it. That
    same file is also why the app's own directory has to be named here: a ._pth
    replaces the path entirely, including the entry for the directory of the
    script being run, so without it gui.py cannot import the module next to it.
    """
    root = HOME / "python"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    archive = HOME / "python-embed.zip"
    download(PYTHON_URL, archive, report)
    unpack(archive, root)
    archive.unlink()

    for path_file in root.glob("python*._pth"):
        text = path_file.read_text(encoding="utf-8").replace("#import site", "import site")
        path_file.write_text(f"{text}\nLib\\site-packages\n{HOME / 'app'}\n", encoding="utf-8")
    (root / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)

    get_pip = HOME / "get-pip.py"
    download(PIP_URL, get_pip, report)
    run(interpreter(), get_pip, "--no-warn-script-location")
    get_pip.unlink()


def install_packages(flavour, report):
    """torch from the index that carries the build we want, the rest from PyPI.

    The pytorch index is asked for torch on its own and without a fallback: its
    wheels are the ones tagged +cu128 or +cpu, and letting PyPI into the same
    resolve is how you end up with whichever build happens to be numbered
    highest rather than the one that was chosen here.
    """
    report(None)
    run(interpreter(), "-m", "pip", "install", "--no-warn-script-location",
        "--index-url", TORCH_INDEX[flavour], "torch")
    run(interpreter(), "-m", "pip", "install", "--no-warn-script-location",
        "numpy", "pywebview")


def install_ffmpeg(report):
    """Two executables out of the standard win64 build."""
    root = HOME / "ffmpeg"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    archive = HOME / "ffmpeg.zip"
    download(FFMPEG_URL, archive, report)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.namelist():
            if Path(member).name in ("ffmpeg.exe", "ffprobe.exe"):
                with bundle.open(member) as source, open(root / Path(member).name, "wb") as out:
                    shutil.copyfileobj(source, out)
    archive.unlink()
    if not (root / "ffmpeg.exe").exists():
        raise RuntimeError("the ffmpeg build did not contain ffmpeg.exe")


def install_app(report):
    """Copy the app's own source out of the exe."""
    report(None)
    root = HOME / "app"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    source = bundled()
    for name in ("gui.py", "silkframe.py", "video.py", "version.py"):
        shutil.copy2(source / name, root / name)
    for name in ("rife", "web"):
        shutil.copytree(source / name, root / name)


def install_weights(report):
    """Let the app's own downloader put the checkpoint in its cache."""
    report(None)
    run(interpreter(), "-c",
        f"import sys; sys.path.insert(0, r'{HOME / 'app'}'); "
        "from rife.model import DEFAULT_MODEL, fetch_weights; print(fetch_weights(DEFAULT_MODEL))")


STEPS = [
    ("Downloading Python", install_python),
    ("Downloading PyTorch", install_packages),
    ("Downloading ffmpeg", install_ffmpeg),
    ("Unpacking SilkFrame", install_app),
    ("Downloading the RIFE weights", install_weights),
]


def install(flavour, announce, report):
    """Work through the steps, then record what was installed."""
    HOME.mkdir(parents=True, exist_ok=True)
    for number, (label, step) in enumerate(STEPS, 1):
        announce(f"{label} ({number} of {len(STEPS)})")
        log(f"--- {label}")
        step(flavour, report) if step is install_packages else step(report)
    MARKER.write_text(json.dumps({"version": __version__, "flavour": flavour}), encoding="utf-8")


def installed():
    """The record of a finished install, or None if there is not one."""
    try:
        return json.loads(MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def launch():
    subprocess.Popen([str(interpreter(windowed=True)), str(HOME / "app" / "gui.py")],
                     cwd=HOME / "app", env=child_env(), creationflags=NO_WINDOW)


def has_nvidia():
    """Whether to offer the CUDA build as the default choice."""
    return bool(shutil.which("nvidia-smi") or
                Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe").exists())


class Setup:
    """A small window that runs the install and then starts the app."""

    def __init__(self):
        self.updates = queue.Queue()
        self.root = tk.Tk()
        self.root.title(f"SilkFrame {__version__} setup")
        self.root.resizable(False, False)
        frame = ttk.Frame(self.root, padding=20)
        frame.grid()

        ttk.Label(frame, text="SilkFrame needs a few things before its first run.",
                  font=("Segoe UI", 11, "bold")).grid(sticky="w")
        ttk.Label(frame, wraplength=430, justify="left",
                  text="They are downloaded from the projects that publish them - python.org, "
                       "PyTorch, ffmpeg and the RIFE weights - and kept in "
                       f"{HOME}. This happens once.").grid(sticky="w", pady=(6, 14))

        self.flavour = tk.StringVar(value="gpu" if has_nvidia() else "cpu")
        self.choices = []
        for value, text in (("gpu", "NVIDIA graphics card - much faster"),
                            ("cpu", "No graphics card - works anywhere, far slower")):
            choice = ttk.Radiobutton(frame, text=f"{text}  ({DOWNLOAD_SIZE[value]})",
                                     value=value, variable=self.flavour)
            choice.grid(sticky="w")
            self.choices.append(choice)

        self.status = ttk.Label(frame, text="", wraplength=430, justify="left")
        self.status.grid(sticky="w", pady=(14, 4))
        self.bar = ttk.Progressbar(frame, length=430, mode="determinate", maximum=1000)
        self.bar.grid(sticky="w")
        self.button = ttk.Button(frame, text="Install and start", command=self.start)
        self.button.grid(sticky="e", pady=(14, 0))

    def start(self):
        self.button.state(["disabled"])
        for choice in self.choices:
            choice.state(["disabled"])
        threading.Thread(target=self.work, args=(self.flavour.get(),), daemon=True).start()
        self.pump()

    def work(self, flavour):
        try:
            install(flavour, lambda text: self.updates.put(("status", text)),
                    lambda fraction: self.updates.put(("progress", fraction)))
            self.updates.put(("done", None))
        except Exception as error:  # any failure has to reach the window, not a dead console
            log(f"FAILED: {error}")
            self.updates.put(("failed", str(error)))

    def pump(self):
        """Drain what the worker thread has said since the last tick."""
        while True:
            try:
                kind, value = self.updates.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status.configure(text=value)
                self.bar.configure(mode="determinate", value=0)
            elif kind == "progress":
                if value is None:
                    self.bar.configure(mode="indeterminate")
                    self.bar.start(12)
                else:
                    self.bar.stop()
                    self.bar.configure(mode="determinate", value=value * 1000)
            elif kind == "done":
                launch()
                self.root.destroy()
                return
            elif kind == "failed":
                self.bar.stop()
                self.status.configure(text=f"Setup failed: {value}\n\nThe details are in {LOG}.")
                self.button.configure(text="Try again")
                self.button.state(["!disabled"])
                for choice in self.choices:
                    choice.state(["!disabled"])
                return
        self.root.after(100, self.pump)

    def run(self):
        self.root.mainloop()


def main():
    record = installed()
    if record and interpreter().exists():
        if record.get("version") != __version__:
            install_app(lambda fraction: None)  # a new release, same downloads
            MARKER.write_text(json.dumps({**record, "version": __version__}), encoding="utf-8")
        launch()
        return
    Setup().run()


if __name__ == "__main__":
    main()
