"""Encrypted HNSW reference-image index with animal-level candidate aggregation."""
from hashlib import sha256
import json
from pathlib import Path
import threading
import uuid

import numpy as np

from src.security.crypto import decrypt_bytes, encrypt_bytes

_LOCK = threading.RLock()
_CACHE = None
_HNSW_M = 32
_EF_CONSTRUCTION = 160
_EF_SEARCH = 256
_INTERNAL_CANDIDATE_MULTIPLIER = 20


def faiss_available() -> bool:
    try:
        import faiss  # noqa: F401
        return True
    except ImportError:
        return False


def _parse_gallery(gallery):
    """Normalize (animal, vector) and (animal, image, vector) rows."""
    rows = {}
    for position, row in enumerate(gallery):
        if len(row) == 2:
            animal_id, encoded = row
            image_id = f"legacy-ref-{position}"
        elif len(row) == 3:
            animal_id, image_id, encoded = row
        else:
            raise ValueError("Gallery rows must contain animal id, optional image id, and vector.")
        vector = np.asarray(json.loads(encoded) if isinstance(encoded, str) else encoded, dtype=np.float32)
        if vector.shape != (512,) or not np.isfinite(vector).all():
            raise ValueError("Stored gallery embedding has an invalid shape or value.")
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-12:
            raise ValueError("Stored gallery embedding has zero magnitude.")
        rows[(str(animal_id), str(image_id))] = vector / norm
    return [(animal_id, image_id, vector) for (animal_id, image_id), vector in sorted(rows.items())]


def _fingerprint(entries, model_version):
    digest = sha256(model_version.encode("utf-8"))
    for animal_id, image_id, vector in entries:
        digest.update(animal_id.encode("utf-8"))
        digest.update(image_id.encode("utf-8"))
        digest.update(vector.tobytes(order="C"))
    return digest.hexdigest()


def ensure_gallery_index(gallery, *, index_path: Path, encryption_key: bytes, model_version: str):
    """Load or rebuild a persisted HNSW index over individual reference images."""
    global _CACHE
    entries = _parse_gallery(gallery)
    if not entries:
        with _LOCK:
            _CACHE = None
        return None, []
    try:
        import faiss
    except ImportError as exc:
        raise RuntimeError("Install faiss-cpu to use the biometric gallery.") from exc
    fingerprint = _fingerprint(entries, model_version)
    cache_key = (str(index_path.resolve()), model_version, fingerprint)
    with _LOCK:
        if _CACHE is not None and _CACHE["key"] == cache_key:
            return _CACHE["index"], sorted(set(_CACHE["animal_ids"]))
        index = _load_index(faiss, index_path, encryption_key, model_version, fingerprint, entries)
        if index is None:
            matrix = np.ascontiguousarray(np.stack([row[2] for row in entries]), dtype=np.float32)
            index = faiss.IndexHNSWFlat(matrix.shape[1], _HNSW_M, faiss.METRIC_INNER_PRODUCT)
            index.hnsw.efConstruction = _EF_CONSTRUCTION
            index.hnsw.efSearch = _EF_SEARCH
            index.add(matrix)
            _save_index(faiss, index, index_path, encryption_key, model_version, fingerprint, entries)
        _CACHE = {
            "key": cache_key,
            "index": index,
            "animal_ids": [row[0] for row in entries],
            "image_ids": [row[1] for row in entries],
            "model_version": model_version,
            "index_path": str(index_path.resolve()),
            "encryption_key": encryption_key,
            "dirty": False,
        }
        return index, sorted(set(_CACHE["animal_ids"]))


def search_cached_gallery(query, *, model_version: str, k: int = 3):
    """Search the already loaded index without rescanning/decrypting the gallery."""
    if _CACHE is None or _CACHE["model_version"] != model_version:
        return None
    return _search_cache(query, k)


def search_gallery(query, gallery, *, index_path: Path, encryption_key: bytes, model_version: str, k: int = 3):
    ensure_gallery_index(gallery, index_path=index_path, encryption_key=encryption_key, model_version=model_version)
    return _search_cache(query, k)


