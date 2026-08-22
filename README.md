# SilkFrame

Doubles the frames of any video by synthesising a new frame between every pair
of existing ones, using RIFE v4.25 — the model the Practical-RIFE authors
recommend as the default for most footage.

```
python gui.py                                  # drag and drop window
python silkframe.py clip.mp4                   # 30 fps -> 60 fps, same length, audio kept
python silkframe.py clip.mp4 --mode slowmo     # 30 fps, half speed, twice as long
```

Everything ffmpeg can decode goes in, the weights download themselves on first
run, and memory use stays flat no matter how long the video is.

## Install

```
pip install torch --index-url https://download.pytorch.org/whl/cu128   # or the cpu build
pip install -r requirements.txt
```

ffmpeg and ffprobe must be on PATH (5.1 or newer). A CUDA GPU is optional; on
the test machine it ran 720p at 83 ms per frame against 1486 ms on the CPU. The
first run downloads a 23 MB checkpoint to `~/.cache/silkframe` (override with
`SILKFRAME_CACHE`).

Check the install end to end with `python selftest.py`, which builds small clips
and asserts frame counts, frame rates, audio, rotation, odd sizes, cut handling
and cpu/gpu agreement.

## The window

`python gui.py` (or `pythonw gui.py` for no console) opens a drop target. Drop
one or more videos and each is written beside its source, as `name.2x.mp4` or
`name.slowmo.mp4`.

The interface itself is plain HTML, CSS and JavaScript in `web/`, rendered by
pywebview in a native window — Edge WebView2 on Windows. `web/app.js` owns only
the pixels: it calls `window.pywebview.api.*` for everything it wants done, and
`gui.py` pushes each status, progress and log line back with one `app.push()`
per message. The drop listener is registered from Python rather than JavaScript,
because a page is never told the real path of a file dropped on it and the
whole point here is to process the original in place, not a copy of it.

Batches run one at a time in the order they arrived, and the status line tracks
where you are: `birthday.mov  (2 of 4)  -  145/193 frames  11.2 fps  75%  eta
00:04`. More files can be dropped while a job is running — they join the queue
and the total goes up. Each file carries the mode and drive that were set when
it was dropped, so a batch can mix the two modes. A file that fails is logged
and the rest carry on. Cancel abandons the whole batch, not just the file in
flight, and anything the tool reports — variable frame rate, low video memory,
an odd frame size — appears in the log at the bottom.

When the queue empties the status line stays on the result rather than resetting:
`Done - 4 videos: 3 finished, 1 failed (drone shot.mp4)` or `Done - 4 videos
finished`, marked by a hollow or filled dot, and a Close button appears beside
Cancel. Dropping more files hides it again and starts a fresh count.

**Copy to a fast drive while processing** copies the source to a working folder
on the drive you pick, runs there, moves the result back next to the original
and then deletes the working folder. If the source already lives on that drive
the copy is skipped. The working folder is the system temp folder when you pick
the drive it lives on, and `<drive>\silkframe-temp\<random>` otherwise; either way
it is removed afterwards, including after a cancel or a crash mid-run.

It is worth knowing that this rarely speeds anything up. Interpolation is
GPU-bound — 720p reads about 6 MB/s, which any disk can serve — so staging pays
off only when the source sits on something genuinely slow, like a network share
or a USB drive. It is off by default.

1. ffmpeg decodes to raw RGB down a pipe, on a reader thread, so decoding
   overlaps with inference. Rotated phone video is handled: the frames arrive
   upright and the dimensions are reported that way.
2. Each frame is uploaded to the GPU **once**, padded up to the multiple of 64
   the network needs, and reused as both the right-hand and left-hand frame of
   consecutive pairs.
3. Two cheap tests decide whether the network runs at all:
   - **repeated frames** (mean pixel difference under `--still-threshold`):
     duplicated frames stay duplicated instead of being blurred together, and
     the pair costs nothing.
   - **shot changes** (luma-histogram distance over `--scene-threshold`): across
     a cut there is no motion to interpolate, so the previous frame is repeated
     rather than morphed into the next scene. On a test reel with known cuts,
     cuts scored 0.42–0.62 and the busiest within-shot pair scored 0.27, so the
     0.35 default sits in the gap.
4. Otherwise RIFE estimates bidirectional flow in five coarse-to-fine steps and
   blends the two warped frames with a learned mask.
5. The original frames are passed through to the encoder byte for byte — only
   the new frames are ever re-rendered — and the audio track is copied, not
   re-encoded.

Precision is chosen by measurement, not by assumption: at startup both fp16 and
fp32 are timed on the actual model, because fp16 is a large win on GPUs with
tensor cores and a large *loss* on those without them. On the GTX 1660 this
tested at 88 ms/frame in fp32 against 148 ms in fp16, so fp32 wins.

## Building something to hand over

