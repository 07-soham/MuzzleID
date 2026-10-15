from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.api import main as api
from src.database.models import Animal, AnimalImage, Base


def jpeg(size=(320, 240), color=(70, 110, 80)):
    stream = BytesIO()
    Image.new("RGB", size, color).save(stream, format="JPEG")
    return stream.getvalue()


@pytest.fixture
def service(tmp_path, monkeypatch):
    database = tmp_path / "service.sqlite"
    engine = create_engine(f"sqlite:///{database}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(api, "SessionFactory", factory)
    settings = {"database_url": f"sqlite:///{database}", "model_path": tmp_path / "missing-model.pt", "calibration_path": tmp_path / "missing-calibration.json", "upload_dir": uploads, "max_upload_bytes": 12 * 1024 * 1024, "operator_password": "test-operator-password-2026", "session_secret": "test-session-secret-value-long-enough-32-bytes", "data_key": "dGVzdC1rZXktbXV6emxlaWQtMzItYnl0ZXMta2V5ISE=", "cookie_secure": False}
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    with TestClient(api.app) as client:
        login = client.post("/api/login", json={"password": settings["operator_password"]})
        assert login.status_code == 200
        yield client, factory, uploads, database
    engine.dispose()


def test_registration_persists_reference_images_and_replay_is_idempotent(service):
    client, factory, uploads, _ = service
    headers = {"Idempotency-Key": "register-request-0001"}
    files = [("images", ("muzzle.jpg", jpeg(), "image/jpeg")), ("images", ("muzzle-2.jpg", jpeg(color=(30, 90, 140)), "image/jpeg"))]
    response = client.post("/api/animals", headers=headers, data={"name": "Field sample", "ear_tag": "TEST-100", "breed": "Gir"}, files=files)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["persisted"] is True
    assert result["status"] == "pending_model"
    assert result["image_count"] == 2
    stored = list(uploads.iterdir())
    assert len(stored) == 2
    assert all(path.suffix == ".enc" and path.read_bytes().startswith(b"MZ01") for path in stored)
    replay = client.post("/api/animals", headers=headers, data={"name": "Should not duplicate"}, files=[("images", ("ignored.jpg", jpeg(color=(20, 50, 200)), "image/jpeg"))])
    assert replay.status_code == 200
    assert replay.json()["id"] == result["id"]
    assert replay.json()["replayed"] is True
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Animal)) == 1
        assert db.scalar(select(func.count()).select_from(AnimalImage)) == 2


def test_registration_rejects_invalid_and_duplicate_image_without_partial_records(service):
    client, factory, uploads, _ = service
    header = {"Idempotency-Key": "register-request-0002"}
    invalid = client.post("/api/animals", headers=header, files=[("images", ("bad.jpg", b"not image", "image/jpeg"))])
    assert invalid.status_code == 422
    data = jpeg()
    repeated = client.post("/api/animals", headers=header, files=[("images", ("a.jpg", data, "image/jpeg")), ("images", ("b.jpg", data, "image/jpeg"))])
    assert repeated.status_code == 422
    assert not list(uploads.iterdir())
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Animal)) == 0


def test_registry_search_details_and_unavailable_verification(service):
    client, _, _, _ = service
    response = client.post("/api/animals", headers={"Idempotency-Key": "register-request-0003"}, data={"name": "Rani", "ear_tag": "EAR-3"}, files=[("images", ("rani.jpg", jpeg(), "image/jpeg"))])
    animal_id = response.json()["id"]
    found = client.get("/api/animals", params={"q": "EAR-3", "sort": "name"})
    assert found.status_code == 200
    assert found.json()["total"] == 1
    detail = client.get(f"/api/animals/{animal_id}")
    assert detail.status_code == 200
    assert detail.json()["images"][0]["width"] == 320
    assert client.get("/api/animals/missing").status_code == 404
    invalid = client.post("/api/verify", files={"image": ("bad.jpg", b"invalid", "image/jpeg")})
    assert invalid.status_code == 422
    unavailable = client.post("/api/verify", files={"image": ("query.jpg", jpeg(), "image/jpeg")})
    assert unavailable.status_code == 503
    assert "trained model" in unavailable.json()["detail"]


def test_private_api_requires_operator_session(monkeypatch, tmp_path):
    database = tmp_path / "private.sqlite"
    engine = create_engine(f"sqlite:///{database}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(api, "SessionFactory", sessionmaker(bind=engine, expire_on_commit=False))
    settings = {"database_url": f"sqlite:///{database}", "model_path": tmp_path / "missing.pt", "calibration_path": tmp_path / "missing.json", "upload_dir": tmp_path / "uploads", "max_upload_bytes": 12 * 1024 * 1024, "operator_password": "test-operator-password-2026", "session_secret": "test-session-secret-value-long-enough-32-bytes", "data_key": "dGVzdC1rZXktbXV6emxlaWQtMzItYnl0ZXMta2V5ISE=", "cookie_secure": False}
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    try:
        with TestClient(api.app) as client:
            assert client.get("/api/animals").status_code == 401
            assert "Content-Security-Policy" in client.get("/").headers
            assert client.post("/api/login", json={"password": settings["operator_password"]}).status_code == 200
            assert client.get("/api/animals").status_code == 200
            assert client.post("/api/logout").status_code == 200
            assert client.get("/api/animals").status_code == 401
    finally:
        engine.dispose()
