"""Image safety and basic acquisition quality checks."""
from io import BytesIO
from PIL import Image, UnidentifiedImageError


def inspect_image(raw: bytes) -> dict:
    try:
        with Image.open(BytesIO(raw)) as image:
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
            image_format = image.format
            image.load()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        return {"valid": False, "reason": f"Unreadable image: {exc}"}
    if width < 160 or height < 160:
        return {"valid": False, "reason": "Image must be at least 160 × 160 pixels."}
    return {"valid": True, "width": width, "height": height, "format": image_format, "quality": "Not available"}
