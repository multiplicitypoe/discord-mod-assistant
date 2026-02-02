from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image


def _guess_mime(image: Image.Image) -> str:
    fmt = (image.format or "PNG").upper()
    if fmt == "JPG":
        fmt = "JPEG"
    if fmt == "JPEG":
        return "image/jpeg"
    if fmt == "PNG":
        return "image/png"
    if fmt == "WEBP":
        return "image/webp"
    if fmt == "GIF":
        return "image/gif"
    return "image/png"


def resize_image_bytes(data: bytes, max_dim: int) -> tuple[bytes, str, int, int]:
    with Image.open(BytesIO(data)) as image:
        original_mode = image.mode
        width, height = image.size
        if max_dim > 0 and (width > max_dim or height > max_dim):
            image.thumbnail((max_dim, max_dim))
        out = BytesIO()
        if image.mode in {"RGBA", "P"}:
            image = image.convert("RGBA")
            image.save(out, format="PNG")
            content_type = "image/png"
        else:
            image = image.convert("RGB") if original_mode != "RGB" else image
            image.save(out, format="JPEG", quality=85, optimize=True)
            content_type = "image/jpeg"
        data_out = out.getvalue()
        return data_out, content_type, image.size[0], image.size[1]


def to_data_url(data: bytes, content_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"
