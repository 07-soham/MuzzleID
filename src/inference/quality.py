"""Image safety and basic acquisition quality checks."""
from io import BytesIO
import warnings
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def inspect_image(raw: bytes) -> dict:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                width, height = image.size
                image_format = image.format
                frames = getattr(image, "n_frames", 1)
                if image_format not in ALLOWED_FORMATS:
                    return {"valid": False, "reason": "Use JPEG, PNG or WebP images."}
                if width * height > MAX_IMAGE_PIXELS:
                    return {"valid": False, "reason": "Image dimensions exceed the safe processing limit."}
                if frames != 1:
                    return {"valid": False, "reason": "Animated images are not supported."}
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                image.load()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return {"valid": False, "reason": "Unreadable or unsafe image."}
    if width < 160 or height < 160:
        return {"valid": False, "reason": "Image must be at least 160 × 160 pixels."}
    return {"valid": True, "width": width, "height": height, "format": image_format, "quality": "Not available"}
