"""Weight management and the frame-level interpolation API."""

import math
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections import namedtuple
from pathlib import Path

import torch
import torch.nn.functional as F

from .arch import IFNet

# Official Practical-RIFE weights, mirrored by the vs-rife project.
BASE_URL = "https://github.com/HolyWu/vs-rife/releases/download/model/"

ModelSpec = namedtuple("ModelSpec", "file channels scale_list modulo features")

MODELS = {
    "4.25": ModelSpec("flownet_v4.25.pkl", (192, 128, 96, 64, 32), (16, 8, 4, 2, 1), 64, 4),
    "4.26": ModelSpec("flownet_v4.26.pkl", (192, 128, 96, 64, 32), (16, 8, 4, 2, 1), 64, 4),
    # .heavy widens the flow blocks (4.25) or the frame encoder (4.26); .lite trims both.
    "4.25.heavy": ModelSpec("flownet_v4.25.heavy.pkl", (384, 256, 192, 128, 64), (16, 8, 4, 2, 1), 64, 4),
    "4.26.heavy": ModelSpec("flownet_v4.26.heavy.pkl", (192, 128, 96, 64, 32), (16, 8, 4, 2, 1), 64, 16),
    "4.25.lite": ModelSpec("flownet_v4.25.lite.pkl", (192, 128, 96, 64, 24), (32, 16, 8, 4, 1), 128, 4),
}

DEFAULT_MODEL = "4.25"

LUMA = (0.299, 0.587, 0.114)


def sampling_grid(height, width, device):
    """The identity grid and flow scaling used by ``arch.warp``."""
    div = torch.tensor([(width - 1.0) / 2.0, (height - 1.0) / 2.0], device=device)
    horizontal = torch.linspace(-1.0, 1.0, width, device=device).view(1, 1, 1, width).expand(-1, -1, height, -1)
    vertical = torch.linspace(-1.0, 1.0, height, device=device).view(1, 1, height, 1).expand(-1, -1, -1, width)
    return div, torch.cat([horizontal, vertical], 1)


