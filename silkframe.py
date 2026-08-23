#!/usr/bin/env python3
"""Multiply the frames of any video with RIFE.

    python silkframe.py clip.mp4                  # 30 fps -> 60 fps, same length
    python silkframe.py clip.mp4 --factor 4       # 30 fps -> 120 fps
    python silkframe.py clip.mp4 --target-fps 60  # 23.976 fps -> exactly 60
    python silkframe.py clip.mp4 --mode slowmo    # 30 fps, half speed
"""

import argparse
import queue
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path

import torch

import video
from version import __version__
from rife.model import DEFAULT_MODEL, MODELS, Interpolator, available_devices


def timecode(text):
    """Seconds, or MM:SS, or HH:MM:SS.sss."""
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Synthesise new frames between the frames of a video.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"SilkFrame {__version__}")
    parser.add_argument("input", nargs="?", help="source video")
    parser.add_argument("-o", "--output", help="destination video (default: <input>.2x.mp4, "
                                               "named after whatever rate you asked for)")
    parser.add_argument("--mode", choices=("fps", "slowmo"), default="fps",
                        help="fps: raise the frame rate, keep the length and the audio. "
                             "slowmo: keep the frame rate, stretch the running time, and do "
                             "whatever --slowmo-audio says with the sound")
    parser.add_argument("--slowmo-audio", choices=("mute", "keep-pitch", "drop-pitch"),
                        default="mute",
                        help="what becomes of the sound when the clip is slowed: mute drops it, "
                             "keep-pitch stretches it to the new length at its own pitch, "
                             "drop-pitch slows the waveform itself so the pitch falls with it. "
                             "Either kept track is re-encoded, and neither sounds like the "
                             "original - the further it is slowed, the rougher it gets. "
                             "Ignored in fps mode, where the audio is copied as it is")
    rate = parser.add_mutually_exclusive_group()
    rate.add_argument("-f", "--factor", type=int, default=2,
                      help="frames out per frame in: 2 doubles, 3 triples, 4 quadruples")
    rate.add_argument("--target-fps", type=Fraction, default=None,
                      help="an exact output rate instead of a whole multiple, e.g. 60, 59.94 "
                           "or 60000/1001; fps mode only")
    parser.add_argument("--model", choices=list(MODELS), default=DEFAULT_MODEL,
                        help="RIFE weights; .heavy is slower and sharper, .lite is faster")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="flow resolution; use 0.5 for 4K, 2.0 for tiny or very slow motion")
    parser.add_argument("--device", default="auto",
                        help="auto (the gpu with the most free memory), cuda, cuda:1, mps, cpu")
    parser.add_argument("--list-devices", action="store_true",
                        help="print what torch can run on here, then exit")
    parser.add_argument("--threads", type=int, default=0,
                        help="torch cpu threads; 0 leaves the choice to torch")
    parser.add_argument("--fp32", action="store_true", help="full precision (slower, rarely needed)")
    parser.add_argument("--start", type=timecode, help="skip this far in, in seconds or HH:MM:SS")
    parser.add_argument("--duration", type=timecode, help="stop after this much of the source")
    parser.add_argument("--encoder", default="libx264", help="output video codec")
    parser.add_argument("--crf", type=int, default=17, help="quality, lower is better")
    parser.add_argument("--preset", default="slow", help="encoder speed preset")
    parser.add_argument("--pix-fmt", default="yuv420p", help="output pixel format")
    parser.add_argument("--no-audio", action="store_true", help="drop the audio track")
    parser.add_argument("--no-overwrite", action="store_true",
                        help="stop rather than replace an output that already exists")
    parser.add_argument("--scene-threshold", type=float, default=0.35,
                        help="repeat a frame instead of interpolating across a cut; 0 disables")
    parser.add_argument("--still-threshold", type=float, default=0.001,
                        help="repeat a frame instead of interpolating when nothing moved")
    args = parser.parse_args(argv)

    if not args.list_devices and not args.input:
        parser.error("the input video is required")
    if args.factor < 2:
        parser.error("--factor has to be 2 or more")
    if args.start is not None and args.start < 0:
        parser.error("--start cannot be negative")
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration has to be positive")
    if args.target_fps is not None:
        if args.target_fps <= 0:
            parser.error("--target-fps has to be positive")
        if args.mode == "slowmo":
            parser.error("--target-fps sets an output rate, which slowmo mode does not change; "
                         "use --factor to choose how much slower it runs")
    return args


