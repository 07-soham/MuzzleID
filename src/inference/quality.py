"""Image safety and basic acquisition quality checks."""
from io import BytesIO
import warnings
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_PIXELS = 20_000_000
def process_image(raw: bytes) -> tuple[bytes | None, dict]:
    """Decode a Pillow-supported raster image and normalize it to clean JPEG bytes.

    The incoming filename and browser MIME type are intentionally ignored; image
    format is determined from the file contents. Animated formats use frame 0.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS:
                    return None, {"valid": False, "reason": "Image dimensions exceed the safe processing limit."}
                image.seek(0)
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                image.seek(0)
                image = ImageOps.exif_transpose(image)
                image.load()
                image = image.convert("RGB")
                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS:
                    return None, {"valid": False, "reason": "Image dimensions exceed the safe processing limit."}
                canonical = BytesIO()
                image.save(canonical, format="JPEG", quality=95, optimize=True)
                gray = image.convert("L")
                gray.thumbnail((512, 512), Image.Resampling.BILINEAR)
                grayscale = np.asarray(gray, dtype=np.float32).copy()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None, {"valid": False, "reason": "Unreadable or unsafe image. Choose a supported raster image."}
    if width < 160 or height < 160:
        return None, {"valid": False, "reason": "Image must be at least 160 × 160 pixels."}
    brightness = float(grayscale.mean()) / 255.0
    contrast = float(grayscale.std()) / 255.0
    horizontal_laplacian = grayscale[:, 2:] - 2 * grayscale[:, 1:-1] + grayscale[:, :-2]
    vertical_laplacian = grayscale[2:, :] - 2 * grayscale[1:-1, :] + grayscale[:-2, :]
    sharpness = float((horizontal_laplacian.var() + vertical_laplacian.var()) / 2.0)
    warnings_found = []
    if brightness < .12:
        warnings_found.append("very_dark")
    elif brightness > .92:
        warnings_found.append("overexposed")
    if contrast < .08:
        warnings_found.append("low_contrast")
    if sharpness < 8.0:
        warnings_found.append("possible_blur")
    return canonical.getvalue(), {"valid": True, "width": width, "height": height, "format": "JPEG", "quality": {"brightness_mean": round(brightness, 4), "contrast_std": round(contrast, 4), "sharpness_laplacian_variance": round(sharpness, 4), "warnings": warnings_found, "note": "Heuristic capture-quality flags only; they do not locate a muzzle or validate identity."}}


def inspect_image(raw: bytes) -> dict:
    """Validate any installed Pillow-supported raster format by its contents."""
    _canonical, quality = process_image(raw)
    return quality
