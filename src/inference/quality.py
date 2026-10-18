"""Image safety and basic acquisition quality checks."""
from io import BytesIO
import warnings
import numpy as np
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_PIXELS = 20_000_000
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
                gray = image.convert("L")
                gray.thumbnail((512, 512), Image.Resampling.BILINEAR)
                grayscale = np.asarray(gray, dtype=np.float32).copy()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return {"valid": False, "reason": "Unreadable or unsafe image."}
    if width < 160 or height < 160:
        return {"valid": False, "reason": "Image must be at least 160 × 160 pixels."}
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
    return {"valid": True, "width": width, "height": height, "format": image_format, "quality": {"brightness_mean": round(brightness, 4), "contrast_std": round(contrast, 4), "sharpness_laplacian_variance": round(sharpness, 4), "warnings": warnings_found, "note": "Heuristic capture-quality flags only; they do not locate a muzzle or validate identity."}}
