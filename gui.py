#!/usr/bin/env python3
"""Drag and drop front end for silkframe.py.

    python gui.py          (or pythonw gui.py for no console window)

The interface is plain HTML, CSS and JavaScript in web/, rendered by pywebview
in a native window. Drop videos on it and each one is written next to its
source. With staging enabled the file is copied to a fast drive first,
processed there, and the result is moved back; the copy and everything else in
the working folder is deleted afterwards.
"""

import ctypes
import json
import os
import queue
import re
import shutil
import string
import subprocess
import sys
import tempfile
import threading
from ctypes import wintypes
from pathlib import Path

import webview
from webview.dom import DOMEventHandler

SILKFRAME = Path(__file__).resolve().parent / "silkframe.py"
KEEP_CONTAINER = {".mp4", ".mkv", ".mov", ".m4v"}
PROGRESS = re.compile(r"(\d+)/(\d+) frames")
NOT_IN_A_NAME = re.compile(r'[<>:"/\\|?*]')
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FROZEN = getattr(sys, "frozen", False)
WORKER_FLAG = "--interpolate"
VIDEO_TYPES = (
    "Video (*.mp4;*.mkv;*.mov;*.m4v;*.avi;*.webm;*.wmv;*.flv;*.mpg;*.mpeg;*.ts;*.m2ts)",
    "All files (*.*)",
)


def interpreter():
    """python.exe rather than pythonw.exe, so the child can talk on stderr."""
    executable = Path(sys.executable)
    console = executable.with_name(executable.name.replace("pythonw", "python"))
    return str(console if console.exists() else executable)


def worker_command(*arguments):
    """A packaged build re-runs itself for the work; a checkout runs the script."""
    if FROZEN:
        return [sys.executable, WORKER_FLAG, *arguments]
    return [interpreter(), "-u", str(SILKFRAME), *arguments]


def web_root():
    """The front end, beside this file in a checkout and inside a packaged build."""
    packaged = getattr(sys, "_MEIPASS", None)
    return Path(packaged if packaged else Path(__file__).resolve().parent) / "web"


def drives():
    found = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:{os.sep}"
        if os.path.exists(root):
            try:
                free = shutil.disk_usage(root).free
            except OSError:
                continue
            found.append((f"{letter}:", free))
    return found


def working_dir(drive):
    """A private folder on `drive` to process in."""
    if os.path.splitdrive(tempfile.gettempdir())[0].upper() == drive.upper():
        return Path(tempfile.mkdtemp(prefix="silkframe-"))
    parent = Path(f"{drive}{os.sep}silkframe-temp")
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="silkframe-", dir=parent))


def auto_tag(mode, factor):
    """The name part a file takes when no suffix was typed for it."""
    if mode == "slowmo":
        return "slowmo" if factor == 2 else f"slowmo{factor}x"
    return f"{factor}x"


def output_for(source, mode, factor, suffix):
    container = source.suffix if source.suffix.lower() in KEEP_CONTAINER else ".mp4"
    tag = NOT_IN_A_NAME.sub("", suffix).strip(" .") or auto_tag(mode, factor)
    return source.with_name(f"{source.stem}.{tag}{container}")


# A frameless window has no resize border either, and putting the native one
# back means letting Windows draw a frame - which it paints black, as a bar
# above the toolbar. So the page grips its own edges instead and pushes a new
# rectangle through here, and the window stays exactly as wide as the page.
SWP_NOZORDER = 0x0004
USER32 = ctypes.windll.user32


class Frame:
    """What the title bar used to do, for a window that no longer has one."""

    def __init__(self, window):
        self.window = window
        self.hwnd = window.native.Handle.ToInt32()
        self.fit_to_screen()

    def fit_to_screen(self):
        """Maximised, a borderless form covers the taskbar unless it is told
        where the screen ends. Done once up front so Windows' own maximise
        lands right too, and again on the way in, in case the window has since
        been dragged onto a different screen."""
        from System.Drawing import Rectangle
        from System.Windows.Forms import Screen

        area = Screen.FromControl(self.window.native).WorkingArea
        self.window.native.MaximizedBounds = Rectangle(
            area.X, area.Y, area.Width, area.Height)

    def toggle_maximize(self):
        if USER32.IsZoomed(self.hwnd):
            self.window.restore()
        else:
            self.fit_to_screen()
            self.window.maximize()

    def resize(self, left, top, width, height):
        """A new rectangle from the page's edge grips, in screen pixels."""
        if USER32.IsZoomed(self.hwnd):
            return
        box = wintypes.RECT()
        USER32.GetWindowRect(self.hwnd, ctypes.byref(box))
        floor = self.window.native.MinimumSize
        # At the minimum the dragged edge stops and the opposite one holds.
        if width < floor.Width:
            left = box.left if left == box.left else box.right - floor.Width
            width = floor.Width
        if height < floor.Height:
            top = box.top if top == box.top else box.bottom - floor.Height
            height = floor.Height
        USER32.SetWindowPos(self.hwnd, 0, left, top, width, height, SWP_NOZORDER)


