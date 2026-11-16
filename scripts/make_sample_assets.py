"""Create the sample input assets used by the demo campaign.

Real clients upload their own assets. These are drawn with Pillow so the repo
has no licensing questions. The lotion product image is deliberately NOT
created, so the pipeline has to generate it.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"


FACES = {"latin": ("/System/Library/Fonts/Avenir Next.ttc", 8), "cjk": ("/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc", 0)}


def font(size: int, text: str = "A") -> ImageFont.FreeTypeFont:
    path, index = FACES["cjk" if any("\u3000" <= c <= "\u9fff" for c in text) else "latin"]
    for p, i in ((path, index), (FONT, 0)):
        try:
            return ImageFont.truetype(p, size, index=i)
        except OSError:
            continue
    return ImageFont.load_default(size)


def logo(path: Path, text: str, fg: str, bg: str) -> None:
    """A wordmark chip: heavy type, tight tracking, a small mark. Height 160 px; width fits the word."""
    f = font(92, text)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tw = probe.textlength(text, font=f)
    w, h = round(tw + 190), 160
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=36, fill=bg)
    d.ellipse((44, 58, 88, 102), outline=fg, width=9)              # the mark: an open ring
    d.text((112, h / 2 + 2), text, font=f, fill=fg, anchor="lm")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def product_shot(path: Path, bottle: str, cap: str, label: str, bg_top: str, bg_bottom: str) -> None:
    w, h = 1536, 1536
    img = Image.new("RGB", (w, h))
    top, bottom = Image.new("RGB", (1, 1), bg_top).getpixel((0, 0)), Image.new("RGB", (1, 1), bg_bottom).getpixel((0, 0))
    d = ImageDraw.Draw(img)
    for y in range(h):  # vertical gradient backdrop
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)))
    cx = w // 2
    d.ellipse((cx - 330, 1250, cx + 330, 1330), fill=(0, 0, 0, 60))          # shadow
    d.rounded_rectangle((cx - 230, 520, cx + 230, 1290), radius=90, fill=bottle)  # bottle
    d.rounded_rectangle((cx - 110, 380, cx + 110, 540), radius=30, fill=cap)      # cap
    d.rounded_rectangle((cx - 180, 760, cx + 180, 1060), radius=24, fill="white")  # label
    f = font(64)
    tw = d.textlength(label, font=f)
    d.text((cx - tw / 2, 870), label, font=f, fill=bottle)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


if __name__ == "__main__":
    a = ROOT / "assets"
    logo(a / "brands/acme/logo.png", "ACME", "#FFFFFF", "#0B3D91")
    logo(a / "markets/jp/logo.png", "アクメ", "#FFFFFF", "#0B3D91")
    logo(a / "partners/sportco/logo.png", "SportCo", "#111111", "#FFC845")
    # Product photos come from scripts/make_sample_photos.py (photographic stand-ins for client shots);
    # product_shot() stays as an offline fallback only. Lotion is intentionally missing -> generated.
    for rel in ("products/shampoo.png", "markets/jp/products/shampoo.png"):
        if not (a / rel).exists():
            product_shot(a / rel, "#0E7C66", "#FFFFFF", "FRESH", "#E8FFF8", "#9EE6D3")
    print("sample assets written to", a)
