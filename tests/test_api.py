from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.api import main as api
from src.database.models import Animal, AnimalImage, Base
from src.inference import engine as inference_engine


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
    settings = {"database_url": f"sqlite:///{database}", "model_path": tmp_path / "missing-model.pt", "calibration_path": tmp_path / "missing-calibration.json", "upload_dir": uploads, "index_path": tmp_path / "gallery.index.enc", "max_upload_bytes": 12 * 1024 * 1024, "max_registration_bytes": 32 * 1024 * 1024, "operator_password": "test-operator-password-2026", "session_secret": "test-session-secret-value-long-enough-32-bytes", "data_key": "dGVzdC1rZXktbXV6emxlaWQtMzItYnl0ZXMta2V5ISE=", "cookie_secure": False}
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(inference_engine, "get_settings", lambda: settings)
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
    settings = {"database_url": f"sqlite:///{database}", "model_path": tmp_path / "missing.pt", "calibration_path": tmp_path / "missing.json", "upload_dir": tmp_path / "uploads", "index_path": tmp_path / "gallery.index.enc", "max_upload_bytes": 12 * 1024 * 1024, "max_registration_bytes": 32 * 1024 * 1024, "operator_password": "test-operator-password-2026", "session_secret": "test-session-secret-value-long-enough-32-bytes", "data_key": "dGVzdC1rZXktbXV6emxlaWQtMzItYnl0ZXMta2V5ISE=", "cookie_secure": False}
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    try:
        with TestClient(api.app) as client:
            assert client.get("/api/animals").status_code == 401
            assert client.get("/api/analytics").status_code == 401
            assert "Content-Security-Policy" in client.get("/").headers
            assert client.post("/api/login", json={"password": settings["operator_password"]}).status_code == 200
            assert client.get("/api/animals").status_code == 200
            assert client.post("/api/logout").status_code == 200
            assert client.get("/api/animals").status_code == 401
    finally:
        engine.dispose()


def test_startup_health_exposes_missing_model_artifact_reason(service):
    client, _, _, _ = service
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["database_available"] is True
    assert health.json()["model_available"] is False
    assert health.json()["verification_available"] is False
    assert "trained checkpoint" in health.json()["biometric_engine_reason"]


def test_animal_edit_protected_images_policy_alerts_analytics_and_delete(service):
    client, factory, uploads, _ = service
    first = client.post("/api/animals", headers={"Idempotency-Key": "registry-lifecycle-0001"}, data={"name": "=Formula cow", "ear_tag": "DUP-1"}, files=[("images", ("a.jpg", jpeg(), "image/jpeg"))])
    second = client.post("/api/animals", headers={"Idempotency-Key": "registry-lifecycle-0002"}, data={"name": "Second cow", "ear_tag": "DUP-1"}, files=[("images", ("b.jpg", jpeg(color=(10, 90, 180)), "image/jpeg"))])
    assert first.status_code == second.status_code == 200
    animal_id, second_id = first.json()["id"], second.json()["id"]

    detail = client.get(f"/api/animals/{animal_id}").json()
    image = detail["images"][0]
    preview = client.get(f"/api/animals/{animal_id}/images/{image['id']}")
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
    assert preview.headers["cache-control"] == "private, no-store"
    updated = client.patch(f"/api/animals/{animal_id}", json={"name": "Rani", "date_of_birth": "2022-01-01"})
    assert updated.status_code == 200
    assert client.get(f"/api/animals/{animal_id}").json()["name"] == "Rani"

    today = __import__("datetime").date.today()
    policy = client.post("/api/insurance/policies", json={"animal_id": animal_id, "provider": "Test insurer", "policy_number": "P-100", "coverage_amount": 1234.5, "start_date": today.isoformat(), "end_date": today.replace(year=today.year + 1).isoformat(), "notes": "Operator reference only"})
    assert policy.status_code == 201
    policy_id = policy.json()["id"]
    assert client.patch(f"/api/insurance/policies/{policy_id}", json={"status": "claim_reported"}).json()["status"] == "claim_reported"
    assert len(client.get("/api/insurance/policies").json()) == 1

    alerts = client.get("/api/alerts").json()
    duplicate_alerts = [row for row in alerts if row["category"] == "duplicate_ear_tag"]
    assert len(duplicate_alerts) == 2
    assert client.patch(f"/api/alerts/{duplicate_alerts[0]['id']}", json={"status": "resolved"}).json()["status"] == "resolved"
    analytics = client.get("/api/analytics").json()
    assert analytics["total_animals"] == 2
    assert analytics["open_alerts"] == 1

    assert client.patch(f"/api/animals/{animal_id}", json={"name": "=Formula cow"}).status_code == 200
    report = client.get("/api/reports/animals.csv")
    assert report.status_code == 200 and "'=Formula cow" in report.text
    assert "\r\n" in report.text
    assert client.delete(f"/api/animals/{animal_id}").status_code == 204
    assert not (uploads / f"{image['id']}.enc").exists()
    assert client.get(f"/api/animals/{animal_id}").status_code == 404
    assert client.get("/api/insurance/policies").json() == []
    assert client.delete(f"/api/animals/{second_id}").status_code == 204
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Animal)) == 0


def test_insurance_validation_and_settings_status(service):
    client, _, _, _ = service
    record = client.post("/api/animals", headers={"Idempotency-Key": "insurance-validation-001"}, files=[("images", ("cow.jpg", jpeg(), "image/jpeg"))]).json()
    today = __import__("datetime").date.today()
    invalid = client.post("/api/insurance/policies", json={"animal_id": record["id"], "provider": "P", "policy_number": "P-1", "coverage_amount": -1, "start_date": today.isoformat(), "end_date": today.isoformat()})
    assert invalid.status_code == 422
    mismatch = client.post("/api/insurance/policies", json={"animal_id": "missing", "provider": "P", "policy_number": "P-2", "coverage_amount": 0, "start_date": today.isoformat(), "end_date": today.isoformat()})
    assert mismatch.status_code == 404
    settings = client.get("/api/settings").json()
    assert settings["operator_password_configured"] and settings["session_secret_configured"] and settings["data_encryption_configured"]
    assert settings["model_available"] is False


def test_storage_recovery_disables_broken_profile_and_removes_old_orphans(service):
    client, factory, uploads, _ = service
    response = client.post("/api/animals", headers={"Idempotency-Key": "storage-recovery-0001"}, files=[("images", ("cow.jpg", jpeg(), "image/jpeg"))])
    animal_id = response.json()["id"]
    image_id = client.get(f"/api/animals/{animal_id}").json()["images"][0]["id"]
    (uploads / f"{image_id}.enc").unlink()
    orphan = uploads / "orphan.enc"
    orphan.write_bytes(b"unreferenced encrypted payload")
    old_time = __import__("time").time() - 2 * 86400
    __import__("os").utime(orphan, (old_time, old_time))
    api.reconcile_storage()
    assert not orphan.exists()
    with factory() as db:
        assert db.get(Animal, animal_id).status == "storage_error"
        assert db.scalar(select(func.count()).select_from(AnimalImage).where(AnimalImage.animal_id == animal_id)) == 1