class Api:
    """Everything the page is allowed to call, as window.pywebview.api.*."""

    def __init__(self, app):
        # Underscored so pywebview does not walk App - and through it the whole
        # native WebView2 object tree - while working out what to expose to JS.
        self._app = app

    def boot(self):
        letters = drives()
        preferred = "C:" if any(letter == "C:" for letter, _ in letters) else (
            letters[0][0] if letters else "")
        return {
            "drives": letters,
            "drive": preferred,
            "mode": self._app.options["mode"],
            "factor": self._app.options["factor"],
            "device": self._app.options["device"],
            "suffix": self._app.options["suffix"],
            "stage": self._app.options["drive"] is not None,
            "greeting": "ready - drop a video, or choose one from disk",
        }

    def set_options(self, mode, factor, device, suffix, stage, drive):
        """The options a file carries are the ones that were set when it was added."""
        self._app.options = {"mode": mode, "factor": int(factor), "device": device,
                             "suffix": suffix, "drive": drive if stage else None}

    def browse(self):
        chosen = self._app.window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True, file_types=VIDEO_TYPES)
        self._app.add(chosen or [])

    def cancel(self):
        self._app.cancel()

    def minimize(self):
        self._app.window.minimize()

    def toggle_maximize(self):
        self._app.frame.toggle_maximize()

    def resize_window(self, left, top, width, height):
        self._app.frame.resize(int(left), int(top), int(width), int(height))

    def close(self):
        self._app.window.destroy()


