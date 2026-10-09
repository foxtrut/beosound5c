"""Contact pictures: whatever a phone uploads is decoded and re-encoded here,
so only a plain, small JPEG ever reaches the disk and the arc.

Re-encoding (rather than storing the upload) drops everything but the pixels —
EXIF with the phone's GPS position included — and means a file that merely
claims to be an image is refused instead of being served back to browsers.
"""

from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps

PHOTO_SIZE = 400                 # square, in pixels
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000          # refuse decompression bombs well before Pillow's own limit
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


class PhotoError(ValueError):
    """Rejected picture. ``str(error)`` is a short code the phone page translates."""


def normalise_photo(data: bytes) -> bytes:
    """Decode an uploaded picture and return a square PHOTO_SIZE JPEG of it,
    cropped around the centre and turned upright according to its EXIF."""
    if not data:
        raise PhotoError("photo_invalid")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PhotoError("photo_too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                if img.format not in ALLOWED_FORMATS:
                    raise PhotoError("photo_invalid")
                if img.width * img.height > MAX_PIXELS:
                    raise PhotoError("photo_too_large")
                img.load()
                upright = ImageOps.exif_transpose(img)
                square = ImageOps.fit(upright.convert("RGB"), (PHOTO_SIZE, PHOTO_SIZE),
                                      method=Image.Resampling.LANCZOS)
    except PhotoError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PhotoError("photo_invalid") from None
    out = io.BytesIO()
    square.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()