`build.bat` packages the whole thing with PyInstaller into `dist\SilkFrame\`, then
zips it. Whoever you give it to unzips it and runs `SilkFrame.exe` — no Python, no
ffmpeg, no model download, because all three are inside. Rebuild after any
change by running it again; `build.bat nozip` skips the slow zip step while you
are iterating.

Expect the package to be large. Nearly all of it is the CUDA half of PyTorch.
A build made from a CPU-only environment is a fraction of the size and still
runs anywhere, just slowly:

```
py -m venv .venv-cpu
.venv-cpu\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv-cpu\Scripts\pip install -r requirements.txt pyinstaller
.venv-cpu\Scripts\python build.py
```

A packaged build runs videos by re-launching its own exe once per file, which is
why cancelling still works there. Windows will show a SmartScreen warning for an
unsigned executable — More info, then Run anyway.

## Quality

`benchmark.py` hides the middle frame of consecutive triplets, rebuilds it from
its two neighbours and compares it with the frame that was really there. Mean
over 25 triplets per clip, 720p:

| method | Sintel (fast, hard) | Big Buck Bunny | Jellyfish |
| --- | --- | --- | --- |
| repeat previous frame | 20.58 dB / 0.875 | 32.43 dB / 0.912 | 26.92 dB / 0.889 |
| cross-fade both frames | 22.97 dB / 0.889 | 36.76 dB / 0.954 | 31.30 dB / 0.927 |
| **RIFE 4.25** (default) | **26.50 dB / 0.923** | **37.14 dB / 0.972** | **37.50 dB / 0.982** |
| RIFE 4.26 | 26.53 dB / 0.923 | 37.11 dB / 0.972 | 37.52 dB / 0.982 |
| RIFE 4.25.heavy | 25.83 dB / 0.919 | 37.70 dB / 0.976 | 37.58 dB / 0.982 |
| RIFE 4.26.heavy | 26.58 dB / 0.925 | 36.99 dB / 0.972 | 37.67 dB / 0.983 |
| RIFE 4.25.lite | 26.10 dB / 0.919 | 36.63 dB / 0.969 | 37.32 dB / 0.982 |

Reproduce with `python benchmark.py your.mp4 --models all`.

The five checkpoints are within a few hundredths of a dB of each other on
average — the choice between them matters far less than the 3–6 dB they all
gain over a cross-fade. 4.25 is the default because it is the upstream
recommendation and the quickest of the full-size models; `4.26.heavy` won two of
the three clips, for about 50% more time per frame. `4.25.lite` placed last on
every clip without being meaningfully faster on this GPU, so it is only worth a
look if a smaller model helps elsewhere.

## Speed and memory

GTX 1660 (6 GB, no tensor cores), 720p, model time per generated frame:

| model | ms/frame | peak GPU memory at 1080p |
| --- | --- | --- |
| 4.25 | 88 | 1.11 GiB |
| 4.26 | 95 | 1.11 GiB |
| 4.25.lite | 86 | 1.17 GiB |
| 4.25.heavy | 120 | 1.18 GiB |
| 4.26.heavy | 136 | 2.01 GiB |

End to end, including decode and an x264 `--preset slow` encode, a 10 s 720p
clip (300 frames in, 599 out) takes 28 s. A modern GPU will be several times
faster than these numbers.

4K needs 4.4 GiB for a frame pair, which does not fit alongside a desktop on a
6 GB card; the driver then pages GPU memory to system RAM and throughput
collapses (0.1 fps here). `--scale 0.5` halves the resolution the *flow* is
estimated at — output stays full resolution — and brought the same 4K clip back
to 1.8 fps. The tool measures this at startup and warns before you wait.

## Options

| flag | default | notes |
| --- | --- | --- |
| `--mode fps\|slowmo` | `fps` | `fps` doubles the rate and keeps audio; `slowmo` halves the speed and drops it |
| `--model` | `4.25` | `4.26`, `4.25.heavy`, `4.26.heavy`, `4.25.lite` |
| `--scale` | `1.0` | `0.5` for 4K or when memory is tight, `2.0` for small frames or very slow motion |
| `--device` | auto | `cuda`, `cuda:1`, `cpu` |
| `--fp32` | auto | force full precision instead of the timed choice |
| `--encoder`, `--crf`, `--preset`, `--pix-fmt` | `libx264`, `17`, `slow`, `yuv420p` | `--encoder h264_nvenc` for the GPU encoder |
| `--no-audio` | off | drop the audio track |
| `--scene-threshold` | `0.35` | `0` interpolates across cuts too |
| `--still-threshold` | `0.001` | `0` interpolates repeated frames too |

## Limits worth knowing

- **2x only.** RIFE takes an arbitrary timestep, so 4x or 60→144 fps is a small
  change to the loop, but nothing here does it today.
- **Variable frame rate in, constant out.** The output rate is twice the
  source's *average* rate — frames over duration — so the running time and the
  audio sync are preserved. Phone and screen recordings are usually VFR, and
  their `r_frame_rate` is only the timing grid (a 24 fps clip often reports 60);
  building the output from that would play the video fast. Every frame is kept
  and none are duplicated, but the small timing wobble of a VFR source is
  evened out. A note is printed whenever this applies.
- **8-bit.** Frames travel as RGB24, so 10-bit and HDR sources are reduced to
  8 bits per channel. The colour primaries, transfer curve and range are copied
  to the encoder, so nothing shifts hue, but the bit depth is lost.
- **Odd frame sizes** cannot be stored in yuv420p, so those clips are encoded as
  yuv444p instead and a note is printed.
- **PSNR is not perception.** The table above ranks the models on a metric the
  RIFE authors themselves warn against reading too closely; for large motion or
  heavy occlusion, research models such as FILM or GMFSS can still look better,
  at ten to fifty times the cost per frame.

## Credits

RIFE is by Huang et al. (ECCV 2022), and the checkpoints are the official
[Practical-RIFE](https://github.com/hzwer/Practical-RIFE) releases, downloaded
from the mirror published by [vs-rife](https://github.com/HolyWu/vs-rife). The
network here is an inference-only port of their `IFNet_HDv3`; the weights are
loaded unmodified.