class App:
    def __init__(self):
        self.window = None
        self.frame = None
        self.messages = queue.Queue()
        self.jobs = queue.Queue()
        self.process = None
        self.cancelled = threading.Event()
        self.loaded = threading.Event()
        self.current = ""
        self.done = 0
        self.results = []
        self.options = {"mode": "fps", "factor": 2, "device": "auto", "suffix": "",
                        "drive": None}

    # ---------------------------------------------------------------- startup

    def attach(self, window):
        self.window = window
        window.events.loaded += self.on_loaded
        window.events.closing += self.cancel
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.pump, daemon=True).start()
        threading.Thread(target=self.probe_devices, daemon=True).start()

    def probe_devices(self):
        """Ask the worker what torch can see here, and fill the device picker.

        Importing torch in this process instead would add seconds to the window
        opening, so the list arrives a moment after the window does and the
        picker holds nothing but Auto until it lands.
        """
        try:
            listed = subprocess.run(worker_command("--list-devices"), capture_output=True,
                                    text=True, timeout=300, cwd=str(SILKFRAME.parent),
                                    creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            return
        devices = [line.split("\t", 1) for line in listed.splitlines() if "\t" in line]
        if devices:
            self.push(kind="devices", devices=devices)

    def on_loaded(self):
        """Only Python can see the real path of a dropped file, so the drop
        listener lives here rather than in app.js."""
        self.loaded.set()  # released first: losing the drop must not mute the page
        self.frame = Frame(self.window)
        target = self.window.dom.get_element("#drop")
        if target:
            target.events.drop += DOMEventHandler(self.on_drop, prevent_default=True)

    # ------------------------------------------------------------- job intake

    def on_drop(self, event):
        paths = []
        for item in event.get("dataTransfer", {}).get("files", []):
            path = item.get("pywebviewFullPath")
            if path:
                paths.append(path)
            else:
                self.push(kind="log", text=f"skipped {item.get('name', 'a file')}: "
                                           f"the drop carried no path")
        self.add(paths)

    def add(self, paths):
        options = dict(self.options)
        for path in paths:
            source = Path(path)
            if source.is_file():
                self.jobs.put((source, options))
                self.push(kind="log", text=f"queued {source.name}")
            else:
                self.push(kind="log", text=f"skipped {source.name}: not a file")

    def cancel(self):
        self.cancelled.set()
        dropped = 0
        while True:  # cancel means the whole batch, not just the current file
            try:
                self.jobs.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        if dropped:
            self.push(kind="log",
                      text=f"removed {dropped} video{'s' if dropped > 1 else ''} from the queue")
        self.kill(self.process)

    @staticmethod
    def kill(process):
        if process and process.poll() is None:
            if os.name == "nt":  # ffmpeg is a grandchild, so kill the tree
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                               capture_output=True, creationflags=NO_WINDOW)
            else:
                process.kill()

    # ------------------------------------------------------------- the worker

    def worker(self):
        while True:
            source, options = self.jobs.get()
            self.cancelled.clear()
            self.done += 1
            self.current = source.name
            self.push(kind="busy", value=True)
            try:
                self.run(source, options)
                self.results.append((source.name, None))
            except Exception as error:  # one bad file must not stop the batch
                self.push(kind="log", text=f"{source.name}: {error}")
                self.results.append((source.name, str(error)))
            if not self.jobs.qsize():
                self.push(kind="busy", value=False)
                text, failed = self.summary()
                self.push(kind="summary", text=text, failed=failed)
                self.results = []
                self.done = 0

    def run(self, source, options):
        destination = output_for(source, options["mode"], options["factor"],
                                 options["suffix"])
        drive = options["drive"]
        same_drive = drive and os.path.splitdrive(source.resolve())[0].upper() == drive.upper()
        if same_drive:
            self.push(kind="log", text=f"{source.name} is already on {drive}, no copy needed")

        if not drive or same_drive:
            self.announce("starting")
            self.interpolate(source, destination, options)
            self.push(kind="log", text=f"done -> {destination}")
            return

        needed = source.stat().st_size * 3
        free = shutil.disk_usage(f"{drive}{os.sep}").free
        if free < needed:
            raise RuntimeError(f"{drive} has {free / 2**30:.1f} GB free, "
                               f"about {needed / 2**30:.1f} GB is needed")

        work = working_dir(drive)
        try:
            self.announce(f"copying to {drive}")
            staged = work / source.name
            shutil.copy2(source, staged)
            staged_output = work / destination.name

            self.announce(f"processing on {drive}")
            self.interpolate(staged, staged_output, options)

            self.announce(f"moving the result back to {destination.parent}")
            shutil.move(str(staged_output), str(destination))
            self.push(kind="log", text=f"done -> {destination}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if work.parent.name == "silkframe-temp":
                try:  # only succeeds once the last job on this drive is done
                    work.parent.rmdir()
                except OSError:
                    pass
            self.push(kind="log", text=f"cleaned up {work}")

    def interpolate(self, source, destination, options):
        if self.cancelled.is_set():  # cancelled during the copy, before any work started
            raise RuntimeError("cancelled")
        command = worker_command(str(source), "-o", str(destination),
                                 "--mode", options["mode"],
                                 "--factor", str(options["factor"]),
                                 "--device", options["device"])
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            cwd=str(SILKFRAME.parent), env=environment, creationflags=NO_WINDOW,
        )
        if self.cancelled.is_set():  # cancel arrived before the process existed to kill
            self.kill(self.process)
        pending = ""
        while chunk := self.process.stderr.read1(4096):
            pending += chunk.decode("utf-8", "replace")
            lines = re.split(r"[\r\n]+", pending)
            pending = lines.pop()
            for line in lines:
                self.report(line.strip())
        self.report(pending.strip())
        self.process.stderr.close()
        code = self.process.wait()
        self.process = None
        if code != 0:
            if self.cancelled.is_set():
                destination.unlink(missing_ok=True)
                raise RuntimeError("cancelled")
            raise RuntimeError(f"silkframe exited with code {code}")

    def summary(self):
        """What to leave on screen once the queue is empty."""
        total = len(self.results)
        failed = [name for name, error in self.results if error]
        finished = total - len(failed)
        videos = "video" if total == 1 else "videos"
        if self.cancelled.is_set():  # the stopped file is not a failure worth naming
            return f"Cancelled - {finished} of {total} {videos} finished", False
        if failed:
            text = f"Done - {total} {videos}: {finished} finished, {len(failed)} failed"
        else:
            text = f"Done - {total} {videos} finished"
        if failed:
            named = ", ".join(failed[:2])
            if len(failed) > 2:
                named += f" and {len(failed) - 2} more"
            text += f" ({named})"
        return text, bool(failed)

    def announce(self, action):
        """The batch total is read fresh, so files dropped mid run are counted."""
        self.push(kind="status",
                  text=f"{self.current}  ({self.done} of {self.done + self.jobs.qsize()})"
                       f"  -  {action}")

    def report(self, line):
        """Progress lines drive the bar; everything else goes to the log."""
        if not line:
            return
        found = PROGRESS.search(line)
        if found:
            done, total = (int(value) for value in found.groups())
            self.push(kind="progress", done=done, total=total)
            self.announce(line)
        elif not line.startswith(("input ", "model ")):
            self.push(kind="log", text=line)

    # -------------------------------------------------------- thread to window

    def push(self, **message):
        self.messages.put(message)

    def pump(self):
        """One writer to the page, so messages arrive in the order they were made."""
        self.loaded.wait()
        while True:
            message = self.messages.get()
            try:
                self.window.run_js(f"app.push({json.dumps(message)})")
            except Exception:  # the window went away mid batch
                pass


def worker_process():
    """The packaged exe re-runs itself for each video; this is that second run."""
    import io

    # A windowed build starts with no streams attached, and --list-devices
    # answers on stdout while the progress lines go to stderr.
    for number, name in ((1, "stdout"), (2, "stderr")):
        if getattr(sys, name) is None:
            setattr(sys, name, io.TextIOWrapper(io.FileIO(number, "w"),
                                                errors="replace", line_buffering=True))
    import silkframe

    silkframe.main(sys.argv[2:])


def main():
    if len(sys.argv) > 1 and sys.argv[1] == WORKER_FLAG:
        worker_process()
        return
    app = App()
    window = webview.create_window(
        "SilkFrame - double the frames",
        str(web_root() / "index.html"),
        js_api=Api(app),
        width=1040, height=680, min_size=(860, 620),
        background_color="#101010",
        text_select=True,
        # The page draws the title bar; easy_drag would otherwise make every
        # square inch of it a drag handle, not just .pywebview-drag-region.
        frameless=True, easy_drag=False,
    )
    app.attach(window)
    webview.start()


if __name__ == "__main__":
    main()
