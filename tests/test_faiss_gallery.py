import json

import numpy as np
import faiss  # Require the actual index dependency in the release test environment.
import pytest
from src.inference import faiss_gallery


def vector(position):
    value = np.zeros(512, dtype=np.float32)
    value[position] = 1
    return value.tolist()


def test_encrypted_faiss_index_persists_identity_mapping_and_recovers(tmp_path):
    key = bytes(range(32))
    path = tmp_path / "gallery.index.enc"
    gallery = [("animal-a", json.dumps(vector(1))), ("animal-b", json.dumps(vector(2)))]
    animal_id, score = faiss_gallery.match_gallery(vector(2), gallery, index_path=path, encryption_key=key, model_version="v1")
    assert (animal_id, score) == ("animal-b", pytest.approx(1.0))
    persisted = path.read_bytes()
    assert b"animal-a" not in persisted and b"animal-b" not in persisted

    faiss_gallery._CACHE = None  # Simulate a service process restart.
    animal_id, score = faiss_gallery.match_gallery(vector(2), gallery, index_path=path, encryption_key=key, model_version="v1")
    assert (animal_id, score) == ("animal-b", pytest.approx(1.0))
    assert path.read_bytes() == persisted

    path.write_bytes(b"corrupt-index")
    faiss_gallery._CACHE = None
    animal_id, score = faiss_gallery.match_gallery(vector(2), gallery, index_path=path, encryption_key=key, model_version="v1")
    assert (animal_id, score) == ("animal-b", pytest.approx(1.0))
    assert path.read_bytes() != b"corrupt-index"


def test_gallery_changes_invalidate_index_and_invalid_vectors_fail(tmp_path):
    key = bytes(range(32))
    path = tmp_path / "gallery.index.enc"
    first = [("animal-a", json.dumps(vector(1)))]
    faiss_gallery._CACHE = None
    faiss_gallery.match_gallery(vector(1), first, index_path=path, encryption_key=key, model_version="v1")
    first_content = path.read_bytes()
    changed = first + [("animal-b", json.dumps(vector(2)))]
    assert faiss_gallery.match_gallery(vector(2), changed, index_path=path, encryption_key=key, model_version="v1")[0] == "animal-b"
    assert path.read_bytes() != first_content
    with pytest.raises(ValueError, match="Stored gallery embedding"):
        faiss_gallery.match_gallery(vector(1), [("animal-a", json.dumps([float("nan")] * 512))], index_path=path, encryption_key=key, model_version="v1")
