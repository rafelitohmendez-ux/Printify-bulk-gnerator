"""Offline tests for print_image.py (white-frame detection/cropping) and the retry in server."""
import asyncio
import base64
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from offline_server import server  # noqa: E402
from print_image import clean_print_image, is_print_safe  # noqa: E402


def art(size=(400, 400), frame=0, frame_color="white"):
    """White line art on a black panel, optionally inside a light frame of `frame` px."""
    img = Image.new("RGB", size, frame_color if frame else "black")
    d = ImageDraw.Draw(img)
    w, h = size
    d.rectangle((frame, frame, w - frame - 1, h - frame - 1), fill="black")
    for i in range(10):
        d.line((w * 0.3, h * (0.25 + i * 0.05), w * 0.7, h * (0.3 + i * 0.04)), fill="white", width=3)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def size_of(b):
    return Image.open(io.BytesIO(b)).size


def test_full_bleed_black_art_untouched():
    raw = art()
    out, cropped, safe = clean_print_image(raw)
    assert out == raw and not cropped and safe


def test_white_frame_cropped_to_panel():
    raw = art(frame=60)
    assert not is_print_safe(raw)
    out, cropped, safe = clean_print_image(raw)
    assert cropped and safe
    w, h = size_of(out)
    assert 270 <= w <= 282 and 270 <= h <= 282  # 400 - 2*60, minus a small inset


def test_light_grey_frame_also_cropped():
    out, cropped, safe = clean_print_image(art(frame=40, frame_color=(225, 225, 225)))
    assert cropped and safe


def test_mostly_white_image_is_rejected():
    img = Image.new("RGB", (300, 300), "white")
    ImageDraw.Draw(img).ellipse((140, 140, 160, 160), fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    _, cropped, safe = clean_print_image(buf.getvalue())
    assert not safe and not cropped


def test_server_crops_and_retries(monkeypatch):
    calls = []
    outputs = [None, art(frame=50)]

    async def fake(prompt):
        calls.append(prompt)
        return outputs[len(calls) - 1] if len(calls) <= len(outputs) else None

    # 1) framed image -> cropped on the first try
    outputs[:] = [art(frame=50)]
    calls.clear()
    monkeypatch.setattr(server, "_gemini_image_bytes", fake)
    b64 = asyncio.run(server.llm_generate_image("x"))
    assert b64 and len(calls) == 1 and is_print_safe(base64.b64decode(b64))

    # 2) unfixable white image, then a good one -> retried once, good one returned
    white = Image.new("RGB", (200, 200), "white")
    buf = io.BytesIO()
    white.save(buf, "PNG")
    outputs[:] = [buf.getvalue(), art()]
    calls.clear()
    b64 = asyncio.run(server.llm_generate_image("x"))
    assert len(calls) == 2 and base64.b64decode(b64) == art()

    # 3) unfixable twice -> None (never an unprintable image)
    outputs[:] = [buf.getvalue(), buf.getvalue()]
    calls.clear()
    assert asyncio.run(server.llm_generate_image("x")) is None and len(calls) == 2


def test_prompt_forbids_frames():
    assert "NO white or light background" in server.NO_FRAME_RULE
