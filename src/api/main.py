"""FastAPI service for MuzzleID. Biometric endpoints stay unavailable until a real model is configured."""
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import logging

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func, or_, text
from sqlalchemy.exc import IntegrityError

from src.config import ROOT, get_settings
from src.database.models import Animal, AnimalImage, RegistrationRequest, Verification, make_session_factory
from src.inference.quality import inspect_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("muzzleid")
SessionFactory = make_session_factory()


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_settings()["upload_dir"].mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="MuzzleID API", version="0.1.0", description="Muzzle biometric MVP. Decisions require a validated model and calibrated thresholds.", lifespan=lifespan)
static_dir = ROOT / "web"
if static_dir.exists():
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
def health():
    cfg = get_settings()
    database_available = False
    try:
        with SessionFactory() as db:
            db.execute(text("SELECT 1"))
        database_available = True
    except Exception:
        log.exception("Database health check failed")
    model_available = cfg["model_path"].is_file()
    return {
        "status": "ok" if database_available else "degraded",
        "database_available": database_available,
        "model_available": model_available,
        "detector_available": False,
        "index_available": False,
        "calibrated_thresholds_available": False,
        "decision_thresholds": "Not available",
        "mode": "MVP",
    }


@app.get("/api/animals")
def list_animals(q: str = Query("", max_length=120), offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100), sort: str = Query("created_at", pattern="^(created_at|name|ear_tag)$")):
    with SessionFactory() as db:
        statement = select(Animal)
        if q.strip():
            needle = f"%{q.strip()}%"
            statement = statement.where(or_(Animal.name.ilike(needle), Animal.ear_tag.ilike(needle), Animal.breed.ilike(needle), Animal.id.ilike(needle)))
        total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
        sort_column = {"created_at": Animal.created_at, "name": Animal.name, "ear_tag": Animal.ear_tag}[sort]
        order = sort_column.desc() if sort == "created_at" else sort_column.asc()
        animals = db.scalars(statement.order_by(order, Animal.id.asc()).offset(offset).limit(limit)).all()
        return {"items": [{"id": a.id, "name": a.name, "breed": a.breed, "ear_tag": a.ear_tag, "status": a.status, "image_count": len(a.images), "created_at": a.created_at.isoformat()} for a in animals], "total": total, "offset": offset, "limit": limit}


@app.get("/api/animals/{animal_id}")
def get_animal(animal_id: str):
    with SessionFactory() as db:
        animal = db.get(Animal, animal_id)
        if animal is None:
            raise HTTPException(404, "Animal record not found.")
        return {"id": animal.id, "name": animal.name, "breed": animal.breed, "ear_tag": animal.ear_tag, "sex": animal.sex, "date_of_birth": animal.date_of_birth, "status": animal.status, "organization_id": animal.organization_id, "created_at": animal.created_at.isoformat(), "images": [{"id": image.id, "width": image.width, "height": image.height, "content_sha256": image.content_sha256} for image in animal.images]}


@app.post("/api/animals")
async def register_animal(name: str = Form(""), breed: str = Form(""), ear_tag: str = Form(""), sex: str = Form(""), date_of_birth: str = Form(""), images: list[UploadFile] = File(...), idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128)):
    if not images or len(images) > 8:
        raise HTTPException(422, "Upload between 1 and 8 images.")
    cfg = get_settings()
    with SessionFactory() as db:
        prior = db.get(RegistrationRequest, idempotency_key)
        if prior:
            animal = db.get(Animal, prior.animal_id)
            return {"id": prior.animal_id, "status": animal.status if animal else "pending_model", "image_count": len(animal.images) if animal else 0, "persisted": animal is not None, "replayed": True, "message": "This registration request was already saved."}
    animal_id = str(uuid4())
    saved: list[tuple[Path, AnimalImage]] = []
    seen_hashes: set[str] = set()
    try:
        for upload in images:
            raw = await upload.read(cfg["max_upload_bytes"] + 1)
            if len(raw) > cfg["max_upload_bytes"]:
                raise HTTPException(413, "Each image must be 12 MB or smaller.")
            quality = inspect_image(raw)
            if not quality["valid"]:
                raise HTTPException(422, quality["reason"])
            digest = sha256(raw).hexdigest()
            if digest in seen_hashes:
                raise HTTPException(422, "The same image was uploaded more than once.")
            seen_hashes.add(digest)
            image_id = str(uuid4())
            suffix = ".jpg" if quality["format"] in {"JPEG", "JPG"} else ".png" if quality["format"] == "PNG" else ".webp" if quality["format"] == "WEBP" else ".img"
            storage_name = f"{image_id}{suffix}"
            path = cfg["upload_dir"] / storage_name
            saved.append((path, AnimalImage(id=image_id, animal_id=animal_id, storage_name=storage_name, content_sha256=digest, width=quality["width"], height=quality["height"])))
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(raw)
            temporary.replace(path)
        with SessionFactory.begin() as db:
            animal = Animal(id=animal_id, name=name[:120], breed=breed[:120], ear_tag=ear_tag[:120], sex=sex[:32], date_of_birth=date_of_birth[:32], status="pending_model")
            db.add(animal)
            db.add_all([item for _, item in saved])
            db.add(RegistrationRequest(idempotency_key=idempotency_key, animal_id=animal_id))
        return {"id": animal_id, "status": "pending_model", "image_count": len(saved), "persisted": True, "replayed": False, "message": "Animal record and images are saved. Biometric profile creation is pending a configured model."}
    except HTTPException:
        for path, _ in saved:
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)
        raise
    except Exception:
        for path, _ in saved:
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)
        with SessionFactory() as db:
            prior = db.get(RegistrationRequest, idempotency_key)
            if prior:
                animal = db.get(Animal, prior.animal_id)
                return {"id": prior.animal_id, "status": animal.status if animal else "pending_model", "image_count": len(animal.images) if animal else 0, "persisted": animal is not None, "replayed": True, "message": "This registration request was already saved."}
        log.exception("Animal registration failed")
        raise HTTPException(500, "Registration could not be saved.")


@app.post("/api/verify")
async def verify_animal(image: UploadFile = File(...)):
    raw = await image.read(get_settings()["max_upload_bytes"] + 1)
    if len(raw) > get_settings()["max_upload_bytes"]:
        raise HTTPException(413, "Image must be 12 MB or smaller.")
    quality = inspect_image(raw)
    if not quality["valid"]:
        raise HTTPException(422, quality["reason"])
    if not get_settings()["model_path"].is_file():
        raise HTTPException(503, "Biometric verification is unavailable: trained model weights and calibrated thresholds are not configured.")
    # A checkpoint alone is not enough: detector, preprocessing contract, index, and frozen thresholds
    # must all be configured before this route can make an identity claim.
    raise HTTPException(503, "Biometric verification is unavailable: inference pipeline artifacts are incomplete.")


@app.get("/api/history")
def history():
    with SessionFactory() as db:
        rows = db.execute(select(Verification).order_by(Verification.created_at.desc()).limit(100)).scalars().all()
        return [{"id": x.id, "animal_id": x.animal_id, "decision": x.decision, "similarity": x.similarity, "reason": x.reason, "created_at": x.created_at.isoformat()} for x in rows]
