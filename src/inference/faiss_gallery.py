"""Encrypted, versioned FAISS gallery cache with the database as source of truth."""
from hashlib import sha256
import json
from pathlib import Path
import threading
import uuid

import numpy as np

from src.security.crypto import decrypt_bytes, encrypt_bytes

_LOCK = threading.RLock()
_CACHE = None


def faiss_available() -> bool:
    try:
        import faiss  # noqa: F401
        return True
    except ImportError:
        return False


def ensure_gallery_index(gallery, *, index_path: Path, encryption_key: bytes, model_version: str):
    """Load or build the durable FAISS index from (animal id, JSON vector) rows."""
    global _CACHE
    if not gallery:
        return None, []
    try:
        import faiss
    except ImportError as exc:
        raise RuntimeError("Install faiss-cpu to use the biometric gallery.") from exc
    image_vectors: dict[str, list[np.ndarray]] = {}
    for animal_id, encoded in gallery:
        vector = np.asarray(json.loads(encoded), dtype=np.float32)
        if vector.shape != (512,) or not np.isfinite(vector).all():
            raise ValueError("Stored gallery embedding has an invalid shape or value.")
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-12:
            raise ValueError("Stored gallery embedding has zero magnitude.")
        image_vectors.setdefault(animal_id, []).append(vector / norm)
    animal_ids = sorted(image_vectors)
    centers = []
    for animal_id in animal_ids:
        center = np.mean(image_vectors[animal_id], axis=0).astype(np.float32)
        norm = float(np.linalg.norm(center))
        if norm <= 1e-12:
            raise ValueError("An animal prototype has zero magnitude.")
        centers.append(center / norm)
    matrix = np.ascontiguousarray(np.stack(centers), dtype=np.float32)
    fingerprint = sha256()
    fingerprint.update(model_version.encode("utf-8"))
    for animal_id, center in zip(animal_ids, matrix):
        fingerprint.update(animal_id.encode("utf-8"))
        fingerprint.update(center.tobytes(order="C"))
    fingerprint = fingerprint.hexdigest()
    cache_key = (str(index_path.resolve()), model_version, fingerprint)

    with _LOCK:
        if _CACHE is not None and _CACHE[0] == cache_key:
            index, indexed_ids = _CACHE[1], _CACHE[2]
        else:
            index = _load_index(faiss, index_path, encryption_key, model_version, fingerprint, animal_ids)
            if index is None:
                index = faiss.IndexFlatIP(matrix.shape[1])
                index.add(matrix)
                _save_index(faiss, index, index_path, encryption_key, model_version, fingerprint, animal_ids)
            indexed_ids = animal_ids
            _CACHE = (cache_key, index, indexed_ids)
    return index, indexed_ids


def match_gallery(query, gallery, *, index_path: Path, encryption_key: bytes, model_version: str):
    """Return (animal id, cosine score) for gallery rows (animal id, JSON vector)."""
    if not gallery:
        return None, None
    query_vector = np.asarray(query, dtype=np.float32)
    if query_vector.ndim != 1 or query_vector.size != 512 or not np.isfinite(query_vector).all():
        raise ValueError("Query embedding has an invalid shape or value.")
    query_norm = float(np.linalg.norm(query_vector))
    if query_norm <= 1e-12:
        raise ValueError("Query embedding has zero magnitude.")
    query_vector /= query_norm
    index, indexed_ids = ensure_gallery_index(gallery, index_path=index_path, encryption_key=encryption_key, model_version=model_version)

    similarities, positions = index.search(np.ascontiguousarray(query_vector.reshape(1, -1)), 1)
    position = int(positions[0, 0])
    if position < 0 or position >= len(indexed_ids):
        return None, None
    return indexed_ids[position], float(similarities[0, 0])


def _load_index(faiss, path, key, version, fingerprint, animal_ids):
    if not path.is_file():
        return None
    try:
        header_raw, encrypted = path.read_bytes().split(b"\n", 1)
        header = json.loads(header_raw.decode("utf-8"))
        expected_header = {"format": "muzzleid-faiss-index-v1", "model_version": version, "fingerprint": fingerprint}
        if header != expected_header:
            return None
        payload = json.loads(decrypt_bytes(encrypted, key, f"muzzleid-faiss-index:{version}:{fingerprint}"))
        if payload.get("animal_ids") != animal_ids:
            return None
        index_data = np.frombuffer(__import__("base64").urlsafe_b64decode(payload["index"].encode("ascii")), dtype=np.uint8)
        index = faiss.deserialize_index(index_data)
        if index.d != 512 or index.ntotal != len(animal_ids):
            return None
        return index
    except Exception:
        # Corruption, key rotation, or a version mismatch is recoverable from DB rows.
        return None


def _save_index(faiss, index, path, key, version, fingerprint, animal_ids):
    import base64
    payload = json.dumps({"animal_ids": animal_ids, "index": base64.urlsafe_b64encode(bytes(faiss.serialize_index(index))).decode("ascii")}, separators=(",", ":")).encode("utf-8")
    encrypted = encrypt_bytes(payload, key, f"muzzleid-faiss-index:{version}:{fingerprint}")
    header = json.dumps({"format": "muzzleid-faiss-index-v1", "model_version": version, "fingerprint": fingerprint}, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(header + b"\n" + encrypted)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
