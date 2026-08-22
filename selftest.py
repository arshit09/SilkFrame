#!/usr/bin/env python3
"""End-to-end checks on generated clips: python selftest.py"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import torch

import video
from rife.model import Interpolator

PYTHON = sys.executable
FAST = ["--preset", "ultrafast", "--crf", "20"]
failures = []


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *map(str, args)], check=True)


def run(*args):
    result = subprocess.run([PYTHON, "silkframe.py", *map(str, args), *FAST],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip()[-800:])
    return result.stderr


def check(name, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  <- ' + detail}")
    if not condition:
        failures.append(name)


def main():
    work = Path(tempfile.mkdtemp(prefix="silkframe-selftest-"))
    print(f"working in {work}")

    # 1. frame rate doubling, audio, frame count
    source = work / "moving.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x96:rate=10:duration=2",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", source)
    out = work / "moving.2x.mp4"
    run(source, "-o", out)
    info = video.VideoInfo(out)
    check("fps mode doubles the frame rate", info.fps == 20, str(info.fps))
    check("fps mode keeps 2N-1 frames", info.frames == 39, str(info.frames))
    check("fps mode keeps the audio", info.has_audio)
    check("size is unchanged", (info.width, info.height) == (160, 96))

    # 2. the new frames are genuinely new, not copies of a neighbour
    frames = list(video.read_frames(info))
    middles = [np.abs(frames[i].astype(int) - frames[i - 1].astype(int)).mean() for i in (1, 3, 5)]
    check("interpolated frames differ from their neighbours", min(middles) > 0.5, str(middles))

    # 3. slow motion keeps the rate and drops the audio
    out = work / "moving.slow.mp4"
    run(source, "-o", out, "--mode", "slowmo")
    info = video.VideoInfo(out)
    check("slowmo keeps the frame rate", info.fps == 10, str(info.fps))
    check("slowmo doubles the duration", abs(float(info.duration) - 3.9) < 0.1, info.duration)
    check("slowmo drops the audio", not info.has_audio)

    # 4. odd sizes: neither the padding nor the encoder may change them
    source = work / "odd.mkv"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=1",
           "-vf", "scale=161:97", "-c:v", "ffv1", "-pix_fmt", "yuv444p", source)
    out = work / "odd.2x.mkv"
    run(source, "-o", out)
    info = video.VideoInfo(out)
    check("odd sizes survive padding", (info.width, info.height) == (161, 97),
          f"{info.width}x{info.height}")

    # 5. rotated video: ffmpeg hands us the frames already upright
    source = work / "portrait.mp4"
    ffmpeg("-display_rotation", "90", "-i", work / "moving.mp4", "-c", "copy", "-map", "0:v", source)
    out = work / "portrait.2x.mp4"
    run(source, "-o", out)
    info = video.VideoInfo(out)
    check("rotated input keeps its display size", (info.width, info.height) == (96, 160),
          f"{info.width}x{info.height}")

    # 6. repeated frames are detected instead of interpolated
    source = work / "duped.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x96:rate=5:duration=2",
           "-r", "10", "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv420p", source)
    report = run(source, "-o", work / "duped.2x.mp4")
    stills = int(report.rsplit("and", 1)[1].split()[0])
    check("repeated frames are not interpolated", stills >= 8, f"{stills} of 19 pairs")

    # 7. a cut is repeated rather than blended across
    source = work / "cut.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x96:rate=10:duration=1",
           "-f", "lavfi", "-i", "smptebars=size=160x96:rate=10:duration=1",
           "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", source)
    report = run(source, "-o", work / "cut.2x.mp4")
    check("the cut is detected", " 1 cuts " in report, report.strip().rsplit("\n", 1)[-1])

    # 8. a single frame in, a single frame out
    source = work / "one.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x96:rate=10:duration=1", "-frames:v", "1",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", source)
    run(source, "-o", work / "one.2x.mp4")
    check("a one frame video still works", video.VideoInfo(work / "one.2x.mp4").frames == 1)

    # 9. the cpu path produces the same picture as the gpu path
    if torch.cuda.is_available():
        pair = list(video.read_frames(video.VideoInfo(work / "moving.mp4")))[:2]
        guesses = [Interpolator(device=device, fp16=False).interpolate_frames(*pair)
                   for device in ("cuda", "cpu")]
        gap = np.abs(guesses[0].astype(int) - guesses[1].astype(int)).mean()
        check("cpu and gpu agree", gap < 0.5, f"mean difference {gap:.3f}")

    # 10. the window's staging round trip, if there is a second drive to stage on
    try:
        import gui
    except Exception as error:  # no pywebview
        print(f"  SKIP  gui checks ({error})")
    else:
        # No window: App only needs one to paint, and nothing here paints.
        app = gui.App()
        check("slowmo output is named apart from the fps one",
              gui.output_for(Path("a/clip.mp4"), "slowmo", 2, "").name == "clip.slowmo.mp4")
        check("the factor reaches the output name",
              gui.output_for(Path("a/clip.mp4"), "fps", 4, "").name == "clip.4x.mp4")
        check("a typed suffix replaces the automatic one",
              gui.output_for(Path("a/clip.mp4"), "fps", 4, "final").name == "clip.final.mp4")
        check("a suffix that cannot be a name falls back",
              gui.output_for(Path("a/clip.mp4"), "fps", 2, " ?? ").name == "clip.2x.mp4")
        here = os.path.splitdrive(work)[0]
        other = next((d for d, free in gui.drives() if d != here and free > 5 * 2**30), None)
        if other:
            source = work / "staged.mp4"
            shutil.copy2(work / "moving.mp4", source)
            before = set(Path(other + os.sep).iterdir())
            app.run(source, {"mode": "fps", "factor": 2, "device": "auto",
                             "suffix": "", "drive": other})
            check("staging puts the result back beside the source",
                  (work / "staged.2x.mp4").exists())
            check("staging leaves nothing on the fast drive",
                  set(Path(other + os.sep).iterdir()) == before)
        else:
            print("  SKIP  staging round trip (no second drive with room)")

    # 11. a factor above two puts factor-1 new frames in every gap
    moving = work / "moving.mp4"
    out = work / "moving.4x.mp4"
    run(moving, "-o", out, "--factor", "4")
    info = video.VideoInfo(out)
    check("4x quadruples the frame rate", info.fps == 40, str(info.fps))
    check("4x keeps 4N-3 frames", info.frames == 77, str(info.frames))

    # 12. an exact output rate that the source rate does not divide into
    out = work / "moving.25fps.mp4"
    run(moving, "-o", out, "--target-fps", "25")
    info = video.VideoInfo(out)
    check("the target rate is exact", info.fps == 25, str(info.fps))
    check("the target rate spans the whole clip", info.frames == 49, str(info.frames))

    # 13. trimming decodes only the span that was asked for
    out = work / "moving.trim.mp4"
    run(moving, "-o", out, "--start", "0.5", "--duration", "1")
    info = video.VideoInfo(out)
    check("trim decodes only the requested span", 17 <= info.frames <= 21, str(info.frames))

    # 14. a target rate under the source rate decimates without outrunning the audio
    out = work / "moving.5fps.mp4"
    run(moving, "-o", out, "--target-fps", "5")
    info = video.VideoInfo(out)
    check("a lower target rate drops frames", info.frames == 10, str(info.frames))
    check("a lower target rate keeps the running time",
          abs(float(info.duration) - 2.0) < 0.05, str(info.duration))

    # 15. a trimmed span of a variable rate source is written at the span's own rate,
    #     which here is 30 fps against a 19.6 fps file average
    head, tail, listing = work / "head.mp4", work / "tail.mp4", work / "concat.txt"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x96:rate=30:duration=2",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", head)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x96:rate=2:duration=2",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", tail)
    listing.write_text("\n".join(f"file '{path.as_posix()}'" for path in (head, tail)))
    vfr = work / "vfr.mp4"
    ffmpeg("-f", "concat", "-safe", "0", "-i", listing, "-c", "copy",
           "-fps_mode", "passthrough", vfr)
    check("the test clip really is variable rate", video.VideoInfo(vfr).variable)
    out = work / "vfr.2x.mp4"
    run(vfr, "-o", out, "--duration", "2")
    info = video.VideoInfo(out)
    check("a trimmed span uses its own rate, not the file average",
          abs(float(info.fps) - 60) < 1, str(info.fps))
    check("a trimmed span keeps its running time",
          abs(float(info.duration) - 1.98) < 0.1, str(info.duration))

    # 16. packet timestamps still parse on a container that decorates them
    ts = work / "moving.ts"
    ffmpeg("-i", work / "moving.mp4", "-map", "0:v", "-c", "copy", ts)
    rate, counted = video.span_rate(video.VideoInfo(ts), 0.5, 1.0)
    check("mpeg-ts packet timestamps parse",
          rate is not None and abs(float(rate) - 10) < 0.5, f"{rate}, {counted} frames")

    # 17. a trim that asks for nothing is refused, not silently ignored
    refused = subprocess.run([PYTHON, "silkframe.py", str(moving), "--duration", "0"],
                             capture_output=True, text=True)
    check("--duration 0 is refused", refused.returncode != 0 and "positive" in refused.stderr,
          refused.stderr.strip()[-160:])

    print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' failed: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
