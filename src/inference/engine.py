"""Lazy, versioned embedding inference for the local prototype.

This embeds the supplied full image. It does not detect or crop a muzzle; keep
the API private and do not use these results for consequential decisions.
"""
from functools import lru_cache
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from src.config import get_settings
from src.security.crypto import data_key_bytes
from src.inference.decision import decide


def _load_torch():
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("Install the ML dependencies before enabling biometric inference.") from exc
    return torch


@lru_cache(maxsize=2)
def _load_artifacts(model_path: str, mtime_ns: int, calibration_path: str, calibration_mtime_ns: int):
    torch = _load_torch()
    from src.models.embedding_model import build_model

    checkpoint = torch.load(model_path, map_location="cpu", weights_only=True)
    if checkpoint.get("format") != "muzzleid-embedding-v1":
        raise RuntimeError("Unsupported model checkpoint format.")
    model = build_model(pretrained=False)
    model.load_state_dict(checkpoint["state_dict"], strict=True)
    model.eval()
    with open(calibration_path, "r", encoding="utf-8") as stream:
        calibration = json.load(stream)
    if calibration.get("format") != "muzzleid-calibration-v1" or calibration.get("model_version") != checkpoint.get("model_version"):
        raise RuntimeError("Model and calibration artifacts do not match.")
    if calibration.get("status") != "prototype-calibrated":
        raise RuntimeError("Calibration artifact is not approved for prototype matching.")
    review = float(calibration.get("review_threshold", float("nan")))
    verified = float(calibration.get("verified_threshold", float("nan")))
    if not math.isfinite(review) or not math.isfinite(verified) or not -1 <= review <= verified <= 1:
        raise RuntimeError("Calibration decision thresholds are invalid.")
    return model, calibration, checkpoint["model_version"], checkpoint.get("image_size", 224)


def artifacts():
    cfg = get_settings()
    calibration_path = cfg["calibration_path"]
    model_path = cfg["model_path"]
    if not model_path.is_file() or not calibration_path.is_file():
        return None
    return _load_artifacts(str(model_path), model_path.stat().st_mtime_ns, str(calibration_path), calibration_path.stat().st_mtime_ns)


def model_state():
    try:
        data_key_bytes(get_settings().get("data_key", ""))
        loaded = artifacts()
        if loaded is None:
            return {"available": False, "reason": "A trained checkpoint and matching calibration file are required."}
        _, calibration, version, _ = loaded
        return {"available": True, "model_version": version, "calibration_status": calibration["status"], "limitations": calibration.get("limitations", [])}
    except Exception as exc:
        return {"available": False, "reason": str(exc)}


def faiss_available() -> bool:
    from src.inference.faiss_gallery import faiss_available as check_faiss
    return check_faiss()


def embed_images(raw_images: list[bytes]):
    loaded = artifacts()
    if loaded is None:
        raise RuntimeError("A trained checkpoint and matching calibration file are required.")
    torch = _load_torch()
    model, calibration, version, image_size = loaded
    batch = []
    for raw in raw_images:
        with Image.open(__import__("io").BytesIO(raw)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB").resize((image_size, image_size), Image.Resampling.BILINEAR)
        arr = np.asarray(image, dtype=np.float32) / 255.0
        arr = (arr - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array([0.229, 0.224, 0.225], dtype=np.float32)
        batch.append(np.transpose(arr, (2, 0, 1)))
    with torch.inference_mode():
        vectors = model(torch.from_numpy(np.stack(batch))).cpu().numpy().astype(np.float32)
    return [vector.tolist() for vector in vectors], version, calibration


def match_embedding(vector: list[float], gallery: list[tuple[str, str]], *, index_path=None, key=None):
    """Match per-animal prototypes with an encrypted, DB-recoverable FAISS index."""
    loaded = artifacts()
    if loaded is None:
        raise RuntimeError("A trained checkpoint and matching calibration file are required.")
    _, calibration, version, _ = loaded
    from src.inference.faiss_gallery import match_gallery
    settings = get_settings()
    if index_path is None:
        index_path = settings["index_path"]
    if key is None:
        key = data_key_bytes(settings.get("data_key", ""))
    animal_id, score = match_gallery(vector, gallery, index_path=index_path, encryption_key=key, model_version=version)
    if animal_id is None:
        return None, None, "UNKNOWN"
    verified = float(calibration["verified_threshold"])
    review = float(calibration["review_threshold"])
    decision = decide(score, verified_threshold=verified, review_threshold=review)
    return animal_id, score, decision
