"""Compose one static creative: product + message band + logo(s) + disclaimer.

Layout decision: the product is never cropped. The source is CONTAINED in the
area above the text band and laid over a blurred COVER of itself, so one 1:1
source works for 1:1, 9:16 and 16:9 without cutting the product out of frame.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",   # Latin + CJK in one file
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",   # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def hex_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def resolve_font(preferred: str | None) -> str | None:
    for path in ([preferred] if preferred else []) + FONT_CANDIDATES:
        if path and Path(path).exists():
            return path
    return None  # Pillow's built-in font


def load_font(path: str | None, size: int) -> ImageFont.ImageFont:
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size)


def cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    r = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    left, top = (r.width - w) // 2, (r.height - h) // 2
    return r.crop((left, top, left + w, top + h))


def contain(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = min(w / img.width, h / img.height)
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)


def _is_cjk(text: str) -> bool:
    return any("　" <= ch <= "鿿" or "가" <= ch <= "힯" for ch in text)


def wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """Word-wrap Latin text; character-wrap CJK (no spaces between words)."""
    units = list(text) if _is_cjk(text) else text.split()
    joiner = "" if _is_cjk(text) else " "
    lines, line = [], ""
    for u in units:
        trial = f"{line}{joiner}{u}" if line else u
        if draw.textlength(trial, font=font) <= max_w:
            line = trial
        else:
            if line:
                lines.append(line)
            line = u
    if line:
        lines.append(line)
    return lines


def fit_text(draw, text, font_path, max_w, max_h, start, minimum, max_lines=3):
    """Largest font size whose wrapped text fits the box. Returns (font, lines, size)."""
    size = start
    while size >= minimum:
        f = load_font(font_path, size)
        lines = wrap(draw, text, f, max_w)
        if len(lines) <= max_lines and len(lines) * size * 1.2 <= max_h:
            return f, lines, size
        size -= 2
    f = load_font(font_path, minimum)
    return f, wrap(draw, text, f, max_w)[:max_lines], minimum


def feather_mask(size: tuple[int, int], fraction: float = 0.08) -> Image.Image:
    """Alpha mask that fades the outer `fraction` of each edge to transparent."""
    w, h = size
    edge = max(1, round(min(w, h) * fraction))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rectangle((edge, edge, w - edge, h - edge), fill=255)
    return mask.filter(ImageFilter.GaussianBlur(edge / 2))


def _paste_logo(canvas: Image.Image, logo_path: Path, box_w: int, x: int, y: int, align_right=False):
    logo = Image.open(logo_path).convert("RGBA")
    logo = contain(logo, box_w, box_w)
    if align_right:
        x = x - logo.width
    canvas.paste(logo, (x, y), logo)
    return (x, y, logo.width, logo.height), logo


def render_static(source: Image.Image, fmt: dict, brand: dict, partner: dict | None,
                  message: str, cta: str | None, disclaimers: list[str], root: Path,
                  safe_zone: float = 0.06) -> tuple[Image.Image, dict, Image.Image]:
    """Returns (creative, layout, logo). layout + logo let brand_check verify the pixels."""
    W, H = fmt["width"], fmt["height"]
    colors = brand["assets"]["colors"]
    primary, accent, text_rgb = hex_rgb(colors["primary"]), hex_rgb(colors["accent"]), hex_rgb(colors["text"])
    font_path = resolve_font(brand["assets"].get("font"))
    safe = round(min(W, H) * safe_zone)

    # 1. background: blurred cover of the source, slightly dimmed
    canvas = cover(source, W, H).filter(ImageFilter.GaussianBlur(radius=max(W, H) // 40))
    canvas = ImageEnhance.Brightness(canvas).enhance(0.9).convert("RGBA")

    # 2. text band at the bottom in the brand's primary colour
    band_h = round(H * (0.30 if W >= H else 0.26))
    band_top = H - band_h
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, band_top, W, H), fill=primary)

    # 3. product: contained in the area above the band, never cropped,
    #    with feathered edges so it blends into the blurred background
    area_w, area_h = W - 2 * safe, band_top - 2 * safe
    product = contain(source, area_w, area_h)
    canvas.paste(product, ((W - product.width) // 2, safe + (area_h - product.height) // 2),
                 feather_mask(product.size))

    # 4. logos: brand top-left, partner top-right (only if active in this market)
    logo_w = round(min(W, H) * 0.22)
    logo_box, logo_img = _paste_logo(canvas, root / brand["assets"]["logo"], logo_w, safe, safe)
    partner_box = None
    if partner and partner.get("assets", {}).get("logo"):
        partner_box, _ = _paste_logo(canvas, root / partner["assets"]["logo"], round(logo_w * 0.8),
                                     W - safe, safe, align_right=True)

    # 5. message (+ CTA) inside the band
    draw = ImageDraw.Draw(canvas)
    disc_size = max(14, round(min(W, H) * 0.022))
    disc_h = (disc_size * 1.3 * len(disclaimers)) if disclaimers else 0
    cta_size = max(18, round(min(W, H) * 0.035))
    cta_h = cta_size * 2.2 if cta else 0
    text_box_h = band_h - 2 * safe * 0.6 - cta_h - disc_h
    font, lines, size = fit_text(draw, message, font_path, W - 2 * safe, text_box_h,
                                 start=round(min(W, H) * 0.09), minimum=round(min(W, H) * 0.035))
    # centre the headline + CTA block vertically in the space above the disclaimer
    block_h = len(lines) * size * 1.2 + cta_h
    y = band_top + max(safe * 0.6, (band_h - disc_h - block_h) / 2)
    for line in lines:
        lw = draw.textlength(line, font=font)
        draw.text(((W - lw) / 2, y), line, font=font, fill=text_rgb)
        y += size * 1.2

    if cta:
        cf = load_font(font_path, cta_size)
        cw = draw.textlength(cta, font=cf)
        pad = cta_size * 0.8
        x0, y0 = (W - cw) / 2 - pad, y + cta_size * 0.5
        draw.rounded_rectangle((x0, y0, x0 + cw + 2 * pad, y0 + cta_size * 1.6), radius=cta_size, fill=accent)
        draw.text((x0 + pad, y0 + cta_size * 0.25), cta, font=cf, fill=primary)

    if disclaimers:
        df = load_font(font_path, disc_size)
        dy = H - safe * 0.4 - disc_h
        for d in disclaimers:
            dw = draw.textlength(d, font=df)
            draw.text(((W - dw) / 2, dy), d, font=df, fill=text_rgb)
            dy += disc_size * 1.3

    layout = {
        "size": (W, H),
        "logo_box": logo_box,
        "partner_box": partner_box,
        "band_box": (0, band_top, W, H),
        "band_color": primary,
        "text_color": text_rgb,
        "headline_font_size": size,
        "headline_lines": len(lines),
        "font": font_path,
    }
    return canvas.convert("RGB"), layout, logo_img
