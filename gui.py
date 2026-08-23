#!/usr/bin/env python3
"""Drag and drop front end for silkframe.py.

    python gui.py          (or pythonw gui.py for no console window)

The interface is plain HTML, CSS and JavaScript in web/, rendered by pywebview
in a native window. Drop videos on it, put them in the order you want them, and
press start; each one is written next to its source.
"""

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

import webview
from webview.dom import DOMEventHandler

from version import __version__

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


def auto_tag(mode, factor):
    """The name part a file takes when no suffix was typed for it."""
    if mode == "slowmo":
        return "slowmo" if factor == 2 else f"slowmo{factor}x"
    return f"{factor}x"


def name_tag(mode, factor, suffix):
    """The part of the output name that says what was done to the file."""
    return NOT_IN_A_NAME.sub("", suffix).strip(" .") or auto_tag(mode, factor)


def output_for(source, mode, factor, suffix):
    container = source.suffix if source.suffix.lower() in KEEP_CONTAINER else ".mp4"
    return source.with_name(f"{source.stem}.{name_tag(mode, factor, suffix)}{container}")


def card(item, state):
    """The part of a queued file the page draws a row from - its settings
    included, because clicking the row brings them back up in the sidebar."""
    return {"id": item["id"], "name": item["name"], "tag": item["tag"],
            "state": state, "options": item["options"]}


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
        return {
            "mode": self._app.options["mode"],
            "factor": self._app.options["factor"],
            "device": self._app.options["device"],
            "suffix": self._app.options["suffix"],
            "audio": self._app.options["audio"],
            "greeting": "ready - drop videos, put them in order, then press start",
        }

    def set_options(self, item_id, patch):
        """The settings that just changed, for one waiting file - or, with no
        file named, for all of them. Only what changed is sent, so setting the
        factor for every file leaves each one's own suffix alone."""
        self._app.configure(int(item_id or 0), dict(patch))

    def browse(self):
        chosen = self._app.window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True, file_types=VIDEO_TYPES)
        self._app.add(chosen or [])

    def start(self):
        self._app.start()

    def stop(self):
        self._app.stop()

    def move(self, item_id, index):
        """One waiting file to a new place in the line."""
        self._app.move(int(item_id), int(index))

    def remove(self, item_id):
        self._app.remove(int(item_id))

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
        # The line of files is a plain list rather than a queue.Queue, because
        # the page reads it, reorders it and takes things out of the middle of
        # it; the condition is what the worker sleeps on between jobs.
        self.lock = threading.Condition()
        self.pending = []        # waiting, in the order they will run
        self.active = None       # what the worker has in hand
        self.running = False     # start pressed, stop not yet
        self.revision = 0        # so a snapshot in flight cannot undo a newer move
        self.numbered = 0        # the ids the page holds its rows by
        self.process = None
        self.cancelled = threading.Event()
        self.loaded = threading.Event()
        self.current = ""
        self.results = []
        self.options = {"mode": "fps", "factor": 2, "device": "auto", "suffix": "",
                        "audio": "mute"}

    # ---------------------------------------------------------------- startup

    def attach(self, window):
        self.window = window
        window.events.loaded += self.on_loaded
        window.events.closing += self.stop
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
        """Files join the back of the line, carrying the settings the sidebar
        held as they arrived; nothing runs until start is pressed."""
        options = dict(self.options)
        sources = []
        for path in paths:
            source = Path(path)
            if source.is_file():
                sources.append(source)
            else:
                self.push(kind="log", text=f"skipped {source.name}: not a file")
        if not sources:
            return
        with self.lock:
            for source in sources:
                self.numbered += 1
                self.pending.append({
                    "id": self.numbered,
                    "name": source.name,
                    "tag": name_tag(options["mode"], options["factor"], options["suffix"]),
                    "source": source,
                    "options": options,
                })
            self.revision += 1
        self.publish()

    def configure(self, item_id, patch):
        """Settings belong to one file each. A waiting file named here takes
        what changed; with no file named, every waiting file takes it and so do
        the files added next. A file already being worked on keeps what it
        started with either way, its settings being with the worker already."""
        if "factor" in patch:
            patch["factor"] = int(patch["factor"])
        with self.lock:
            if item_id:
                taking = [one for one in self.pending if one["id"] == item_id]
                if not taking:   # started, or dropped, since the page last looked
                    return
            else:
                self.options.update(patch)
                taking = list(self.pending)
            for item in taking:
                item["options"] = {**item["options"], **patch}
                item["tag"] = name_tag(item["options"]["mode"], item["options"]["factor"],
                                       item["options"]["suffix"])
            self.revision += 1
        self.publish()

    # -------------------------------------------------------------- the queue

    def start(self):
        with self.lock:
            if self.running or not self.pending:
                return
            self.running = True
            self.results = []      # a run counts from the press that began it
            self.revision += 1
            self.lock.notify_all()
        self.publish()

    def stop(self):
        """The pair to start: the file being worked on is abandoned and goes
        back to the head of the line, and everything behind it keeps its place,
        so start picks the same order back up."""
        with self.lock:
            self.running = False
            self.cancelled.set()
            self.revision += 1
        self.kill(self.process)
        self.publish()

    def move(self, item_id, index):
        with self.lock:
            at = next((n for n, item in enumerate(self.pending)
                       if item["id"] == item_id), None)
            if at is not None:
                item = self.pending.pop(at)
                self.pending.insert(max(0, min(index, len(self.pending))), item)
            # Bumped even when the file has since started, so a move that came
            # too late is answered with the order that really holds.
            self.revision += 1
        self.publish()

    def remove(self, item_id):
        with self.lock:
            self.pending = [item for item in self.pending if item["id"] != item_id]
            self.revision += 1
        self.publish()

    def snapshot(self):
        """The whole line, in order, as the page draws it."""
        with self.lock:
            items = [card(self.active, "running")] if self.active else []
            items += [card(item, "queued") for item in self.pending]
            return {"kind": "queue", "revision": self.revision,
                    "running": self.running, "items": items}

    def publish(self):
        self.push(**self.snapshot())

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
            with self.lock:
                while not (self.running and self.pending):
                    self.lock.wait()
                item = self.pending.pop(0)
                self.active = item
                self.current = item["name"]
                self.cancelled.clear()
                self.revision += 1
            self.push(kind="busy", value=True)
            self.publish()
            try:
                self.run(item)
                failure = None
            except Exception as error:  # one bad file must not stop the rest
                failure = str(error)
            # A stop reaches the job as a failure; anything else really did fail.
            stopped = failure is not None and self.cancelled.is_set()
            if failure and not stopped:
                self.push(kind="log", text=f"{item['name']}: {failure}")
            with self.lock:
                self.active = None
                if stopped:
                    self.pending.insert(0, item)   # unfinished, so still waiting
                else:
                    self.results.append((item["name"], failure))
                idle = not (self.running and self.pending)
                if idle:
                    self.running = False
                    report = self.summary(stopped, len(self.pending))
                self.revision += 1
            self.publish()
            if idle:
                self.push(kind="busy", value=False)
                self.push(kind="summary", text=report[0], failed=report[1])

    def run(self, item):
        options = item["options"]
        destination = output_for(item["source"], options["mode"], options["factor"],
                                 options["suffix"])
        self.announce("starting")
        self.interpolate(item["source"], destination, options)
        self.push(kind="log", text=f"done -> {destination}")

    def interpolate(self, source, destination, options):
        if self.cancelled.is_set():  # stopped before the process was started
            raise RuntimeError("stopped")
        command = worker_command(str(source), "-o", str(destination),
                                 "--mode", options["mode"],
                                 "--factor", str(options["factor"]),
                                 "--device", options["device"],
                                 "--slowmo-audio", options["audio"])
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            cwd=str(SILKFRAME.parent), env=environment, creationflags=NO_WINDOW,
        )
        if self.cancelled.is_set():  # stop arrived before the process existed to kill
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
                raise RuntimeError("stopped")
            raise RuntimeError(f"silkframe exited with code {code}")

    def summary(self, stopped, left):
        """What to leave on screen once the worker has nothing left to do."""
        total = len(self.results)
        failed = [name for name, error in self.results if error]
        finished = total - len(failed)
        videos = "video" if total == 1 else "videos"
        if stopped:  # the file that was stopped is waiting again, not lost
            waiting = f", {left} still queued" if left else ""
            return f"Stopped - {finished} finished{waiting}", False
        if failed:
            named = ", ".join(failed[:2])
            if len(failed) > 2:
                named += f" and {len(failed) - 2} more"
            return (f"Done - {total} {videos}: {finished} finished, "
                    f"{len(failed)} failed ({named})"), True
        return f"Done - {total} {videos} finished", False

    def announce(self, action):
        """The totals are read fresh, so files added mid run are counted."""
        with self.lock:
            position = len(self.results) + 1
            total = position + len(self.pending)
        self.push(kind="status",
                  text=f"{self.current}  ({position} of {total})  -  {action}")

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
        f"SilkFrame {__version__} - double the frames",
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
