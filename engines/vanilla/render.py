"""Compose one static ad from one source photo: three layouts, chosen by aspect ratio.

  square  (≈1:1)    full-bleed photo, product placed right of centre, brand gradient from
                    the left carrying a left-aligned headline and a solid button
  story   (tall)    product in the middle third; headline in the top and button in the bottom,
                    both kept inside the zones Stories/Reels leave clear of their own UI
  split   (wide)    solid brand panel on the left with logo, headline and button; photo right

The source is assumed to be a product shot with the product centred (the prompt asks for
that; supplied photos are briefed the same way). Where the photo does not cover the frame,
its own edge colours are extended so the background stays seamless: no blur, no pasted box.
"""
from __future__ import annotations

import itertools
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

# (path, face index) per role and script; first one that exists wins. macOS first, then Linux.
FONTS = {
    ("headline", "latin"): [("/System/Library/Fonts/Avenir Next.ttc", 8), ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0)],
    ("button", "latin"): [("/System/Library/Fonts/Avenir Next.ttc", 2), ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0)],
    ("small", "latin"): [("/System/Library/Fonts/Avenir Next.ttc", 5), ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0)],
    ("headline", "cjk"): [("/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc", 0), ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0)],
    ("button", "cjk"): [("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc", 0), ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0)],
    ("small", "cjk"): [("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", 0), ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0)],
}
FALLBACK = ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0)


def hex_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def _is_cjk(text: str) -> bool:
    return any("　" <= ch <= "鿿" or "가" <= ch <= "힯" for ch in text)


def font(role: str, text: str, size: int, brand_font: str | None = None) -> ImageFont.ImageFont:
    script = "cjk" if _is_cjk(text) else "latin"
    candidates = ([(brand_font, 0)] if brand_font and script == "latin" and role == "headline" else []) + FONTS[(role, script)] + [FALLBACK]
    for path, index in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size, index=index)
    return ImageFont.load_default(size)


NO_LINE_START = set("、。，．！？」』）〕ーぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ")


def _units(draw, text: str, f, max_w: int, split: bool = True) -> tuple[list[str], str]:
    """Breakable units. Latin: words. Japanese: phrases ending at 、。！？ (split into single
    characters only if a phrase is wider than the line), never starting a line with 、 or small kana."""
    if not _is_cjk(text):
        return text.split(), " "
    units = []
    for phrase in re.findall(r"[^、。！？]+[、。！？]?|[、。！？]", text):
        units += [phrase] if not split or draw.textlength(phrase, font=f) <= max_w else list(phrase)
    glued: list[str] = []
    for u in units:
        if glued and u[0] in NO_LINE_START:
            glued[-1] += u
        else:
            glued.append(u)
    return glued, ""


def wrap(draw: ImageDraw.ImageDraw, text: str, f, max_w: int, split: bool = True) -> list[str]:
    """Fewest lines that fit, then the most even split of those lines (no orphan words)."""
    units, joiner = _units(draw, text, f, max_w, split)
    width = lambda group: draw.textlength(joiner.join(group), font=f)
    greedy, line = [], []
    for u in units:
        if line and width(line + [u]) > max_w:
            greedy.append(line)
            line = []
        line.append(u)
    greedy += [line] if line else []
    n, best = len(greedy), None
    if 1 < n <= len(units) <= 14:                    # tiny search space: headlines are short
        for cuts in itertools.combinations(range(1, len(units)), n - 1):
            groups = [units[a:b] for a, b in zip((0,) + cuts, cuts + (len(units),))]
            widest = max(width(g) for g in groups)
            if widest <= max_w and (best is None or widest < best[0]):
                best = (widest, groups)
    return [joiner.join(g) for g in (best[1] if best else greedy)]


def fit(draw, text, role, box_w, box_h, start, minimum, brand_font=None, max_lines=3):
    """Largest size whose wrapped text fits the box. Returns (font, lines, size, line_height).
    Japanese phrases are kept whole if any size allows it; only then are they split by character."""
    for split in (False, True):
        for size in range(start, minimum - 1, -2):
            f = font(role, text, size, brand_font)
            lines, lh = wrap(draw, text, f, box_w, split), round(size * 1.08)
            fits = all(draw.textlength(l, font=f) <= box_w for l in lines)
            if fits and len(lines) <= max_lines and len(lines) * lh <= box_h:
                return f, lines, size, lh
    f = font(role, text, minimum, brand_font)
    return f, wrap(draw, text, f, box_w)[:max_lines], minimum, round(minimum * 1.08)


