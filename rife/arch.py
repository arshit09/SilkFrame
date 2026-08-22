"""IFNet — the RIFE v4.25 / v4.26 network.

Inference-path port of Practical-RIFE's ``train_log/IFNet_HDv3.py``
(https://github.com/hzwer/Practical-RIFE), cross-checked against
https://github.com/HolyWu/vs-rife.  Loads the official weights unmodified.

The v4.25 family runs five coarse-to-fine flow blocks.  Each block predicts a
bidirectional flow residual, a blending mask and an 8-channel feature map that
is handed to the next block; the final frame is the mask-blended average of the
two warped inputs.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


def conv(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1):
    return nn.Sequential(
        nn.Conv2d(in_planes, out_planes, kernel_size, stride, padding, dilation, bias=True),
        nn.LeakyReLU(0.2, True),
    )


def warp(img, flow, grid, div):
    """Backward-warp ``img`` by ``flow`` (pixels).  Sampling stays in fp32."""
    dtype = img.dtype
    flow = torch.cat([flow[:, 0:1] / div[0], flow[:, 1:2] / div[1]], 1).float()
    g = (grid + flow).permute(0, 2, 3, 1)
    out = F.grid_sample(img.float(), g, mode="bilinear", padding_mode="border", align_corners=True)
    return out.to(dtype)


class Head(nn.Module):
    """Shallow feature encoder applied to each input frame."""

    def __init__(self, features=4):
        super().__init__()
        self.cnn0 = nn.Conv2d(3, 16, 3, 2, 1)
        self.cnn1 = nn.Conv2d(16, 16, 3, 1, 1)
        self.cnn2 = nn.Conv2d(16, 16, 3, 1, 1)
        self.cnn3 = nn.ConvTranspose2d(16, features, 4, 2, 1)
        self.relu = nn.LeakyReLU(0.2, True)

    def forward(self, x):
        x = self.relu(self.cnn0(x))
        x = self.relu(self.cnn1(x))
        x = self.relu(self.cnn2(x))
        return self.cnn3(x)


class ResConv(nn.Module):
    def __init__(self, c, dilation=1):
        super().__init__()
        self.conv = nn.Conv2d(c, c, 3, 1, dilation, dilation=dilation, groups=1)
        self.beta = nn.Parameter(torch.ones((1, c, 1, 1)), requires_grad=True)
        self.relu = nn.LeakyReLU(0.2, True)

    def forward(self, x):
        return self.relu(self.conv(x) * self.beta + x)


class IFBlock(nn.Module):
    def __init__(self, in_planes, c=64):
        super().__init__()
        self.conv0 = nn.Sequential(
            conv(in_planes, c // 2, 3, 2, 1),
            conv(c // 2, c, 3, 2, 1),
        )
        self.convblock = nn.Sequential(*[ResConv(c) for _ in range(8)])
        self.lastconv = nn.Sequential(nn.ConvTranspose2d(c, 4 * 13, 4, 2, 1), nn.PixelShuffle(2))

    def forward(self, x, flow=None, scale=1):
        x = F.interpolate(x, scale_factor=1.0 / scale, mode="bilinear", align_corners=False)
        if flow is not None:
            flow = F.interpolate(flow, scale_factor=1.0 / scale, mode="bilinear", align_corners=False) / scale
            x = torch.cat((x, flow), 1)
        feat = self.conv0(x)
        feat = self.convblock(feat)
        tmp = self.lastconv(feat)
        tmp = F.interpolate(tmp, scale_factor=scale, mode="bilinear", align_corners=False)
        return tmp[:, :4] * scale, tmp[:, 4:5], tmp[:, 5:]


class IFNet(nn.Module):
    def __init__(self, channels=(192, 128, 96, 64, 32), scale_list=(16, 8, 4, 2, 1), features=4):
        super().__init__()
        # block0 sees both frames, both feature maps and t; the rest also see the
        # warped features, the previous mask, the previous 8-channel feature and the flow.
        self.block0 = IFBlock(7 + 2 * features, c=channels[0])
        for index in range(1, 5):
            setattr(self, f"block{index}", IFBlock(20 + 2 * features, c=channels[index]))
        self.encode = Head(features)
        self.scale_list = list(scale_list)

    def forward(self, img0, img1, timestep, div, grid):
        """img0/img1: [N,3,H,W] in [0,1], H and W already padded.  Returns [N,3,H,W]."""
        img0 = img0.clamp(0.0, 1.0)
        img1 = img1.clamp(0.0, 1.0)
        f0 = self.encode(img0)
        f1 = self.encode(img1)

        warped_img0, warped_img1 = img0, img1
        flow = mask = feat = None
        for i, block in enumerate((self.block0, self.block1, self.block2, self.block3, self.block4)):
            if flow is None:
                flow, mask, feat = block(
                    torch.cat((img0, img1, f0, f1, timestep), 1), None, scale=self.scale_list[i]
                )
            else:
                wf0 = warp(f0, flow[:, :2], grid, div)
                wf1 = warp(f1, flow[:, 2:4], grid, div)
                fd, mask, feat = block(
                    torch.cat((warped_img0, warped_img1, wf0, wf1, timestep, mask, feat), 1),
                    flow,
                    scale=self.scale_list[i],
                )
                flow = flow + fd
            warped_img0 = warp(img0, flow[:, :2], grid, div)
            warped_img1 = warp(img1, flow[:, 2:4], grid, div)

        mask = torch.sigmoid(mask)
        return warped_img0 * mask + warped_img1 * (1 - mask)
