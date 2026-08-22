#!/usr/bin/env python3
"""Double the frames of any video with RIFE.

    python silkframe.py clip.mp4                  # 30 fps -> 60 fps, same length
    python silkframe.py clip.mp4 --mode slowmo    # 30 fps, half speed
"""

import argparse
import queue
import sys
import threading
import time
from pathlib import Path

import torch

import video
from rife.model import DEFAULT_MODEL, MODELS, Interpolator


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Interpolate one new frame between every pair of frames (2x).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", help="source video")
    parser.add_argument("-o", "--output", help="destination video (default: <input>.2x.mp4)")
    parser.add_argument("--mode", choices=("fps", "slowmo"), default="fps",
                        help="fps: double the frame rate, keep the length and the audio. "
                             "slowmo: keep the frame rate, half speed, no audio")
    parser.add_argument("--model", choices=list(MODELS), default=DEFAULT_MODEL,
                        help="RIFE weights; .heavy is slower and sharper, .lite is faster")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="flow resolution; use 0.5 for 4K, 2.0 for tiny or very slow motion")
    parser.add_argument("--device", default=None, help="cuda, cuda:1, cpu (default: cuda if available)")
    parser.add_argument("--fp32", action="store_true", help="full precision (slower, rarely needed)")
    parser.add_argument("--encoder", default="libx264", help="output video codec")
    parser.add_argument("--crf", type=int, default=17, help="quality, lower is better")
    parser.add_argument("--preset", default="slow", help="encoder speed preset")
    parser.add_argument("--pix-fmt", default="yuv420p", help="output pixel format")
    parser.add_argument("--no-audio", action="store_true", help="drop the audio track")
    parser.add_argument("--scene-threshold", type=float, default=0.35,
                        help="repeat a frame instead of interpolating across a cut; 0 disables")
    parser.add_argument("--still-threshold", type=float, default=0.001,
                        help="repeat a frame instead of interpolating when nothing moved")
    return parser.parse_args(argv)


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


def main(argv=None):
    args = parse_args(argv)
    source = Path(args.input)
    output = Path(args.output) if args.output else source.with_suffix(f".2x{source.suffix or '.mp4'}")
    if output.resolve() == source.resolve():
        raise SystemExit("refusing to overwrite the input; pass -o")

    info = video.VideoInfo(source)
    print(f"input  {source}: {info}", file=sys.stderr)
    if info.variable:
        print(f"note: variable frame rate source ({float(info.nominal_fps):.3f} fps timing grid); "
              f"using the {float(info.fps):.3f} fps average, so the running time is kept "
              f"and the output is constant rate", file=sys.stderr)

    interpolator = Interpolator(args.model, device=args.device,
                                fp16=False if args.fp32 else None, scale=args.scale)
    interpolator.prepare(info.height, info.width)
    print(f"model  RIFE {args.model} on {interpolator.device} "
          f"({'fp16' if interpolator.dtype.itemsize == 2 else 'fp32'}), scale {args.scale}", file=sys.stderr)

    needed, free = interpolator.probe_memory()
    if needed > free * 0.8:
        print(f"warning: a frame pair needs {needed / 2**30:.1f} GiB but only {free / 2**30:.1f} GiB "
              f"is free on the gpu. Expect the driver to spill to system memory and run many times "
              f"slower - rerun with --scale 0.5, or with --device cpu.", file=sys.stderr)

    out_fps = info.fps * 2 if args.mode == "fps" else info.fps
    keep_audio = info.has_audio and args.mode == "fps" and not args.no_audio
    writer = video.open_writer(output, info, out_fps, encoder=args.encoder, crf=args.crf,
                              preset=args.preset, pix_fmt=args.pix_fmt, audio=keep_audio)

    frames = prefetch(video.read_frames(info))
    progress = Progress(info.frames)
    read = written = cuts = stills = 0
    try:
        previous = next(frames, None)
        if previous is None:
            raise SystemExit("no frames decoded")
        video.write_frame(writer, previous)
        read = written = 1
        previous_tensor = interpolator.upload(previous)
        previous_histogram = interpolator.histogram(previous_tensor)

        for current in frames:
            current_tensor = interpolator.upload(current)
            current_histogram = interpolator.histogram(current_tensor)
            moved = Interpolator.motion(previous_tensor, current_tensor)
            changed = Interpolator.shot_change(previous_histogram, current_histogram)

            if moved < args.still_threshold:
                middle, stills = previous, stills + 1
            elif args.scene_threshold and changed > args.scene_threshold:
                middle, cuts = previous, cuts + 1
            else:
                middle = interpolator.to_frame(interpolator.middle(previous_tensor, current_tensor))

            video.write_frame(writer, middle)
            video.write_frame(writer, current)
            previous, previous_tensor, previous_histogram = current, current_tensor, current_histogram
            read += 1
            written += 2
            progress.update(read)
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
    print(f"output {output}: {written} frames @ {float(out_fps):.3f} fps "
          f"({'audio copied' if keep_audio else 'no audio'})", file=sys.stderr)
    print(f"done   {read} -> {written} frames in {elapsed:.1f}s "
          f"({read / max(elapsed, 1e-6):.1f} fps in), {cuts} cuts and {stills} still pairs repeated",
          file=sys.stderr)


if __name__ == "__main__":
    main()
