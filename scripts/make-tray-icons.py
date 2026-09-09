#!/usr/bin/env python3
"""Colored menu-bar dots: idle / listening / thinking / speaking (+ @2x)."""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "ui" / "tray"

COLORS = {
    "idle":      (255, 45, 45),
    "listening": (0, 255, 136),
    "thinking":  (170, 136, 255),
    "speaking":  (255, 149, 0),
    "offline":   (110, 110, 118),
}


def render_circle(size: int, rgb: tuple[int, int, int]) -> np.ndarray:
    y, x = np.ogrid[-1:1:complex(size), -1:1:complex(size)]
    r = np.sqrt(x * x + y * y)
    rgba = np.zeros((size, size, 4), dtype=np.float32)
    core = np.clip((0.62 - r) / 0.08, 0, 1)
    glow = np.clip((0.78 - r) / 0.22, 0, 1) * 0.35
    rgba[..., 0] = rgb[0] / 255.0
    rgba[..., 1] = rgb[1] / 255.0
    rgba[..., 2] = rgb[2] / 255.0
    rgba[..., 3] = np.clip(core + glow, 0, 1)
    highlight = np.clip((0.28 - np.sqrt((x + 0.12) ** 2 + (y + 0.18) ** 2)) / 0.12, 0, 1)
    rgba[..., :3] = np.clip(rgba[..., :3] + highlight[..., None] * 0.35, 0, 1)
    rgba[..., 3] = np.clip(rgba[..., 3] + highlight * 0.15, 0, 1)
    return (rgba * 255).astype(np.uint8)


def write_png(path: Path, rgba: np.ndarray) -> None:
    h, w = rgba.shape[:2]
    raw = b"".join(b"\x00" + rgba[i].tobytes() for i in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, rgb in COLORS.items():
        for px, suffix in ((22, ""), (44, "@2x")):
            png = OUT / f"{name}{suffix}.png"
            write_png(png, render_circle(px, rgb))
            print(f"Wrote {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
