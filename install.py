#!/usr/bin/env python3
"""First run setup for the packaged build, the launcher after that, and the
uninstaller.

The shipped exe is small. Everything heavy - CPython, PyTorch, ffmpeg and the
RIFE weights - is fetched on first launch from the project that publishes it,
so no large file has to be hosted alongside the release. It all lands in
%LOCALAPPDATA%\\SilkFrame and later launches go straight to the window.

Apart from the two shortcuts and the entry in Settings > Apps, nothing is
written outside that one directory, which is what lets the entry remove the
install by deleting it: see register(), add_shortcuts() and Uninstaller.
"""

import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
import winreg
import zipfile
from pathlib import Path
from tkinter import ttk

from version import __version__

HOME = Path(os.environ.get("SILKFRAME_HOME") or
            Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SilkFrame")
MARKER = HOME / "installed.json"
LOG = HOME / "setup.log"
WEIGHTS = HOME / "weights"
PIP_CACHE = HOME / "pip-cache"
UNINSTALLER = HOME / "uninstall.exe"
LAUNCHER = HOME / "SilkFrame.exe"
# Where the weights used to go, before they were moved under HOME. An install
# made before that still has 180 MB sitting there for the uninstaller to take.
LEGACY_CACHE = Path.home() / ".cache" / "silkframe"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SilkFrame"
REPO = "https://github.com/arshit09/SilkFrame"
FROM_TEMP = "--uninstall-from-temp"

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

    The two cache directories are aimed inside HOME so that removing the
    install is removing one folder. Left alone, pip keeps its copy of the
    2.7 GB torch wheel in the cache it shares with every other python on the
    machine, where nothing else can safely delete it again, and the weights go
    to ~/.cache/silkframe.
    """
    env = dict(os.environ)
    env["PATH"] = f"{HOME / 'ffmpeg'}{os.pathsep}{env.get('PATH', '')}"
    env["SILKFRAME_CACHE"] = str(WEIGHTS)
    env["PIP_CACHE_DIR"] = str(PIP_CACHE)
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
    announce("Finishing up")
    report(None)
    shutil.rmtree(PIP_CACHE, ignore_errors=True)  # the wheels are unpacked; the cache is dead weight
    # The marker goes first: it is what says the downloads are done, and an
    # install that is on disk should not be repeated because a key would not write.
    MARKER.write_text(json.dumps({"version": __version__, "flavour": flavour}), encoding="utf-8")
    add_shortcuts()
    register()


def installed():
    """The record of a finished install, or None if there is not one."""
    try:
        return json.loads(MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def size_of(path):
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def human(size):
    return f"{size / 2**30:.1f} GB" if size >= 2**30 else f"{size >> 20} MB"


def register():
    """Put SilkFrame in Settings > Apps, with an Uninstall button that works.

    The uninstaller is this exe. At install time sys.executable is the setup
    exe, which already knows every path it would have to delete, so a copy of
    it under HOME is the whole of it - there is no second program to build or
    to attach to a release. The entry goes under HKCU rather than HKLM because
    the install is one user's, under LOCALAPPDATA; that is also why none of
    this needs an administrator.
    """
    if not getattr(sys, "frozen", False):
        return  # a source checkout has no exe for the entry to point at
    shutil.copy2(sys.executable, UNINSTALLER)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in (("DisplayName", "SilkFrame"),
                            ("DisplayVersion", __version__),
                            ("DisplayIcon", str(UNINSTALLER)),
                            ("Publisher", "arshit09"),
                            ("InstallLocation", str(HOME)),
                            ("URLInfoAbout", REPO),
                            ("UninstallString", f'"{UNINSTALLER}" --uninstall')):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name, value in (("EstimatedSize", size_of(HOME) >> 10),  # in KB, as the key wants
                            ("NoModify", 1), ("NoRepair", 1)):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


# Both of the things below are COM, which ctypes reaches by hand. The shortcut
# half has an obvious alternative - WScript.Shell, one line of powershell - and
# it cannot be used: see write_shortcut.
CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
IID_SHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
IID_PERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
FOLDERID_PROGRAMS = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"


def guid(text):
    """One of the identifiers above, as the sixteen bytes COM wants."""
    buffer = (ctypes.c_byte * 16)()
    ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(buffer))
    return ctypes.byref(buffer)


def method(obj, slot, *argtypes):
    """The call sitting at one slot of a COM object's vtable.

    Every one of them takes the object itself first and answers with an
    HRESULT, and oledll turns a failing HRESULT into an OSError, which is the
    whole of the error checking these need.
    """
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(table[slot])


def known_folder(folder):
    """Where Windows says one of its own folders is right now.

    Asked rather than assembled out of the profile: Desktop in particular
    moves - OneDrive's backup takes it - and a shortcut written to the one
    that is no longer being shown is a shortcut nobody ever sees.
    """
    found = ctypes.c_wchar_p()
    ctypes.oledll.shell32.SHGetKnownFolderPath(guid(folder), 0, None, ctypes.byref(found))
    try:
        return Path(found.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(found)  # the one call here that is not an HRESULT


def shortcuts():
    """The two .lnk files: one on the desktop, one in the Start menu."""
    return [known_folder(FOLDERID_DESKTOP) / "SilkFrame.lnk",
            known_folder(FOLDERID_PROGRAMS) / "SilkFrame.lnk"]


def write_shortcut(path):
    """One .lnk at path, pointing at the launcher under HOME.

    Not WScript.Shell, which would be a line of powershell and is the obvious
    way to do this: it puts every path it is handed through the system ANSI
    code page first, so on a Western install a profile named in Cyrillic or
    CJK, or with so much as a Polish l-stroke in it, gets no shortcuts at all,
    and the assignment that would have named the target throws rather than
    writes. IShellLinkW takes the wide strings as they are.
    """
    link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    ctypes.oledll.ole32.CoCreateInstance(guid(CLSID_SHELL_LINK), None, 1,  # in this process
                                         guid(IID_SHELL_LINK_W), ctypes.byref(link))
    try:
        # The slots taken out of IShellLinkW: SetDescription is the eighth entry
        # of its vtable, SetWorkingDirectory the tenth and SetPath the twenty first.
        for slot, value in ((20, str(LAUNCHER)), (9, str(HOME)),
                            (7, "Smooth or slow down video by synthesising new frames")):
            method(link, slot, ctypes.c_wchar_p)(link, value)
        method(link, 0, ctypes.c_void_p, ctypes.c_void_p)(  # QueryInterface
            link, guid(IID_PERSIST_FILE), ctypes.byref(persist))
        method(persist, 6, ctypes.c_wchar_p, ctypes.c_int)(persist, str(path), True)  # Save
    finally:
        for obj in (persist, link):
            if obj:
                method(obj, 2)(obj)  # Release


def add_shortcuts():
    """Put SilkFrame on the desktop and in the Start menu.

    They point at a copy of this exe rather than at pythonw.exe directly: the
    setup exe was run from wherever it was downloaded to and will not be there
    for long, and going through it is also what gets a launch the environment
    child_env() sets up. Running it with no arguments is a launch - see main().
    """
    if not getattr(sys, "frozen", False):
        return  # a source checkout has no exe for the shortcuts to point at
    try:
        if Path(sys.executable).resolve() != LAUNCHER.resolve():
            shutil.copy2(sys.executable, LAUNCHER)  # unless this already is that copy, being run
        ctypes.oledll.ole32.CoInitialize(None)  # COM is per thread, and this one is a worker
        try:
            for path in shortcuts():
                if path.parent.is_dir():
                    write_shortcut(path)
        finally:
            ctypes.oledll.ole32.CoUninitialize()
    except OSError as error:
        # An icon is not worth failing an install that has otherwise finished
        # over. What went wrong is in the log.
        log(f"shortcuts not created: {error}")


def unregister():
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass


def relaunch_from_temp():
    """Start the uninstaller again from outside the folder it has to delete.

    Windows holds a running exe open, and this one lives in HOME, so HOME
    cannot go while it is the thing doing the deleting. The copy left in %TEMP%
    afterwards is 12 MB and is cleared with the rest of %TEMP%.
    """
    copy = Path(tempfile.gettempdir()) / f"SilkFrame-{__version__}-uninstall.exe"
    shutil.copy2(sys.executable, copy)
    subprocess.Popen([str(copy), FROM_TEMP])


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


class Uninstaller:
    """A small window that names what will go, then deletes it.

    It only ever runs from the copy of itself in %TEMP% - see
    relaunch_from_temp - so that nothing it is deleting is open at the time.
    """

    def __init__(self):
        self.updates = queue.Queue()
        self.targets = [path for path in (HOME, LEGACY_CACHE) if path.exists()]
        self.root = tk.Tk()
        self.root.title(f"Remove SilkFrame {__version__}")
        self.root.resizable(False, False)
        frame = ttk.Frame(self.root, padding=20)
        frame.grid()

        ttk.Label(frame, text="Remove SilkFrame and everything its setup downloaded?",
                  font=("Segoe UI", 11, "bold")).grid(sticky="w")
        for path in self.targets:
            ttk.Label(frame, text=str(path),
                      wraplength=430, justify="left").grid(sticky="w", pady=(6, 0))
        # Adding up 22000 files takes about seven seconds, which is seven
        # seconds of nothing on screen if the window waits for it.
        self.freed = ttk.Label(frame, text="Working out how much that frees...")
        self.freed.grid(sticky="w", pady=(6, 0))
        ttk.Label(frame, wraplength=430, justify="left",
                  text="The videos SilkFrame has written are kept. They sit beside the "
                       "files they were made from, not in here.").grid(sticky="w", pady=(12, 0))

        self.status = ttk.Label(frame, text="", wraplength=430, justify="left")
        self.status.grid(sticky="w", pady=(14, 4))
        self.bar = ttk.Progressbar(frame, length=430, mode="indeterminate")
        self.bar.grid(sticky="w")

        buttons = ttk.Frame(frame)
        buttons.grid(sticky="e", pady=(14, 0))
        self.cancel = ttk.Button(buttons, text="Cancel", command=self.root.destroy)
        self.cancel.grid(row=0, column=0, padx=(0, 8))
        self.button = ttk.Button(buttons, text="Remove", command=self.start)
        self.button.grid(row=0, column=1)

    def start(self):
        self.button.state(["disabled"])
        self.cancel.state(["disabled"])
        self.bar.start(12)
        threading.Thread(target=self.work, daemon=True).start()

    def measure(self):
        try:
            self.updates.put(("sized", sum(size_of(path) for path in self.targets)))
        except OSError:
            pass  # Remove was pressed mid-count; what it would have said no longer matters

    def work(self):
        try:
            unregister()
            for shortcut in shortcuts():
                shortcut.unlink(missing_ok=True)
            # Recomputed rather than taken from self.targets: a first attempt
            # that stopped on a locked file leaves some of them already gone.
            for path in [path for path in self.targets if path.exists()]:
                self.updates.put(("status", f"Removing {path}"))
                shutil.rmtree(path)
            self.updates.put(("done", None))
        except OSError as error:
            self.updates.put(("failed", str(error)))

    def pump(self):
        """Drain what the worker thread has said since the last tick."""
        while True:
            try:
                kind, value = self.updates.get_nowait()
            except queue.Empty:
                break
            if kind == "sized":
                self.freed.configure(text=f"Frees {human(value)}.")
            elif kind == "status":
                self.status.configure(text=value)
            elif kind == "done":
                self.bar.stop()
                self.status.configure(text="SilkFrame has been removed.")
                self.button.configure(text="Close", command=self.root.destroy)
                self.button.state(["!disabled"])
                return
            elif kind == "failed":
                self.bar.stop()
                self.status.configure(text=f"Could not finish: {value}\n\n"
                                           "If SilkFrame is still open, close it and try again.")
                self.button.configure(text="Try again")
                self.button.state(["!disabled"])
                self.cancel.state(["!disabled"])
                # and no return: the retry has to have something still reading
        self.root.after(100, self.pump)

    def run(self):
        threading.Thread(target=self.measure, daemon=True).start()
        self.pump()
        self.root.mainloop()


def main():
    if FROM_TEMP in sys.argv:
        Uninstaller().run()
        return
    # Settings > Apps passes --uninstall; the copy in HOME is called
    # uninstall.exe, so running that means the same thing on its own.
    if "--uninstall" in sys.argv or Path(sys.executable).resolve() == UNINSTALLER.resolve():
        relaunch_from_temp()
        return
    record = installed()
    if record and interpreter().exists():
        if record.get("version") != __version__:
            install_app(lambda fraction: None)  # a new release, same downloads
            add_shortcuts()  # a launcher carrying this release, not the one it replaced
            register()  # a fresh uninstall.exe, and the new version in Settings
            MARKER.write_text(json.dumps({**record, "version": __version__}), encoding="utf-8")
        launch()
        return
    Setup().run()


if __name__ == "__main__":
    main()
