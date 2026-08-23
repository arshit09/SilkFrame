"""ffmpeg-backed video input and output.

Decoding and encoding are streamed through pipes, so memory use is a couple of
frames regardless of how long the video is, and every container/codec ffmpeg
understands is supported.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction

import numpy as np

def _tool(name):
    """A packaged build carries its own ffmpeg; a source checkout uses PATH.

    The packaged copies live in their own folder: windows searches an
    executable's own directory for dlls first, and the rest of the bundle holds
    files with system dll names.
    """
    packaged = getattr(sys, "_MEIPASS", None)
    if packaged:
        candidate = os.path.join(packaged, "ffmpeg", f"{name}.exe" if os.name == "nt" else name)
        if os.path.exists(candidate):
            return candidate
    return shutil.which(name)


FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")
# keeps console windows from flashing when a gui runs this under pythonw
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ffprobe stream key -> ffmpeg output option, so the colours survive the round trip.
COLOR_OPTIONS = {
    "color_primaries": "color_primaries",
    "color_trc": "color_trc",
    "color_space": "colorspace",
    "color_range": "color_range",
}


def require_ffmpeg():
    if not FFMPEG or not FFPROBE:
        raise RuntimeError("ffmpeg and ffprobe must be installed and on PATH")


class VideoInfo:
    def __init__(self, path):
        require_ffmpeg()
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, check=True, creationflags=NO_WINDOW,
        ).stdout
        probed = json.loads(out)
        streams = probed["streams"]
        videos = [s for s in streams if s["codec_type"] == "video"]
        # cover art is stored as a one frame video stream; never pick that one
        video = next((s for s in videos if not s.get("disposition", {}).get("attached_pic")),
                     videos[0] if videos else None)
        if video is None:
            raise RuntimeError(f"{path}: no video stream")
        self.duration = video.get("duration") or probed.get("format", {}).get("duration")

        self.path = str(path)
        self.stream = video["index"]
        self.width = int(video["width"])
        self.height = int(video["height"])
        # ffmpeg rotates frames on decode, so portrait videos come out transposed.
        if abs(self._rotation(video)) % 180 == 90:
            self.width, self.height = self.height, self.width

        # The average rate is frames over duration, which is what the output has to
        # be written at to keep the running time.  r_frame_rate is only the finest
        # timing grid the container uses: on variable frame rate video (phone and
        # screen capture) it is far higher, and playing at twice *that* would run
        # the video fast.
        average = self._rate(video.get("avg_frame_rate"))
        nominal = self._rate(video.get("r_frame_rate"))
        self.fps = average or nominal or Fraction(25)
        self.variable = bool(average and nominal and abs(nominal - average) > average / 100)
        self.nominal_fps = nominal
        self.frames = self._frame_count(video)
        sound = next((s for s in streams if s["codec_type"] == "audio"), None)
        self.has_audio = sound is not None
        # Wanted by asetrate, which slows a track by declaring a lower rate.
        self.audio_rate = int(sound.get("sample_rate") or 48000) if sound else 0
        # Carried over to the encoder so HDR and non-709 clips keep their meaning.
        # "gbr" is dropped: an RGB source has no matrix to hand to a YUV encoder.
        self.color = {
            key: video[key]
            for key in ("color_primaries", "color_trc", "color_space", "color_range")
            if video.get(key) and video[key] not in ("unknown", "gbr")
        }

    @staticmethod
    def _rate(value):
        try:
            return Fraction(value or 0)  # ffprobe writes 0/0 when it does not know
        except ZeroDivisionError:
            return Fraction(0)

    @staticmethod
    def _rotation(video):
        for side in video.get("side_data_list", []):
            if "rotation" in side:
                return int(float(side["rotation"]))
        return int(float(video.get("tags", {}).get("rotate", 0)))

    def _frame_count(self, video):
        for key in ("nb_frames", "nb_read_frames"):
            if str(video.get(key, "N/A")).isdigit():
                return int(video[key])
        try:
            return int(float(self.duration) * self.fps)
        except (TypeError, ValueError):
            return 0

    def __str__(self):
        return (f"{self.width}x{self.height} @ {float(self.fps):.3f} fps, "
                f"{self.frames or '?'} frames, audio: {'yes' if self.has_audio else 'no'}")


def _diagnostics(log):
    """ffmpeg's stderr, kept in a file so a chatty run can never fill a pipe."""
    log.seek(0)
    return log.read().decode("utf-8", "replace").strip() or "no output"


def _read_exact(stream, array):
    view = memoryview(array.reshape(-1).data)
    filled = 0
    while filled < len(view):
        chunk = stream.readinto(view[filled:])
        if not chunk:
            break
        filled += chunk
    return filled == len(view)


def trim_options(start, duration):
    """Input-side seek, shared by the decoder and the audio copy so they line up."""
    options = []
    if start:
        options += ["-ss", f"{start:.6f}"]
    if duration:
        options += ["-t", f"{duration:.6f}"]
    return options