def _search_cache(query, k):
    if k < 1:
        raise ValueError("Candidate count must be positive.")
    if _CACHE is None:
        return []
    query_vector = np.asarray(query, dtype=np.float32)
    if query_vector.ndim != 1 or query_vector.size != 512 or not np.isfinite(query_vector).all():
        raise ValueError("Query embedding has an invalid shape or value.")
    query_norm = float(np.linalg.norm(query_vector))
    if query_norm <= 1e-12:
        raise ValueError("Query embedding has zero magnitude.")
    query_vector /= query_norm
    index = _CACHE["index"]
    total = int(index.ntotal)
    if total == 0:
        return []
    retrieved = min(total, max(k * _INTERNAL_CANDIDATE_MULTIPLIER, 32))
    by_animal = {}
    while True:
        index.hnsw.efSearch = max(_EF_SEARCH, retrieved)
        similarities, positions = index.search(np.ascontiguousarray(query_vector.reshape(1, -1)), retrieved)
        by_animal.clear()
        for score, position in zip(similarities[0], positions[0]):
            position = int(position)
            if position < 0 or position >= len(_CACHE["animal_ids"]):
                continue
            animal_id = _CACHE["animal_ids"][position]
            reference = {"image_id": _CACHE["image_ids"][position], "similarity": float(score)}
            current = by_animal.setdefault(animal_id, [])
            current.append(reference)
        if len(by_animal) >= k or retrieved >= total:
            break
        retrieved = min(total, retrieved * 2)

    candidates = []
    for animal_id, references in by_animal.items():
        references.sort(key=lambda item: item["similarity"], reverse=True)
        candidates.append({
            "animal_id": animal_id,
            "similarity": references[0]["similarity"],
            "reference_image_id": references[0]["image_id"],
            "support_count": len(references),
            "reference_image_ids": [item["image_id"] for item in references[:3]],
            "support_similarities": [item["similarity"] for item in references],
        })
    candidates.sort(key=lambda item: item["similarity"], reverse=True)
    return candidates[:k]


def add_gallery_entries(entries, *, model_version: str, index_path: Path, encryption_key: bytes):
    """Incrementally add newly committed image embeddings to the live HNSW cache.

    The encrypted database remains authoritative. The next process start rebuilds
    any dirty index from database rows, so an interrupted checkpoint cannot lose
    registered references.
    """
    global _CACHE
    if not entries:
        return
    parsed = _parse_gallery(entries)
    with _LOCK:
        if _CACHE is None:
            return
        if _CACHE["model_version"] != model_version:
            return
        existing = set(_CACHE["image_ids"])
        additions = [row for row in parsed if row[1] not in existing]
        if not additions:
            return
        matrix = np.ascontiguousarray(np.stack([row[2] for row in additions]), dtype=np.float32)
        _CACHE["index"].add(matrix)
        _CACHE["animal_ids"].extend(row[0] for row in additions)
        _CACHE["image_ids"].extend(row[1] for row in additions)
        _CACHE["dirty"] = True


def invalidate_gallery_index():
    """Drop the process cache after deletions or model-profile changes."""
    global _CACHE
    with _LOCK:
        _CACHE = None


def match_gallery(query, gallery, *, index_path: Path, encryption_key: bytes, model_version: str):
    """Backward-compatible best-candidate helper."""
    candidates = search_gallery(query, gallery, index_path=index_path, encryption_key=encryption_key, model_version=model_version, k=1)
    if not candidates:
        return None, None
    return candidates[0]["animal_id"], candidates[0]["similarity"]


def _load_index(faiss, path, key, version, fingerprint, entries):
    if not path.is_file():
        return None
    try:
        header_raw, encrypted = path.read_bytes().split(b"\n", 1)
        header = json.loads(header_raw.decode("utf-8"))
        expected_header = {"format": "muzzleid-faiss-hnsw-v2", "model_version": version, "fingerprint": fingerprint}
        if header != expected_header:
            return None
        payload = json.loads(decrypt_bytes(encrypted, key, f"muzzleid-faiss-index:{version}:{fingerprint}"))
        if payload.get("animal_ids") != [row[0] for row in entries] or payload.get("image_ids") != [row[1] for row in entries]:
            return None
        index_data = np.frombuffer(__import__("base64").urlsafe_b64decode(payload["index"].encode("ascii")), dtype=np.uint8)
        index = faiss.deserialize_index(index_data)
        if index.d != 512 or index.ntotal != len(entries) or not hasattr(index, "hnsw"):
            return None
        index.hnsw.efSearch = _EF_SEARCH
        return index
    except Exception:
        return None


def _save_index(faiss, index, path, key, version, fingerprint, entries):
    import base64
    payload = json.dumps({
        "animal_ids": [row[0] for row in entries],
        "image_ids": [row[1] for row in entries],
        "index": base64.urlsafe_b64encode(bytes(faiss.serialize_index(index))).decode("ascii"),
    }, separators=(",", ":")).encode("utf-8")
    encrypted = encrypt_bytes(payload, key, f"muzzleid-faiss-index:{version}:{fingerprint}")
    header = json.dumps({"format": "muzzleid-faiss-hnsw-v2", "model_version": version, "fingerprint": fingerprint}, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(header + b"\n" + encrypted)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
