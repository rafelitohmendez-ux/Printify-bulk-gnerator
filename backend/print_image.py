"""Make generated print graphics safe for DTG on a black shirt.

Gemini sometimes returns the artwork as a "poster": a black panel on a white (or light)
canvas. Printed on a black tee, that light canvas becomes a solid white rectangle.
clean_print_image() detects a light frame around the image, crops to the dark artwork
panel, and reports whether the result is usable (dark all the way to the edges).
"""
import io
from typing import Tuple

from PIL import Image

LIGHT = 200          # luminance at/above this counts as "light"
DARK = 70            # luminance below this counts as "dark"
BORDER_FRAC = 0.03   # width of the edge ring that is inspected, as a fraction of size
MAX_LIGHT_EDGE = 0.15  # an image is print-safe if at most this share of its edge ring is light
INSET_FRAC = 0.01    # extra crop past the panel edge to drop anti-aliased fringe


def _edge_light_share(img: Image.Image) -> float:
    """Share of pixels in the outer ring that are light."""
    w, h = img.size
    b = max(1, int(min(w, h) * BORDER_FRAC))
    px = img.load()
    light = total = 0
    for y in range(h):
        if b <= y < h - b:
            xs = list(range(0, b)) + list(range(w - b, w))
        else:
            xs = range(w)
        for x in xs:
            total += 1
            light += px[x, y] >= LIGHT
    return light / total if total else 0.0


def _gray(image_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        # transparent areas count as black (they won't print)
        bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
        img = Image.alpha_composite(bg, img.convert("RGBA"))
    return img.convert("L")


def is_print_safe(image_bytes: bytes) -> bool:
    small = _gray(image_bytes)
    small.thumbnail((256, 256))
    return _edge_light_share(small) <= MAX_LIGHT_EDGE


def clean_print_image(image_bytes: bytes) -> Tuple[bytes, bool, bool]:
    """Return (image_bytes, cropped, print_safe).

    If the edges are mostly light, crop to the bounding box of the dark artwork panel.
    print_safe is False when the result still has a light frame (caller should regenerate).
    """
    gray = _gray(image_bytes)
    probe = gray.copy()
    probe.thumbnail((256, 256))
    if _edge_light_share(probe) <= MAX_LIGHT_EDGE:
        return image_bytes, False, True

    # Bounding box of dark pixels = the artwork panel
    mask = gray.point(lambda v: 255 if v < DARK else 0)
    bbox = mask.getbbox()
    if not bbox:
        return image_bytes, False, False
    l, t, r, btm = bbox
    w, h = r - l, btm - t
    if w < gray.size[0] * 0.3 or h < gray.size[1] * 0.3:
        return image_bytes, False, False  # dark area too small to be the panel
    inset = int(min(w, h) * INSET_FRAC)
    box = (l + inset, t + inset, r - inset, btm - inset)

    original = Image.open(io.BytesIO(image_bytes))
    cropped = original.crop(box)
    buf = io.BytesIO()
    cropped.save(buf, "PNG")
    out = buf.getvalue()
    return out, True, is_print_safe(out)
