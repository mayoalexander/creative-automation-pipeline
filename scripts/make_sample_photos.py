"""Generate the sample "client-supplied" product photos (stand-ins for real photography).

A real client uploads their own shots; these exist so the demo starts from photographs
rather than drawings. Run once, with a key:
    python scripts/make_sample_photos.py --env-file .env
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engines.vanilla.config import load_env  # noqa: E402
from engines.vanilla.providers import OpenRouterImageProvider  # noqa: E402

STYLE = ("Commercial studio product photograph for a premium personal-care advertisement. "
         "Soft diffused key light, gentle reflection and contact shadow, shallow depth of field, 85 mm lens. "
         "The product is centred and fills about 45% of the frame height, with clean negative space above and "
         "around it. The label is blank: no text, no letters, no logos anywhere in the image.")
PHOTOS = {
    "assets/products/shampoo.png":
        "A tall teal plastic shampoo bottle with a white flip cap, standing on a pale mint stone plinth, "
        "with a few fresh mint leaves and a halved lime beside it, soft mint-green seamless background. " + STYLE,
    "assets/markets/jp/products/shampoo.png":
        "The same tall teal plastic shampoo bottle with a white flip cap, standing on a pale blush stone plinth, "
        "with a few cherry-blossom petals scattered around it, soft pale-pink seamless background, spring in Japan. " + STYLE,
}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--model", default="google/gemini-2.5-flash-image")
    a = ap.parse_args()
    load_env(a.env_file)
    provider = OpenRouterImageProvider(a.model)
    for rel, prompt in PHOTOS.items():
        img = provider.generate(prompt, {"name": rel})
        (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
        img.save(ROOT / rel)
        print("wrote", rel, img.size)
