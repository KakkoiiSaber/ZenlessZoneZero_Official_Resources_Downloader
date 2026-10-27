"""Image header metadata and the NAS-local preview size index."""
import json
from pathlib import Path
import warnings

from PIL import Image, UnidentifiedImageError


def read_image_size(path):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                # PNG getexif() can decode pixel data; only JPEG orientation needs this lookup.
                if image.format == 'JPEG' and image.getexif().get(274) in (5, 6, 7, 8):
                    width, height = height, width
        return [width, height] if width > 0 and height > 0 else None
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def load_size_index(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}
