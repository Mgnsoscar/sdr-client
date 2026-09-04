#!/usr/bin/env python3
"""
Generate the application icon (app.ico for Windows, app.png for macOS/Linux and
the installer) from the app's own theme colours and bundled IBM Plex Sans face.

This is a PLACEHOLDER wordmark ("SDR" on the slate-blue accent tile) — good
enough to ship, trivial to replace: drop real artwork at ui/assets/app.ico
(a multi-size .ico) / ui/assets/app.png (>=256px) and PyInstaller/Inno pick it up.

Run from anywhere:
    python packaging/make_icon.py

Needs Pillow (build-time only; not an app runtime dependency):
    pip install Pillow
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "ui" / "assets"
FONT = ASSETS / "fonts" / "IBMPlexSans-700.ttf"

ACCENT = (44, 110, 155)     # Palette.ACCENT  #2C6E9B
ACCENT_INK = (31, 84, 118)  # Palette.ACCENT_INK #1F5476
WHITE = (255, 255, 255)

MASTER = 1024               # render big, downsample for crisp small sizes
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def _render(size: int) -> Image.Image:
    """One square icon at `size` px: a rounded accent tile with a vertical
    gradient, a subtly lighter top edge, and the white 'SDR' wordmark."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    radius = int(size * 0.22)
    # Vertical gradient ACCENT -> ACCENT_INK, painted row by row then masked to
    # the rounded square.
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(size - 1, 1)
        r = round(ACCENT[0] + (ACCENT_INK[0] - ACCENT[0]) * t)
        g = round(ACCENT[1] + (ACCENT_INK[1] - ACCENT[1]) * t)
        b = round(ACCENT[2] + (ACCENT_INK[2] - ACCENT[2]) * t)
        gd.line([(0, y), (size, y)], fill=(r, g, b, 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius, fill=255)
    img.paste(grad, (0, 0), mask)

    # A faint highlight arc across the top for a little depth.
    hl = max(1, int(size * 0.02))
    d.rounded_rectangle([hl, hl, size - 1 - hl, size - 1 - hl], radius,
                        outline=(255, 255, 255, 40), width=hl)

    # The wordmark. IBM Plex Sans Bold, sized to fit ~72% of the width.
    text = "SDR"
    font_px = int(size * 0.40)
    try:
        font = ImageFont.truetype(str(FONT), font_px)
    except OSError:
        font = ImageFont.load_default()
    l, t_, r, b = d.textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t_
    d.text(((size - tw) / 2 - l, (size - th) / 2 - t_), text, font=font, fill=WHITE)
    return img


def main() -> None:
    master = _render(MASTER)
    png = ASSETS / "app.png"
    master.resize((256, 256), Image.LANCZOS).save(png)
    ico = ASSETS / "app.ico"
    # Save from a 256px base; Pillow embeds every requested size (down to 16px).
    base = master.resize((256, 256), Image.LANCZOS)
    base.save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"wrote {ico.relative_to(REPO)} ({', '.join(str(s) for s in ICO_SIZES)} px)")
    print(f"wrote {png.relative_to(REPO)} (256 px)")


if __name__ == "__main__":
    main()
