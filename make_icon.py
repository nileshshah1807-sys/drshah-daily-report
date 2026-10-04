"""
Creates static/app.ico (the Windows .exe icon) from static/logo.png.

Run once; build_exe.bat runs it automatically. Needs Pillow (already in
requirements.txt).
"""
import os
import sys

try:
    from PIL import Image
except ImportError:
    print("Pillow is not installed.  Run:  pip install pillow")
    sys.exit(1)

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "static", "logo.png")
OUT = os.path.join(BASE, "static", "app.ico")
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    if not os.path.exists(SRC):
        print(f"Logo not found: {SRC}")
        return 1
    img = Image.open(SRC).convert("RGBA")

    # pad to a square so Windows does not stretch the icon
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)

    canvas.save(OUT, format="ICO", sizes=SIZES)
    print(f"Icon written: {OUT}  ({os.path.getsize(OUT):,} bytes)")
    print("Sizes embedded:", ", ".join(f"{w}x{h}" for w, h in SIZES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
