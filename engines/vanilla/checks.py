"""Legal and brand-compliance checks.

legal_check  - runs BEFORE rendering; a hit blocks that market (fail closed).
brand_check  - runs AFTER rendering, on the actual pixels, not on intentions.
"""
from __future__ import annotations

import re

from PIL import Image, ImageChops, ImageStat


def legal_check(texts: dict[str, str], banned: list[str]) -> list[dict]:
    """Return one finding per banned word found. Latin words match on word
    boundaries (so 'cure' doesn't hit 'secure'); CJK terms match as substrings."""
    findings = []
    for field, text in texts.items():
        if not text:
            continue
        for word in banned:
            if word.isascii():
                hit = re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE)
            else:
                hit = word in text
            if hit:
                findings.append({"field": field, "word": word, "text": text})
    return findings


def _luminance(rgb) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def brand_check(img: Image.Image, layout: dict, logo: Image.Image) -> dict:
    results = {}

    # 1. logo present: the pixels where the logo is opaque must match the logo
    x, y, w, h = layout["logo_box"]
    region = img.crop((x, y, x + w, y + h)).convert("RGB")
    mask = logo.split()[-1].point(lambda a: 255 if a > 200 else 0)
    diff = ImageChops.difference(region, logo.convert("RGB"))
    mean_diff = sum(ImageStat.Stat(diff, mask).mean) / 3 if mask.getbbox() else 255
    results["logo_present"] = {"pass": mean_diff < 12, "mean_pixel_diff": round(mean_diff, 1)}

    # 2. brand colour: a point the layout guarantees is pure brand colour
    sample = img.getpixel(layout["brand_sample"])
    expected = layout["band_color"]
    dist = max(abs(a - b) for a, b in zip(sample, expected))
    results["brand_color"] = {"pass": dist <= 6, "sampled": "#%02X%02X%02X" % sample[:3],
                              "expected": "#%02X%02X%02X" % expected}

    # 3. legibility: WCAG AA contrast between the headline and the pixels actually behind it
    ratio = contrast_ratio(layout["text_color"], layout["text_bg"])
    results["text_contrast"] = {"pass": ratio >= 4.5, "ratio": round(ratio, 2)}

    # 4. headline didn't have to shrink to an unreadable size
    results["headline_size"] = {"pass": layout["headline_font_size"] > layout["min_headline"],
                                "px": layout["headline_font_size"]}

    results["pass"] = all(v["pass"] for v in results.values() if isinstance(v, dict))
    return results