# ------------------------------------------------------------------ photo placement
def edge_colour(img: Image.Image, side: str) -> tuple[int, int, int]:
    w, h = img.size
    strip = {"top": (0, 0, w, 6), "bottom": (0, h - 6, w, h), "left": (0, 0, 6, h), "right": (w - 6, 0, w, h)}[side]
    return tuple(round(c) for c in ImageStat.Stat(img.crop(strip)).median)


def place(src: Image.Image, W: int, H: int, cx: float, cy: float, scale: float) -> Image.Image:
    """Put the (centred) product at (cx, cy) of a W×H frame, scaled so the source is `scale`×H tall.
    Uncovered frame is filled with the photo's own edge colours, blended vertically."""
    s = scale * H / src.height
    img = src.resize((round(src.width * s), round(src.height * s)), Image.LANCZOS)
    x, y = round(cx * W - img.width / 2), round(cy * H - img.height / 2)
    top, bottom = edge_colour(img, "top"), edge_colour(img, "bottom")
    canvas = Image.new("RGB", (W, H))
    for row in range(H):  # seamless ground: top colour above the photo, bottom colour below
        t = min(1, max(0, (row - y) / max(img.height, 1)))
        canvas.paste(tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)), (0, row, W, row + 1))
    edge = max(2, round(min(img.size) * 0.04))       # feather the photo's edges into the ground
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rectangle((edge, edge, img.width - edge, img.height - edge), fill=255)
    canvas.paste(img, (x, y), mask.filter(ImageFilter.GaussianBlur(edge / 2)))
    return canvas


def gradient(canvas: Image.Image, colour, box, axis: str, a0: float, a1: float, solid_until: float = 0.0) -> None:
    """Brand-colour scrim over `box`, opacity a0 → a1 along `axis` ('x' or 'y'); fully solid for
    the first `solid_until` of the run, so the brand colour exists un-blended somewhere."""
    x0, y0, x1, y1 = box
    n = (x1 - x0) if axis == "x" else (y1 - y0)
    alpha = Image.new("L", (x1 - x0, y1 - y0))
    d = ImageDraw.Draw(alpha)
    for i in range(n):
        t = i / max(n - 1, 1)
        a = a0 if t <= solid_until else a0 + (a1 - a0) * ((t - solid_until) / max(1 - solid_until, 1e-6)) ** 1.4
        if axis == "x":
            d.line([(i, 0), (i, y1 - y0)], fill=round(255 * a))
        else:
            d.line([(0, i), (x1 - x0, i)], fill=round(255 * a))
    canvas.paste(Image.new("RGB", alpha.size, colour), (x0, y0), alpha)


# ------------------------------------------------------------------ pieces
def paste_logo(canvas, path: Path, height: int, x: int, y: int):
    logo = Image.open(path).convert("RGBA")
    logo = logo.resize((round(logo.width * height / logo.height), height), Image.LANCZOS)
    canvas.paste(logo, (x, y), logo)
    return (x, y, logo.width, logo.height), logo


def draw_lines(draw, lines, f, lh, x, y, colour, align="left", box_w=0):
    for line in lines:
        lx = x + (box_w - draw.textlength(line, font=f)) / 2 if align == "center" else x
        draw.text((lx, y), line, font=f, fill=colour)
        y += lh
    return y


def button(draw, text, x, y, size, bg, fg, align="left", box_w=0):
    f = font("button", text, size)
    w = draw.textlength(text, font=f)
    pad_x, h = size * 1.1, size * 2.3
    bx = x + (box_w - (w + 2 * pad_x)) / 2 if align == "center" else x
    draw.rounded_rectangle((bx, y, bx + w + 2 * pad_x, y + h), radius=h / 2, fill=bg)
    draw.text((bx + pad_x, y + h / 2), text, font=f, fill=fg, anchor="lm")
    return y + h


def mean_colour(img: Image.Image, box) -> tuple[int, int, int]:
    x0, y0, x1, y1 = (max(0, round(v)) for v in box)
    return tuple(round(c) for c in ImageStat.Stat(img.crop((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1))).convert("RGB")).mean)


def layout_for(w: int, h: int) -> str:
    r = w / h
    return "split" if r >= 1.3 else "story" if r <= 0.75 else "square"


