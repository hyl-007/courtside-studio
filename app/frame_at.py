#!/usr/bin/env python3
"""frame_at.py: look at the source video the way the reel will show it (1080x1920, centre-cropped), read-only.

  python3 frame_at.py T [--zoom 1.1] [--grid] [--out FILE.png] [--source VIDEO]
        one frame at T seconds. --grid draws a labelled 100-pixel coordinate grid so you can place freeze-frame pointers
        (annotations use these 1080x1920 coordinates: x across 0-1080, y down 0-1920).
  python3 frame_at.py --sheet START END STEP [--out FILE.png] [--source VIDEO]
        a contact sheet of frames from START to END every STEP seconds, each labelled with its time.

The default source is the coach-sim raw video (a light 540p copy is used when available, so it is quick).
"""
from __future__ import annotations
import subprocess, sys, tempfile
from pathlib import Path
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / "library"
W, H = 1080, 1920


def grab(src, t, zoom=1.0, width=W):
    tmp = Path(tempfile.mkdtemp(prefix="fa_")) / "f.png"
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    if zoom and abs(zoom - 1.0) > 1e-3:
        vf += f",crop=iw/{zoom}:ih/{zoom}:(iw-ow)*0.5:(ih-oh)*0.5,scale={W}:{H}"
    if width != W:
        vf += f",scale={width}:-2"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(t, 0):.3f}", "-i", str(src), "-frames:v", "1", "-vf", vf, str(tmp)], check=True, timeout=120)
    return Image.open(tmp).convert("RGB")


def main(a):
    if not a or a[0] in ("-h", "--help"):
        print(__doc__); return 0
    opt = lambda k, d=None: a[a.index(k) + 1] if k in a else d
    src = Path(opt("--source", str(LIB / "coach-sim-1on1" / "proxy.mp4")))
    out = Path(opt("--out", str(Path(tempfile.gettempdir()) / "frame_at.png")))
    if a[0] == "--sheet":
        t0, t1, st = float(a[1]), float(a[2]), float(a[3]); ts = []; t = t0
        while t <= t1 + 1e-6 and len(ts) < 48:
            ts.append(t); t += st
        tw = 180; th = int(tw * H / W); cols = 6; rows = -(-len(ts) // cols); sheet = Image.new("RGB", (cols * tw, rows * (th + 16)), (14, 18, 48)); d = ImageDraw.Draw(sheet)
        for i, t in enumerate(ts):
            im = grab(src, t, width=tw); x, y = (i % cols) * tw, (i // cols) * (th + 16); sheet.paste(im, (x, y + 16)); d.text((x + 4, y + 2), f"{t:.1f}s", fill=(248, 200, 128))
        sheet.save(out); print(out); return 0
    t = float(a[0]); im = grab(src, t, float(opt("--zoom", "1.0")))
    if "--grid" in a:
        d = ImageDraw.Draw(im)
        for x in range(0, W, 100):
            d.line([(x, 0), (x, H)], fill=(255, 255, 0) if x % 500 == 0 else (255, 255, 255), width=1); d.text((x + 3, 3), str(x), fill=(255, 255, 0))
        for y in range(0, H, 100):
            d.line([(0, y), (W, y)], fill=(255, 255, 0) if y % 500 == 0 else (255, 255, 255), width=1); d.text((3, y + 3), str(y), fill=(255, 255, 0))
    im.thumbnail((720, 1280)); im.save(out); print(out); return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