def fastest_dtype(net, device, modulo=64, runs=6):
    """Time the network in both precisions and keep the quicker one.

    Half precision is a large win on GPUs with tensor cores and a large loss on
    those without them (the GTX 16xx line, for instance), and the two are not
    distinguishable from the compute capability alone.
    """
    if device.type != "cuda":
        return torch.float32
    height, width = (-(-side // modulo) * modulo for side in (256, 448))
    div, grid = sampling_grid(height, width, device)
    timings = {}
    for dtype in (torch.float16, torch.float32):
        net.to(device, dtype)
        img = torch.rand(1, 3, height, width, device=device, dtype=dtype)
        timestep = torch.full((1, 1, height, width), 0.5, device=device, dtype=dtype)
        runtimes = []
        with torch.inference_mode():
            for _ in range(2):
                net(img, img, timestep, div, grid)
            torch.cuda.synchronize()
            for _ in range(runs):
                start = time.perf_counter()
                net(img, img, timestep, div, grid)
                torch.cuda.synchronize()
                runtimes.append(time.perf_counter() - start)
        # the fastest run, so that a hitch elsewhere on the gpu cannot decide this
        timings[dtype] = min(runtimes)
    # half precision also costs a little accuracy, so it has to win clearly
    return torch.float16 if timings[torch.float16] < 0.9 * timings[torch.float32] else torch.float32


def available_devices():
    """Everything torch can run on here, best first: (spec, label, free bytes).

    CUDA cards are ranked by free memory rather than by index, so ``auto`` picks
    the card that is idle rather than the one that happens to be first.
    """
    found = []
    for index in range(torch.cuda.device_count()):
        free, total = torch.cuda.mem_get_info(index)
        name = torch.cuda.get_device_properties(index).name
        found.append((f"cuda:{index}",
                      f"{name} ({free / 2**30:.1f} of {total / 2**30:.1f} GiB free)", free))
    found.sort(key=lambda entry: -entry[2])
    if torch.backends.mps.is_available():
        found.append(("mps", "Apple GPU", 0))
    found.append(("cpu", f"CPU ({os.cpu_count()} threads)", 0))
    return found


def resolve_device(name=None):
    """None or "auto" means the roomiest gpu, then an Apple gpu, then the cpu."""
    if name and name != "auto":
        return torch.device(name)
    return torch.device(available_devices()[0][0])


def weights_dir():
    return Path(os.environ.get("SILKFRAME_CACHE", Path.home() / ".cache" / "silkframe"))


def fetch_weights(name, directory=None):
    """Return the local path to a checkpoint, downloading it on first use."""
    spec = MODELS[name]
    packaged = getattr(sys, "_MEIPASS", None)
    if packaged and (Path(packaged) / "weights" / spec.file).exists():
        return Path(packaged) / "weights" / spec.file  # shipped inside a packaged build
    directory = Path(directory) if directory else weights_dir()
    path = directory / spec.file
    if path.exists():
        return path

    directory.mkdir(parents=True, exist_ok=True)
    url = BASE_URL + spec.file
    tmp = path.with_suffix(path.suffix + ".part")
    print(f"downloading {spec.file} -> {path}", file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=60) as response, open(tmp, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done / total:.0%} ({done >> 20} / {total >> 20} MiB)", end="", file=sys.stderr)
        print("", file=sys.stderr)
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"could not download {url}: {exc}") from exc
    shutil.move(tmp, path)
    return path


class Interpolator:
    """Synthesises the frame that sits between two consecutive frames.

    Frames go in and come out as HxWx3 uint8 RGB arrays; ``upload`` keeps a
    frame on the device so that each source frame is transferred only once.
    """

    def __init__(self, model=DEFAULT_MODEL, device=None, fp16=None, scale=1.0, weights=None):
        if model not in MODELS:
            raise ValueError(f"unknown model {model!r}; choose from {', '.join(MODELS)}")
        spec = MODELS[model]

        self.device = resolve_device(device)
        # A lower scale makes the coarsest block work on a smaller image, which
        # needs correspondingly more padding to stay divisible.
        self.modulo = spec.modulo * math.ceil(max(1.0, 1.0 / scale))

        self.net = IFNet(spec.channels, [s / scale for s in spec.scale_list], spec.features)
        state = torch.load(weights or fetch_weights(model), map_location="cpu", weights_only=True)
        if any(k.startswith("module.") for k in state):
            state = {k[len("module."):]: v for k, v in state.items() if k.startswith("module.")}
        # The checkpoints also carry the training-only teacher and caltime branches.
        missing, _ = self.net.load_state_dict(state, strict=False)
        if missing:
            raise RuntimeError(f"checkpoint for {model} is missing weights: {missing[:4]}")
        self.net.eval().to(self.device)

        if fp16 is None:
            self.dtype = fastest_dtype(self.net, self.device, self.modulo)
        else:
            self.dtype = torch.float16 if fp16 else torch.float32
        self.net.to(self.dtype)

        self.luma = torch.tensor(LUMA, device=self.device).view(1, 3, 1, 1)
        self.height = self.width = None

    def prepare(self, height, width):
        """Fix the frame size: computes padding, the sampling grid and the t buffer."""
        self.height, self.width = height, width
        ph = -(-height // self.modulo) * self.modulo
        pw = -(-width // self.modulo) * self.modulo
        self.padded = (ph, pw)
        self.padding = (0, pw - width, 0, ph - height)
        self.div, self.grid = sampling_grid(ph, pw, self.device)
        self.timestep = torch.full((1, 1, ph, pw), 0.5, dtype=self.dtype, device=self.device)
        return self

    def upload(self, frame):
        """HxWx3 uint8 RGB -> padded [1,3,ph,pw] tensor in [0,1]."""
        if self.height is None:
            self.prepare(frame.shape[0], frame.shape[1])
        tensor = torch.from_numpy(frame).to(self.device).permute(2, 0, 1)[None]
        tensor = tensor.to(self.dtype).div_(255.0)
        return F.pad(tensor, self.padding) if any(self.padding) else tensor

    def to_frame(self, tensor):
        """[1,3,ph,pw] tensor -> HxWx3 uint8 RGB, padding removed."""
        tensor = tensor[0, :, : self.height, : self.width]
        tensor = tensor.clamp(0, 1).mul(255).round_().to(torch.uint8)
        return tensor.permute(1, 2, 0).contiguous().cpu().numpy()

    @torch.inference_mode()
    def at(self, img0, img1, t):
        """The frame ``t`` of the way from img0 to img1, for 0 < t < 1.

        Both arguments come from ``upload`` and the result is in the same
        layout.  The timestep is refilled in place rather than reallocated: a
        rate change like 30 -> 60000/1001 asks for a thousand distinct values of
        t, and one buffer of them at 1080p would be several gigabytes.
        """
        self.timestep.fill_(float(t))
        return self.net(img0, img1, self.timestep, self.div, self.grid)

    def middle(self, img0, img1):
        """The frame halfway between the two."""
        return self.at(img0, img1, 0.5)

    @torch.inference_mode()
    def probe_memory(self):
        """Warm the kernels up at the real frame size and report (peak use, free)."""
        if self.device.type != "cuda":
            return 0, 0
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats(self.device)
        blank = torch.zeros(1, 3, *self.padded, dtype=self.dtype, device=self.device)
        self.middle(blank, blank)
        peak = torch.cuda.max_memory_allocated(self.device)
        del blank
        torch.cuda.empty_cache()
        return peak, torch.cuda.mem_get_info(self.device)[0]

    @torch.inference_mode()
    def histogram(self, tensor):
        """Luma histogram of a 32x32 thumbnail: changes with the shot, not with motion."""
        small = F.adaptive_avg_pool2d(tensor.float(), 32)
        luma = (small * self.luma).sum(1)
        hist = torch.histc(luma, bins=32, min=0.0, max=1.0)
        return hist / hist.sum().clamp(min=1)

    @staticmethod
    @torch.inference_mode()
    def motion(img0, img1):
        """Mean absolute difference over every pixel; 0 for a repeated frame."""
        return (img0 - img1).abs().mean(dtype=torch.float32).item()

    @staticmethod
    def shot_change(hist0, hist1):
        """L1 distance between luma histograms, 0 (same) to 1 (no overlap)."""
        return (hist0 - hist1).abs().sum().item() / 2

    def interpolate_frames(self, frame0, frame1):
        """Convenience path for one-off pairs of numpy frames."""
        return self.to_frame(self.middle(self.upload(frame0), self.upload(frame1)))