def stretch_audio(kind, factor, rate):
    """The -af chain that makes a track `factor` times as long.

    keep-pitch cuts the sound into pieces and overlaps them to fill the extra
    time, which holds the pitch where it was and is why it echoes; atempo will
    not go below half speed in one pass, so deeper slowdowns are chained.
    drop-pitch resamples instead, the way a tape played slow: nothing is
    invented, and the pitch falls an octave for every doubling.
    """
    if kind == "drop-pitch":
        return f"asetrate={round(rate / factor)},aresample={rate}"
    steps = []
    tempo = Fraction(1, factor)
    while tempo < Fraction(1, 2):
        steps.append(Fraction(1, 2))
        tempo *= 2
    steps.append(tempo)
    return ",".join(f"atempo={float(step):g}" for step in steps)


def span_rate(info, start, duration):
    """(frame rate, frame count) over just the stretch that will be decoded.

    A variable frame rate source's file-wide average says nothing about the
    stretch that was asked for, so a trimmed span has to be measured or it is
    written at a rate it never ran at: wrong speed, drifting away from the
    audio copied beside it.  Every packet header is read rather than seeking to
    the span, because ``-read_intervals`` lands on the keyframe before it and
    would count frames the decoder goes on to throw away.  Nothing is decoded.
    Returns (None, count) when there is too little there to measure a rate.
    """
    listed = subprocess.run(
        # not csv: that appends a field for the side data mpeg-ts hangs on every
        # packet, and every timestamp would come back with a comma stuck to it
        [FFPROBE, "-v", "error", "-select_streams", str(info.stream),
         "-show_entries", "packet=pts_time", "-of", "default=nw=1:nk=1", info.path],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    ).stdout.split()
    stamps = []
    for value in listed:
        try:
            stamps.append(float(value))
        except ValueError:  # "N/A", or whatever else a container decorates it with
            continue
    if not stamps:
        return None, 0
    stamps.sort()  # a container may store its packets in decode order
    # -ss counts from where the stream starts, which is not 0 in every container:
    # mpeg-ts conventionally opens at 1.4 seconds.
    origin = stamps[0]
    first = start or 0.0
    beyond = first + duration if duration else float("inf")
    # exactly the frames -ss and -t leave for the decoder to hand over
    times = [stamp for stamp in stamps if first <= stamp - origin < beyond]
    if len(times) < 2 or times[-1] == times[0]:
        return None, len(times)
    seconds = Fraction(times[-1] - times[0]).limit_denominator(1000000)
    return Fraction(len(times) - 1) / seconds, len(times)


def read_frames(info, start=None, duration=None):
    """Yield decoded frames as writable HxWx3 uint8 RGB arrays."""
    command = [
        FFMPEG, "-v", "error", "-nostdin", *trim_options(start, duration), "-i", info.path,
        "-map", f"0:{info.stream}", "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    log = tempfile.TemporaryFile()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log,
                               bufsize=info.width * info.height * 3,
                               creationflags=NO_WINDOW)
    drained = False
    try:
        while True:
            frame = np.empty((info.height, info.width, 3), np.uint8)
            if not _read_exact(process.stdout, frame):
                drained = True
                break
            yield frame
    finally:
        if not drained:  # consumer stopped early, so the broken pipe is expected
            process.kill()
        process.stdout.close()
        code = process.wait()
        if drained and code != 0:
            raise RuntimeError(f"ffmpeg failed while decoding: {_diagnostics(log)}")
        log.close()


def open_writer(path, info, fps, encoder="libx264", crf=17, preset="slow",
                pix_fmt="yuv420p", audio=True, audio_filter=None, start=None, duration=None):
    """Start an ffmpeg process that accepts raw RGB frames on stdin."""
    if (info.width % 2 or info.height % 2) and not any(tag in pix_fmt for tag in ("444", "rgb", "gbr")):
        print(f"note: {info.width}x{info.height} has an odd side, "
              f"which {pix_fmt} cannot store; encoding as yuv444p", file=sys.stderr)
        pix_fmt = "yuv444p"
    command = [
        FFMPEG, "-y", "-v", "error", "-nostdin",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{info.width}x{info.height}", "-r", str(fps), "-i", "-",
    ]
    if audio:
        # Copied packets can only start on a packet boundary, so a trimmed audio
        # track begins within a frame or two of where the video does.
        command += [*trim_options(start, duration), "-i", info.path, "-map", "1:a:0"]
        # A track stretched to a new length has been rewritten, so it cannot be
        # copied; one that only rides along untouched is never re-encoded.
        if audio_filter:
            command += ["-af", audio_filter, "-c:a", "aac", "-b:a", "192k"]
        else:
            command += ["-c:a", "copy"]
    command += ["-map", "0:v:0", "-c:v", encoder, "-pix_fmt", pix_fmt]
    if "nvenc" in encoder or "qsv" in encoder or "amf" in encoder:
        command += ["-cq", str(crf), "-preset", preset]
    elif encoder.startswith(("libx26", "libsvt", "librav1e", "libaom")):
        command += ["-crf", str(crf), "-preset", preset]
    for key, value in info.color.items():
        command += ["-" + COLOR_OPTIONS[key], value]
    command.append(str(path))
    log = tempfile.TemporaryFile()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log,
                               creationflags=NO_WINDOW)
    process.log = log
    return process


def write_frame(process, frame):
    try:
        process.stdin.write(frame)
    except OSError:  # the encoder died; Windows reports EINVAL rather than a broken pipe
        raise RuntimeError(f"ffmpeg stopped while encoding: {_diagnostics(process.log)}") from None


def close_writer(process):
    process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError(f"ffmpeg failed while encoding: {_diagnostics(process.log)}")
    process.log.close()
