"""GenAI providers: image generation and message translation.

Each provider has a mock twin so the whole pipeline runs offline (tests, CI,
demo without a key). The OpenAI calls use only the standard library.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageFont

OPENAI_URL = "https://api.openai.com/v1"


class ProviderError(RuntimeError):
    pass


def _post_json(url: str, payload: dict, api_key: str, timeout: int = 180, retries: int = 2) -> dict:
    """POST with a small retry on rate limits / server errors."""
    body = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise ProviderError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise ProviderError(str(e)) from e
    raise ProviderError("unreachable")


# ------------------------------------------------------------------ images
class MockImageProvider:
    """Deterministic placeholder 'product shot' for offline runs."""
    name = "mock"

    def for_models(self, preferred: list[str], fallbacks: list[str] = ()):
        return self

    def generate(self, prompt: str, product: dict) -> Image.Image:
        seed = int(hashlib.sha1(prompt.encode()).hexdigest()[:6], 16)
        hue = seed % 360
        img = Image.new("HSV", (1024, 1024), (int(hue / 360 * 255), 60, 235)).convert("RGB")
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((372, 260, 652, 860), radius=70, fill=(250, 250, 250))
        d.rounded_rectangle((452, 170, 572, 270), radius=20, fill=(30, 30, 30))
        try:
            f = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 40)
        except OSError:
            f = ImageFont.load_default(40)
        label = product["name"].split()[-1].upper()
        w = d.textlength(label, font=f)
        d.text((512 - w / 2, 540), label, font=f, fill=(40, 40, 40))
        return img


class OpenAIImageProvider:
    """OpenAI Images API (gpt-image-1). Returns base64 PNG."""
    name = "openai"

    def __init__(self, model: str = "gpt-image-1", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise ProviderError("OPENAI_API_KEY is not set (add it to .env or use --provider mock)")

    def for_models(self, preferred: list[str], fallbacks: list[str] = ()):
        return self   # direct OpenAI has one image model here

    def generate(self, prompt: str, product: dict) -> Image.Image:
        data = _post_json(
            f"{OPENAI_URL}/images/generations",
            {"model": self.model, "prompt": prompt, "size": "1024x1024", "n": 1},
            self.api_key,
        )
        b64 = data["data"][0]["b64_json"]
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


class OpenRouterImageProvider:
    """Any image model on OpenRouter (Gemini image, FLUX, GPT image...) with one key.

    Uses Chat Completions with `modalities`; images come back as base64 data URLs
    in choices[0].message.images.
    """
    name = "openrouter"
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, model: str, image_only: bool | None = None, api_key: str | None = None):
        self.model = model
        self.label = f"openrouter:{model}"
        if image_only is None:   # models that answer with text too (Gemini, GPT-5 image) vs image-only (FLUX, ...)
            image_only = not model.startswith(("google/gemini", "openai/gpt-5"))
        self.modalities = ["image"] if image_only else ["image", "text"]
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise ProviderError("OPENROUTER_API_KEY is not set (add it to .env or use --provider mock)")

    def for_models(self, preferred: list[str], fallbacks: list[str] = ()):
        """Chain: the creative type's preferred models, then this (default) model, then fallbacks."""
        chain = list(dict.fromkeys([*preferred, self.model, *fallbacks]))
        return ChainImageProvider([OpenRouterImageProvider(m, api_key=self.api_key) for m in chain])

    def generate(self, prompt: str, product: dict) -> Image.Image:
        data = _post_json(
            self.URL,
            {"model": self.model, "modalities": self.modalities,
             "messages": [{"role": "user", "content": prompt}]},
            self.api_key,
        )
        try:
            url = data["choices"][0]["message"]["images"][0]["image_url"]["url"]
        except (KeyError, IndexError) as e:
            reason = (data.get("choices") or [{}])[0].get("finish_reason", "no image")
            raise ProviderError(f"refused ({reason})" if reason == "content_filter" else f"no image ({reason})") from e
        b64 = url.split(",", 1)[1] if url.startswith("data:") else url
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


class ChainImageProvider:
    """Try models in order; a refusal or error falls through to the next. All reasons are kept."""

    def __init__(self, providers: list):
        self.providers = providers
        # cache identity = the first choice; adding a fallback must not invalidate paid-for images
        self.name = getattr(providers[0], "label", providers[0].name)
        self.chain = " → ".join(getattr(p, "label", p.name) for p in providers)

    def generate(self, prompt: str, product: dict) -> Image.Image:
        errors = []
        for p in self.providers:
            try:
                return p.generate(prompt, product)
            except ProviderError as e:
                errors.append(f"{getattr(p, 'label', p.name)}: {e}")
        raise ProviderError(" | ".join(errors))


# ------------------------------------------------------------- translation
class MockTranslator:
    name = "mock"

    def translate(self, text: str, locale: str) -> str | None:
        return None  # offline: caller falls back to English and flags it


class OpenAITranslator:
    """Chat-completions translator. OpenRouter speaks the same API, so one class serves both."""
    name = "openai"
    url = f"{OPENAI_URL}/chat/completions"
    key_env = "OPENAI_API_KEY"

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get(self.key_env, "")

    def translate(self, text: str, locale: str) -> str | None:
        if not self.api_key:
            return None
        data = _post_json(
            self.url,
            {
                "model": self.model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": (
                        "You localize short advertising headlines. Return ONLY the localized headline, "
                        "no quotes, natural for native speakers, about the same length.")},
                    {"role": "user", "content": f"Locale: {locale}\nHeadline: {text}"},
                ],
            },
            self.api_key,
            timeout=60,
        )
        return data["choices"][0]["message"]["content"].strip().strip('"')


class OpenRouterTranslator(OpenAITranslator):
    name = "openrouter"
    url = OpenRouterImageProvider.URL
    key_env = "OPENROUTER_API_KEY"


def build_providers(kind: str, genai_cfg: dict):
    if kind == "mock":
        return MockImageProvider(), MockTranslator()
    if kind == "openai":
        return (
            OpenAIImageProvider(genai_cfg.get("image_model", "gpt-image-1")),
            OpenAITranslator(genai_cfg.get("text_model", "gpt-4o-mini")),
        )
    if kind == "openrouter":
        return (
            OpenRouterImageProvider(genai_cfg.get("openrouter_image_model", "google/gemini-2.5-flash-image")),
            OpenRouterTranslator(genai_cfg.get("openrouter_text_model", "google/gemini-2.5-flash")),
        )
    raise ValueError(f"unknown provider '{kind}' (use openai, openrouter or mock)")