# ------------------------------------------------------------------ the ad
def render_static(source: Image.Image, fmt: dict, brand: dict, partner: dict | None,
                  message: str, cta: str | None, disclaimers: list[str], root: Path,
                  safe_zone: float = 0.06) -> tuple[Image.Image, dict, Image.Image]:
    """Returns (creative, layout, logo). layout + logo let brand_check verify the pixels."""
    W, H = fmt["width"], fmt["height"]
    colors = brand["assets"]["colors"]
    primary, text_rgb = hex_rgb(colors["primary"]), hex_rgb(colors["text"])
    brand_font = brand["assets"].get("font")
    kind, m = layout_for(W, H), round(min(W, H) * max(safe_zone, 0.06))

    if kind == "square":
        canvas = place(source, W, H, cx=0.73, cy=0.54, scale=1.12)
        # solid behind the whole text column (white on brand colour must clear 4.5:1), then fade
        gradient(canvas, primary, (0, 0, round(W * 0.70), H), "x", 1.0, 0.0, solid_until=0.68)
        text_box = (m, round(H * 0.30), round(W * 0.46) - m, round(H * 0.42))   # x, y, w, h
        align, brand_sample = "left", (3, H // 2)
    elif kind == "story":
        canvas = place(source, W, H, cx=0.5, cy=0.57, scale=0.62)
        gradient(canvas, primary, (0, 0, W, round(H * 0.42)), "y", 1.0, 0.0, solid_until=0.80)
        gradient(canvas, primary, (0, round(H * 0.70), W, H), "y", 0.0, 1.0)
        top = round(H * 0.13)                          # Stories UI covers ~13% at the top
        text_box = (m, top + round(H * 0.06), W - 2 * m, round(H * 0.15))
        align, brand_sample = "center", (W - 3, 3)
    else:  # split
        panel = round(W * 0.42)
        canvas = place(source, W, H, cx=0.71, cy=0.52, scale=1.0)
        gradient(canvas, primary, (0, 0, panel + round(W * 0.10), H), "x", 1.0, 0.0, solid_until=panel / (panel + W * 0.10))
        text_box = (m, round(H * 0.30), panel - 2 * m, round(H * 0.36))
        align, brand_sample = "left", (3, H - 3)

    canvas = canvas.convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    # logo (+ partner lockup "×  partner") — top left; on a story, below the Stories header
    logo_h = round(min(W, H) * (0.075 if kind != "story" else 0.06))
    ly = round(H * 0.13) - logo_h - round(H * 0.015) if kind == "story" else m
    logo_box, logo_img = paste_logo(canvas, root / brand["assets"]["logo"], logo_h, m, ly)
    partner_box = None
    if partner and partner.get("assets", {}).get("logo"):
        xf = font("small", "×", round(logo_h * 0.55))
        xx = m + logo_box[2] + round(logo_h * 0.35)
        draw.text((xx, ly + logo_h / 2), "×", font=xf, fill=text_rgb, anchor="lm")
        partner_box, _ = paste_logo(canvas, root / partner["assets"]["logo"], round(logo_h * 0.72),
                                    xx + round(logo_h * 0.55), ly + round(logo_h * 0.14))

    # headline
    x, y, bw, bh = text_box
    start = round(min(W, H) * (0.115 if kind == "square" else 0.10 if kind == "split" else 0.105))
    f, lines, size, lh = fit(draw, message, "headline", bw, bh, start, round(min(W, H) * 0.045), brand_font)
    block_h = len(lines) * lh
    if kind == "story":
        y = y + (bh - block_h) // 2
    text_bg = mean_colour(canvas, (x, y, x + bw, y + block_h))       # the real pixels behind the words
    end = draw_lines(draw, lines, f, lh, x, y, text_rgb, align, bw)

    # button
    bsize = max(16, round(min(W, H) * (0.046 if kind == "story" else 0.034)))
    if cta:
        by = round(H * 0.80) if kind == "story" else end + round(size * 0.55)   # story: above the reply bar
        button(draw, cta, x, by, bsize, text_rgb, primary, align, bw)

    # disclaimers, small, at the bottom of the text side
    if disclaimers:
        dsize = max(13, round(min(W, H) * 0.02))
        dy = H - m - dsize * 1.3 * len(disclaimers)
        for d in disclaimers:
            df = font("small", d, dsize)
            dx = x + (bw - draw.textlength(d, font=df)) / 2 if align == "center" else x
            draw.text((dx, dy), d, font=df, fill=text_rgb)
            dy += dsize * 1.3

    layout = {
        "kind": kind, "size": (W, H), "logo_box": logo_box, "partner_box": partner_box,
        "band_color": primary, "brand_sample": brand_sample, "text_color": text_rgb, "text_bg": text_bg,
        "headline_font_size": size, "headline_lines": len(lines), "min_headline": round(min(W, H) * 0.045),
    }
    return canvas.convert("RGB"), layout, logo_img
