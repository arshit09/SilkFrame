#!/usr/bin/env python3
"""Measure interpolation quality against the frames the model never sees.

For triplets of consecutive frames the middle one is hidden, rebuilt from its
neighbours and compared with the original.  The two trivial strategies -
repeating a frame and cross-fading - are included so the numbers have a floor.

    python benchmark.py clip.mp4 --models 4.25,4.26,4.25.lite
"""

import argparse
import math
from collections import deque

import numpy as np
import torch
import torch.nn.functional as F

import video
from rife.model import DEFAULT_MODEL, MODELS, Interpolator


def psnr(a, b):
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return math.inf if mse == 0 else 10 * math.log10(255.0 ** 2 / mse)


def gaussian_kernel(device, window=11, sigma=1.5):
    coords = torch.arange(window, dtype=torch.float32, device=device) - (window - 1) / 2
    line = torch.exp(-coords.pow(2) / (2 * sigma ** 2))
    line /= line.sum()
    return (line[:, None] * line[None, :]).view(1, 1, window, window)


def to_luma(frame, device):
    tensor = torch.from_numpy(frame).to(device).permute(2, 0, 1)[None].float() / 255
    weights = torch.tensor([0.299, 0.587, 0.114], device=device).view(1, 3, 1, 1)
    return (tensor * weights).sum(1, keepdim=True)


def ssim(a, b, kernel, device):
    x, y = to_luma(a, device), to_luma(b, device)
    mu_x, mu_y = F.conv2d(x, kernel), F.conv2d(y, kernel)
    var_x = F.conv2d(x * x, kernel) - mu_x ** 2
    var_y = F.conv2d(y * y, kernel) - mu_y ** 2
    cov = F.conv2d(x * y, kernel) - mu_x * mu_y
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    numerator = (2 * mu_x * mu_y + c1) * (2 * cov + c2)
    denominator = (mu_x ** 2 + mu_y ** 2 + c1) * (var_x + var_y + c2)
    return (numerator / denominator).mean().item()


def blend(frame0, frame1):
    return ((frame0.astype(np.uint16) + frame1.astype(np.uint16) + 1) >> 1).astype(np.uint8)


def triplets(info, count):
    """Yield (before, truth, after) spread evenly over the video."""
    stride = max(2, (info.frames - 2) // count) if info.frames else 2
    window = deque(maxlen=3)
    taken = 0
    for index, frame in enumerate(video.read_frames(info)):
        window.append(frame)
        if len(window) == 3 and (index - 2) % stride == 0:
            yield tuple(window)
            taken += 1
            if taken >= count:
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("videos", nargs="+")
    parser.add_argument("--models", default=DEFAULT_MODEL, help="comma separated, or 'all'")
    parser.add_argument("--pairs", type=int, default=25, help="triplets sampled per video")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    names = list(MODELS) if args.models == "all" else args.models.split(",")
    interpolators = {name: Interpolator(name, device=args.device, scale=args.scale) for name in names}
    device = next(iter(interpolators.values())).device
    kernel = gaussian_kernel(device)

    for path in args.videos:
        info = video.VideoInfo(path)
        for interpolator in interpolators.values():
            interpolator.prepare(info.height, info.width)
        methods = ["repeat", "blend"] + names
        scores = {name: [] for name in methods}

        samples = 0
        for before, truth, after in triplets(info, args.pairs):
            samples += 1
            candidates = {"repeat": before, "blend": blend(before, after)}
            for name, interpolator in interpolators.items():
                candidates[name] = interpolator.interpolate_frames(before, after)
            for name, guess in candidates.items():
                scores[name].append((psnr(guess, truth), ssim(guess, truth, kernel, device)))

        print(f"\n{path}  {info}  ({samples} triplets)")
        print(f"  {'method':<12} {'PSNR dB':>9} {'SSIM':>8}")
        for name in methods:
            values = np.array(scores[name], dtype=np.float64)
            finite = values[np.isfinite(values[:, 0])]
            print(f"  {name:<12} {finite[:, 0].mean():9.2f} {values[:, 1].mean():8.4f}")


if __name__ == "__main__":
    main()