def prefetch(iterable, depth=6):
    """Drain a generator on a worker thread so decoding overlaps with inference."""
    items = queue.Queue(depth)
    done = object()

    def worker():
        try:
            for item in iterable:
                items.put(item)
        except BaseException as error:  # noqa: BLE001 - re-raised on the consumer side
            items.put(error)
        items.put(done)

    threading.Thread(target=worker, daemon=True).start()
    while True:
        item = items.get()
        if item is done:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


class Progress:
    def __init__(self, total):
        self.total = total
        self.start = time.monotonic()
        self.last = 0.0

    def update(self, done, force=False):
        now = time.monotonic()
        if not force and now - self.last < 0.5:
            return
        self.last = now
        rate = done / max(now - self.start, 1e-6)
        line = f"\r  {done}/{self.total or '?'} frames  {rate:5.1f} fps"
        if self.total:
            remaining = (self.total - done) / max(rate, 1e-6)
            line += f"  {done / self.total:4.0%}  eta {int(remaining) // 60:02d}:{int(remaining) % 60:02d}"
        print(line.ljust(70), end="", file=sys.stderr, flush=True)


def timeline(fps, args):
    """(step, output rate): how far apart output frames sit on the source's timeline.

    A step of 1/4 puts three new frames between every pair of real ones.  An
    exact target rate lands wherever the two rates meet instead, which is what
    lets 23.976 become 60 without dropping or repeating anything.
    """
    if args.target_fps:
        return Fraction(fps) / args.target_fps, args.target_fps
    out_fps = fps if args.mode == "slowmo" else fps * args.factor
    return Fraction(1, args.factor), out_fps


def span(info, args):
    """(frame rate, frame count) for the stretch that will actually be decoded.

    A constant rate source runs at the same rate everywhere, so its own average
    and a count off the clock will do.  A variable rate one has to be measured
    over the span itself, or the span is written at a rate it never ran at.
    """
    if not (args.start or args.duration):
        return info.fps, info.frames
    if info.variable:
        rate, frames = video.span_rate(info, args.start, args.duration)
        if rate:
            return rate, frames
    seconds = float(info.duration or 0) - (args.start or 0.0)
    if args.duration:
        seconds = min(seconds, args.duration)
    return info.fps, max(int(seconds * float(info.fps)), 0)


