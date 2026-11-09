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


def test_hnsw_returns_three_distinct_animals_and_aggregates_references(tmp_path):
    key = bytes(range(32))
    path = tmp_path / "gallery.index.enc"
    query = np.zeros(512, dtype=np.float32)
    query[0] = 1

    def near_query(position, strength):
        value = np.zeros(512, dtype=np.float32)
        value[0] = strength
        value[position] = (1 - strength * strength) ** .5
        return value.tolist()

    gallery = [("animal-a", f"a-{i}", json.dumps(near_query(i + 1, .99 - i * .001))) for i in range(8)]
    gallery += [("animal-b", "b-1", json.dumps(near_query(20, .91)))]
    gallery += [("animal-c", "c-1", json.dumps(near_query(21, .86)))]
    gallery += [("animal-d", "d-1", json.dumps(near_query(22, .80)))]
    faiss_gallery._CACHE = None

    candidates = faiss_gallery.search_gallery(query, gallery, index_path=path, encryption_key=key, model_version="v2", k=3)

    assert [item["animal_id"] for item in candidates] == ["animal-a", "animal-b", "animal-c"]
    assert len({item["animal_id"] for item in candidates}) == 3
    assert candidates[0]["support_count"] == 8
    assert path.is_file()


def test_incremental_reference_add_is_available_without_rebuilding_gallery(tmp_path):
    key = bytes(range(32))
    path = tmp_path / "gallery.index.enc"
    first = [("animal-a", "image-a", json.dumps(vector(0)))]
    faiss_gallery._CACHE = None
    faiss_gallery.ensure_gallery_index(first, index_path=path, encryption_key=key, model_version="v1")
    faiss_gallery.add_gallery_entries([("animal-b", "image-b", vector(1))], model_version="v1", index_path=path, encryption_key=key)

    candidates = faiss_gallery.search_cached_gallery(vector(1), model_version="v1", k=2)

    assert candidates[0]["animal_id"] == "animal-b"
