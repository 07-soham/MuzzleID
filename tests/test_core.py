from io import BytesIO

import pytest
from PIL import Image

from src.inference.decision import decide
from src.inference.quality import inspect_image, process_image


def image_bytes(size=(256, 256)):
    buffer = BytesIO()
    Image.new("RGB", size, color=(120, 90, 70)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_quality_accepts_readable_image():
    result = inspect_image(image_bytes())
    assert result["valid"] is True
    assert result["width"] == 256
    assert 0 <= result["quality"]["brightness_mean"] <= 1
    assert "possible_blur" in result["quality"]["warnings"]


def test_quality_rejects_small_and_corrupt_images():
    assert inspect_image(image_bytes((64, 64)))["valid"] is False
    assert inspect_image(b"not an image")["valid"] is False


@pytest.mark.parametrize("source_format", ["BMP", "GIF", "TIFF", "PNG", "WEBP"])
def test_supported_raster_formats_are_normalized_to_jpeg(source_format):
    source = BytesIO()
    Image.new("RGB", (256, 256), color=(120, 90, 70)).save(source, format=source_format)

    normalized, result = process_image(source.getvalue())

    assert result["valid"] is True
    assert result["format"] == "JPEG"
    assert normalized[:3] == b"\xff\xd8\xff"
    with Image.open(BytesIO(normalized)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (256, 256)


def test_decision_requires_explicit_valid_thresholds():
    assert decide(.91, verified_threshold=.9, review_threshold=.75) == "VERIFIED"
    assert decide(.8, verified_threshold=.9, review_threshold=.75) == "REVIEW"
    assert decide(.5, verified_threshold=.9, review_threshold=.75) == "UNKNOWN"
    assert decide(-.2, verified_threshold=.1, review_threshold=-.3) == "REVIEW"
    with pytest.raises(ValueError):
        decide(.8, verified_threshold=.7, review_threshold=.75)
    with pytest.raises(ValueError):
        decide(float("nan"), verified_threshold=.9, review_threshold=.75)