def main(argv=None):
    args = parse_args(argv)
    if args.list_devices:
        for spec, label, _ in available_devices():
            print(f"{spec}\t{label}")
        return
    if args.threads:
        torch.set_num_threads(args.threads)

    source = Path(args.input)
    named = f"{float(args.target_fps):g}fps" if args.target_fps else f"{args.factor}x"
    output = (Path(args.output) if args.output
              else source.with_suffix(f".{named}{source.suffix or '.mp4'}"))
    if output.resolve() == source.resolve():
        raise SystemExit("refusing to overwrite the input; pass -o")
    if args.no_overwrite and output.exists():
        raise SystemExit(f"{output} already exists; delete it or drop --no-overwrite")

    info = video.VideoInfo(source)
    print(f"input  {source}: {info}", file=sys.stderr)
    if info.variable:
        print(f"note: variable frame rate source ({float(info.nominal_fps):.3f} fps timing grid); "
              f"using the {float(info.fps):.3f} fps average, so the running time is kept "
              f"and the output is constant rate", file=sys.stderr)

    fps, total = span(info, args)
    if fps != info.fps:
        print(f"note: the requested span runs at {float(fps):.3f} fps, not the "
              f"{float(info.fps):.3f} fps file average; it is written at the span's rate",
              file=sys.stderr)

    step, out_fps = timeline(fps, args)
    if step > 1:
        print(f"note: {float(out_fps):.3f} fps is below the source rate, so frames are dropped "
              f"rather than added", file=sys.stderr)

    interpolator = Interpolator(args.model, device=args.device,
                                fp16=False if args.fp32 else None, scale=args.scale)
    interpolator.prepare(info.height, info.width)
    print(f"model  RIFE {args.model} on {interpolator.device} "
          f"({'fp16' if interpolator.dtype.itemsize == 2 else 'fp32'}), scale {args.scale}", file=sys.stderr)
    print(f"plan   {float(fps):.3f} -> {float(out_fps):.3f} fps"
          f"{f', {args.factor}x longer' if args.mode == 'slowmo' else ''}", file=sys.stderr)

    needed, free = interpolator.probe_memory()
    if needed > free * 0.8:
        print(f"warning: a frame pair needs {needed / 2**30:.1f} GiB but only {free / 2**30:.1f} GiB "
              f"is free on the gpu. Expect the driver to spill to system memory and run many times "
              f"slower - rerun with --scale 0.5, or with --device cpu.", file=sys.stderr)

    stretching = args.mode == "slowmo" and args.slowmo_audio != "mute"
    keep_audio = info.has_audio and not args.no_audio and (args.mode == "fps" or stretching)
    audio_filter = (video.stretch_audio(args.slowmo_audio, args.factor, info.audio_rate)
                    if keep_audio and stretching else None)
    writer = video.open_writer(output, info, out_fps, encoder=args.encoder, crf=args.crf,
                              preset=args.preset, pix_fmt=args.pix_fmt, audio=keep_audio,
                              audio_filter=audio_filter, start=args.start, duration=args.duration)

    frames = prefetch(video.read_frames(info, args.start, args.duration))
    progress = Progress(total)
    read = written = cuts = stills = 0
    try:
        previous = next(frames, None)
        if previous is None:
            raise SystemExit("no frames decoded")
        previous_tensor = interpolator.upload(previous)
        previous_histogram = interpolator.histogram(previous_tensor)
        read = 1
        index = 0               # the source frame `previous` was decoded from
        position = Fraction(0)  # where the next output frame sits, in source frames

        for current in frames:
            current_tensor = interpolator.upload(current)
            current_histogram = interpolator.histogram(current_tensor)
            moved = Interpolator.motion(previous_tensor, current_tensor)
            changed = Interpolator.shot_change(previous_histogram, current_histogram)

            held = None  # a pair the network must not be asked about; repeat instead
            if moved < args.still_threshold:
                held, stills = previous, stills + 1
            elif args.scene_threshold and changed > args.scene_threshold:
                held, cuts = previous, cuts + 1

            while position < index + 1:
                t = position - index
                if t == 0:
                    frame = previous  # an original frame, passed through byte for byte
                elif held is not None:
                    frame = held
                else:
                    frame = interpolator.to_frame(
                        interpolator.at(previous_tensor, current_tensor, t))
                video.write_frame(writer, frame)
                written += 1
                position += step

            previous, previous_tensor, previous_histogram = current, current_tensor, current_histogram
            index += 1
            read += 1
            progress.update(read)

        # The last frame has nothing to pair with, so it can only be repeated.
        # Its slot is `position`, which sits `position - index` source frames
        # past the end: worth holding for less than one source frame, but not
        # worth stretching the clip past its own audio when a target rate below
        # the source rate leaves the grid a whole frame short.
        if position - index < 1:
            video.write_frame(writer, previous)
            written += 1
        progress.update(read, force=True)
    except KeyboardInterrupt:
        writer.kill()
        raise SystemExit("\ninterrupted")
    except torch.cuda.OutOfMemoryError:
        writer.kill()
        raise SystemExit(f"\nout of gpu memory at {info.width}x{info.height}; "
                         f"rerun with --scale 0.5 or --device cpu")
    finally:
        print("", file=sys.stderr)

    video.close_writer(writer)
    elapsed = time.monotonic() - progress.start
    if not keep_audio:
        sound = "no audio"
    elif not audio_filter:
        sound = "audio copied"
    else:
        sound = "audio stretched" if args.slowmo_audio == "keep-pitch" else "audio slowed"
    print(f"output {output}: {written} frames @ {float(out_fps):.3f} fps ({sound})",
          file=sys.stderr)
    print(f"done   {read} -> {written} frames in {elapsed:.1f}s "
          f"({read / max(elapsed, 1e-6):.1f} fps in), {cuts} cuts and {stills} still pairs repeated",
          file=sys.stderr)


if __name__ == "__main__":
    main()
