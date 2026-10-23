"""Exercise real model inference, API persistence, restart recovery, and index rebuild."""
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from src.config import ROOT, get_settings
from src.database.models import Animal, AnimalImage, ImageEmbedding, Base, Verification
from src.training.train import collect_partitions
from src.api import main as api
from src.inference import engine as inference_engine
from src.inference import faiss_gallery
from src.inference.quality import inspect_image


def jpeg(path):
    return path.read_bytes()


def assert_status(response, expected, message):
    if response.status_code != expected:
        raise AssertionError(f"{message}: got HTTP {response.status_code}: {response.text[:300]}")


def main():
    model_path = ROOT / "models" / "best_model.pt"
    calibration_path = ROOT / "models" / "calibration.json"
    if not model_path.is_file() or not calibration_path.is_file():
        raise SystemExit("A trained checkpoint and matching calibration are required. Run src.training.train first.")
    source = get_settings()
    if len(source.get("operator_password", "")) < 12 or len(source.get("session_secret", "")) < 32 or not source.get("data_key"):
        raise SystemExit("Configure the operator password, session secret, and data key in the private .env file.")

    partitions = collect_partitions(ROOT / "data" / "train" / "dataset", 2026)
    known = next(value for value in partitions["test"].values() if len(value["gallery"]) >= 2 and value["query"])
    unknown = next(value for value in partitions["test"].values() if value is not known and value["query"])
    known_paths = [path for path, _ in known["gallery"] if inspect_image(path.read_bytes())["valid"]][:2]
    unknown_path = next((path for path, _ in unknown["query"] if inspect_image(path.read_bytes())["valid"]), None)
    if len(known_paths) < 2 or unknown_path is None:
        raise SystemExit("The identity-disjoint test partition does not contain enough API-acceptable images.")

    with tempfile.TemporaryDirectory(prefix="muzzleid-release-e2e-") as temp:
        root = Path(temp)
        database = root / "release.sqlite"
        upload_dir = root / "uploads"
        index_path = root / "gallery.index.enc"
        db_engine = create_engine(f"sqlite:///{database}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(db_engine)
        factory = sessionmaker(bind=db_engine, expire_on_commit=False)
        settings = dict(source)
        settings.update({"database_url": f"sqlite:///{database}", "model_path": model_path, "calibration_path": calibration_path, "upload_dir": upload_dir, "index_path": index_path, "max_upload_bytes": 12 * 1024 * 1024, "max_registration_bytes": 32 * 1024 * 1024})
        api.SessionFactory = factory
        api.get_settings = lambda: settings
        inference_engine.get_settings = lambda: settings
        faiss_gallery._CACHE = None

        headers = {"Idempotency-Key": "release-e2e-registration-0001"}
        registration_files = [("images", (f"reference-{i}.jpg", jpeg(path), "image/jpeg")) for i, path in enumerate(known_paths)]
        with TestClient(api.app) as client:
            login = client.post("/api/login", json={"password": settings["operator_password"]})
            assert_status(login, 200, "operator login")
            health = client.get("/api/health")
            assert_status(health, 200, "startup biometric health")
            if not health.json()["verification_available"] or health.json()["model_version"] != inference_engine.artifacts()[2]:
                raise AssertionError(f"Biometric engine did not initialize on startup: {health.json()}")
            registered = client.post("/api/animals", headers=headers, data={"name": "Release test animal", "ear_tag": "E2E-001", "breed": "", "sex": "", "date_of_birth": ""}, files=registration_files)
            assert_status(registered, 200, "real model registration")
            animal_id = registered.json()["id"]
            if registered.json()["status"] != "prototype_ready":
                raise AssertionError(f"Registration did not generate embeddings: {registered.json()['status']}")
            replay = client.post("/api/animals", headers=headers, data={"name": "Ignored replay"}, files=[("images", ("ignored.jpg", jpeg(known_paths[0]), "image/jpeg"))])
            assert_status(replay, 200, "registration idempotency replay")
            if replay.json()["id"] != animal_id or not replay.json()["replayed"]:
                raise AssertionError("Registration retry was not idempotent.")

            # Exercise migration from a pending/stale profile to the current model version.
            with factory.begin() as db:
                animal = db.get(Animal, animal_id)
                db.query(ImageEmbedding).filter(ImageEmbedding.image_id.in_([image.id for image in animal.images])).delete(synchronize_session=False)
                animal.status = "pending_model"
            if client.get(f"/api/animals/{animal_id}").json()["status"] != "pending_model":
                raise AssertionError("Profile status did not reflect missing current-model embeddings.")
            reindexed = client.post(f"/api/animals/{animal_id}/reindex")
            assert_status(reindexed, 200, "current-model reindex")
            if reindexed.json()["status"] != "prototype_ready":
                raise AssertionError(f"Animal reindex failed: {reindexed.json()}")

            known_result = client.post("/api/verify", files={"image": ("known.jpg", jpeg(known_paths[0]), "image/jpeg")})
            assert_status(known_result, 200, "known animal verification")
            if known_result.json()["decision"] != "VERIFIED" or known_result.json()["animal_id"] != animal_id:
                raise AssertionError(f"Known animal failed to verify: {known_result.json()}")

            unknown_result = client.post("/api/verify", files={"image": ("unknown.jpg", jpeg(unknown_path), "image/jpeg")})
            assert_status(unknown_result, 200, "unknown animal verification")
            if unknown_result.json()["decision"] == "VERIFIED":
                raise AssertionError(f"Identity-disjoint unknown animal was falsely accepted: {unknown_result.json()}")

            invalid = client.post("/api/verify", files={"image": ("invalid.jpg", b"not an image", "image/jpeg")})
            assert_status(invalid, 422, "invalid image rejection")
            if not index_path.is_file():
                raise AssertionError("FAISS index was not persisted.")
            saved_index = index_path.read_bytes()
            detail = client.get(f"/api/animals/{animal_id}")
            assert_status(detail, 200, "persisted animal details")
            preview = client.get(f"/api/animals/{animal_id}/images/{detail.json()['images'][0]['id']}")
            assert_status(preview, 200, "authenticated encrypted image retrieval")

        # A new TestClient lifetime uses the same persistent DB and encrypted index.
        faiss_gallery._CACHE = None
        with TestClient(api.app) as client:
            assert_status(client.post("/api/login", json={"password": settings["operator_password"]}), 200, "restart login")
            if not index_path.is_file() or not api.ENGINE_STARTUP_STATE["initialized"] or api.ENGINE_STARTUP_STATE["gallery_identities"] != 1:
                raise AssertionError("Startup did not restore the encrypted FAISS gallery and identity mapping from persistent records.")
            records = client.get("/api/animals", params={"q": "E2E-001"}).json()
            if records["total"] != 1 or records["items"][0]["id"] != animal_id:
                raise AssertionError("Animal record did not survive service restart.")
            recovered = client.post("/api/verify", files={"image": ("known.jpg", jpeg(known_paths[0]), "image/jpeg")})
            assert_status(recovered, 200, "post-restart verification")
            if recovered.json()["decision"] != "VERIFIED" or index_path.read_bytes() != saved_index:
                raise AssertionError("The encrypted FAISS index was not recovered after restart.")

            index_path.write_bytes(b"corrupt cache")
            faiss_gallery._CACHE = None
            rebuilt = client.post("/api/verify", files={"image": ("known.jpg", jpeg(known_paths[0]), "image/jpeg")})
            assert_status(rebuilt, 200, "corrupted-index rebuild verification")
            if rebuilt.json()["decision"] != "VERIFIED" or index_path.read_bytes() == b"corrupt cache":
                raise AssertionError("Corrupted FAISS cache was not rebuilt from the database.")
            history = client.get("/api/history").json()
            if len(history) < 4:
                raise AssertionError("Verification events did not persist across restart.")

        with factory() as db:
            if db.scalar(select(func.count()).select_from(Animal)) != 1 or db.scalar(select(func.count()).select_from(Verification)) < 4:
                raise AssertionError("Persistent E2E database counts do not match the exercised workflows.")
        print({"known_decision": known_result.json()["decision"], "known_score": round(known_result.json()["similarity"], 6), "unknown_decision": unknown_result.json()["decision"], "unknown_score": round(unknown_result.json()["similarity"], 6), "invalid_image_http": invalid.status_code, "reindex": "passed", "record_survived_restart": True, "encrypted_index_recovered": True, "corrupt_index_rebuilt": True})
        db_engine.dispose()


if __name__ == "__main__":
    main()
