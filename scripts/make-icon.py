#!/usr/bin/env python3
"""Generate build/icon.icns — red arc-reactor orb for the Mac app."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build"
SIZE = 1024


def render_png(path: Path) -> None:
    y, x = np.ogrid[-1:1:complex(SIZE), -1:1:complex(SIZE)]
    r = np.sqrt(x * x + y * y)
    img = np.zeros((SIZE, SIZE, 3), dtype=np.float32)
    img[:] = (0.04, 0.015, 0.02)
    glow = np.clip(1.0 - r, 0, 1) ** 2
    ring = np.exp(-((r - 0.44) ** 2) / 0.0018)
    core = np.clip(1.0 - r / 0.28, 0, 1) ** 2
    img[..., 0] += glow * 0.55 + ring * 1.0 + core * 1.0
    img[..., 1] += glow * 0.08 + ring * 0.16 + core * 0.16
    img[..., 2] += glow * 0.10 + ring * 0.16 + core * 0.18
    rgb = (np.clip(img, 0, 1) * 255).astype(np.uint8)

    ppm = OUT / "icon.ppm"
    OUT.mkdir(parents=True, exist_ok=True)
    with ppm.open("wb") as f:
        f.write(f"P6\n{SIZE} {SIZE}\n255\n".encode())
        f.write(rgb.tobytes())
    subprocess.check_call(
        ["sips", "-s", "format", "png", str(ppm), "--out", str(path)],
        stdout=subprocess.DEVNULL,
    )
    ppm.unlink(missing_ok=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    png = OUT / "icon.png"
    render_png(png)

    iconset = OUT / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()

    sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for px, name in sizes:
        subprocess.check_call(
            ["sips", "-z", str(px), str(px), str(png), "--out", str(iconset / name)],
            stdout=subprocess.DEVNULL,
        )

    icns = OUT / "icon.icns"
    subprocess.check_call(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)])
    shutil.rmtree(iconset)
    print(f"Wrote {icns}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
