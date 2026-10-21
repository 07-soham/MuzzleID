"""FastAPI service for the secured, research-only MuzzleID prototype."""
from contextlib import asynccontextmanager
from collections import OrderedDict, deque
from datetime import date, datetime, timedelta, timezone
import csv
from io import StringIO
import hmac
from hashlib import sha256
from pathlib import Path
import secrets
import time
from uuid import uuid4
import logging

from typing import Literal
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from fastapi.staticfiles import StaticFiles
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import select, func, or_, text, delete, update
from src.config import ROOT, get_settings
from src.database.models import Animal, AnimalImage, ImageEmbedding, RegistrationRequest, Verification, InsurancePolicy, OperationalAlert, make_session_factory
from src.inference.engine import embed_images, faiss_available, match_embedding, model_state
from src.inference.faiss_gallery import ensure_gallery_index
from src.inference.quality import inspect_image
from src.security.crypto import data_key_bytes, decrypt_bytes, encrypt_bytes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("muzzleid")
SessionFactory = make_session_factory()
ENGINE_STARTUP_STATE = {"initialized": False, "reason": "Biometric engine startup preflight has not run."}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ENGINE_STARTUP_STATE
    reconcile_storage()
    pipeline = model_state()
    if not pipeline["available"]:
        ENGINE_STARTUP_STATE = {"initialized": False, "reason": pipeline.get("reason", "Model artifacts are unavailable.")}
        log.error("Biometric engine startup preflight failed: %s", ENGINE_STARTUP_STATE["reason"])
        yield
        return
    if not faiss_available():
        ENGINE_STARTUP_STATE = {"initialized": False, "reason": "FAISS runtime is unavailable. Install the matching faiss-cpu dependency."}
        log.error("Biometric engine startup preflight failed: %s", ENGINE_STARTUP_STATE["reason"])
        yield
        return
    try:
        key = data_key_bytes(get_settings().get("data_key", ""))
        with SessionFactory() as db:
            gallery = _load_current_gallery(db, pipeline["model_version"], key)
        index, identities = ensure_gallery_index(gallery, index_path=get_settings()["index_path"], encryption_key=key, model_version=pipeline["model_version"])
        ENGINE_STARTUP_STATE = {"initialized": True, "reason": None, "model_version": pipeline["model_version"], "gallery_identities": len(identities)}
        log.info("Biometric engine initialized at startup: model=%s gallery_identities=%d persisted_index=%s", pipeline["model_version"], len(identities), index is not None)
    except Exception as exc:
        ENGINE_STARTUP_STATE = {"initialized": False, "reason": "Biometric engine startup failed while loading encrypted gallery data."}
        log.exception("Biometric engine startup preflight failed")
    yield


def reconcile_storage():
    """Reconcile filesystem records after an interrupted upload or disk loss."""
    upload_dir = get_settings()["upload_dir"]
    upload_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with SessionFactory.begin() as db:
        rows = db.execute(select(AnimalImage.id, AnimalImage.animal_id, AnimalImage.storage_name)).all()
        referenced = {name for _, _, name in rows}
        missing = [(image_id, animal_id) for image_id, animal_id, name in rows if not _safe_upload_path(upload_dir, name).is_file()]
        missing_image_ids = [image_id for image_id, _ in missing]
        affected_animals = {animal_id for _, animal_id in missing}
        if missing_image_ids:
            db.execute(delete(ImageEmbedding).where(ImageEmbedding.image_id.in_(missing_image_ids)))
            db.execute(update(Animal).where(Animal.id.in_(affected_animals)).values(status="storage_error"))
            log.error("Storage recovery found %d missing or unsafe image file(s); affected animal profiles were disabled", len(missing_image_ids))
        if upload_dir.exists():
            for path in upload_dir.iterdir():
                if not path.is_file():
                    continue
                age = now - path.stat().st_mtime
                if path.name.endswith(".tmp") and age > 3600:
                    path.unlink(missing_ok=True)
                elif path.suffix == ".enc" and path.name not in referenced and age > 86400:
                    path.unlink(missing_ok=True)


def _safe_upload_path(upload_dir: Path, storage_name: str) -> Path:
    root = upload_dir.resolve()
    filename = Path(storage_name)
    if filename.name != storage_name:
        return root / "__invalid_storage_name__"
    path = root / filename
    if path.is_symlink() or path.resolve().parent != root:
        return root / "__invalid_storage_path__"
    return path


def _load_current_gallery(db, model_version, encryption_key):
    rows = db.execute(select(Animal.id, ImageEmbedding.image_id, ImageEmbedding.vector_encrypted)
                      .join(AnimalImage, AnimalImage.animal_id == Animal.id)
                      .join(ImageEmbedding, ImageEmbedding.image_id == AnimalImage.id)
                      .where(Animal.status == "prototype_ready", ImageEmbedding.model_version == model_version)).all()
    gallery = []
    for animal_id, image_id, encrypted in rows:
        ciphertext = __import__("base64").urlsafe_b64decode(encrypted.encode("ascii"))
        vector_json = decrypt_bytes(ciphertext, encryption_key, f"muzzleid-embedding:{image_id}:{model_version}").decode("utf-8")
        gallery.append((animal_id, vector_json))
    return gallery


app = FastAPI(title="MuzzleID API", version="0.2.0", description="Secured prototype for full-image cattle identity embeddings; not validated for consequential decisions.", lifespan=lifespan)
SESSION_COOKIE = "muzzleid_session"
REQUEST_ATTEMPTS = OrderedDict()


class AnimalUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str | None = Field(default=None, max_length=120)
    breed: str | None = Field(default=None, max_length=120)
    ear_tag: str | None = Field(default=None, max_length=120)
    sex: Literal["", "Female", "Male"] | None = None
    date_of_birth: date | None = None


class InsurancePolicyCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    animal_id: str = Field(min_length=1, max_length=36)
    provider: str = Field(min_length=1, max_length=120)
    policy_number: str = Field(min_length=1, max_length=120)
    coverage_amount: float = Field(ge=0, le=1_000_000_000)
    start_date: date
    end_date: date
    notes: str = Field(default="", max_length=2000)

class InsurancePolicyUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    status: Literal["pending", "active", "expired", "cancelled", "claim_reported"] | None = None
    notes: str | None = Field(default=None, max_length=2000)
    end_date: date | None = None


class AlertUpdate(BaseModel):
    status: Literal["open", "resolved"]


class RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Enforce request bounds while streaming, including chunked requests."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        max_image = get_settings()["max_upload_bytes"]
        limit = 8 * 1024 if path == "/api/login" else max_image + 1024 * 1024 if path == "/api/verify" else get_settings()["max_registration_bytes"] + 1024 * 1024 if path == "/api/animals" else 1024 * 1024
        length = 0
        response_started = False

        async def limited_receive():
            nonlocal length
            message = await receive()
            if message["type"] == "http.request":
                length += len(message.get("body", b""))
                if length > limit:
                    raise RequestBodyTooLarge()
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if not response_started:
                body = b"Request body exceeds the configured limit."
                await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})


app.add_middleware(RequestBodyLimitMiddleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit():
        path = request.url.path
        limit = 8 * 1024 if path == "/api/login" else get_settings()["max_upload_bytes"] + 1024 * 1024 if path == "/api/verify" else get_settings()["max_registration_bytes"] + 1024 * 1024 if path == "/api/animals" else 1024 * 1024
        if int(content_length) > limit:
            return Response("Request body is too large.", status_code=413)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


def _session_signature(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), sha256).hexdigest()


def _session_valid(token: str | None, secret: str) -> bool:
    if not token or "." not in token:
        return False
    payload, signature = token.rsplit(".", 1)
    try:
        expires, nonce = payload.split(".", 1)
        return int(expires) > int(time.time()) and len(nonce) >= 20 and hmac.compare_digest(signature, _session_signature(payload, secret))
    except (TypeError, ValueError):
        return False


def require_session(request: Request):
    secret = get_settings().get("session_secret", "")
    if len(secret) < 32 or not _session_valid(request.cookies.get(SESSION_COOKIE), secret):
        raise HTTPException(401, "Sign in to access MuzzleID records.")


def check_rate_limit(request: Request, route: str, limit: int, window_seconds: int):
    now = time.time()
    client_key = request.client.host if request.client else "unknown"
    key = (route, client_key)
    attempts = REQUEST_ATTEMPTS.get(key)
    if attempts is None:
        if len(REQUEST_ATTEMPTS) >= 2048:
            REQUEST_ATTEMPTS.popitem(last=False)
        attempts = deque()
        REQUEST_ATTEMPTS[key] = attempts
    else:
        REQUEST_ATTEMPTS.move_to_end(key)
    while attempts and now - attempts[0] > window_seconds:
        attempts.popleft()
    if len(attempts) >= limit:
        raise HTTPException(429, "Too many requests. Wait before trying again.")
    attempts.append(now)


def _effective_profile_status(stored_status, image_ids, versions_by_image, current_version):
    if stored_status == "storage_error":
        return "storage_error"
    if not image_ids:
        return "pending_model"
    versions = [versions_by_image.get(image_id, set()) for image_id in image_ids]
    if not current_version:
        return "stale_model" if any(versions) else "pending_model"
    current_count = sum(current_version in available for available in versions)
    if current_count == len(image_ids):
        return "prototype_ready"
    if any(versions):
        return "reindex_required"
    return "pending_model"


def _current_profile_statuses(db):
    animals = db.scalars(select(Animal)).all()
    animal_ids = [animal.id for animal in animals]
    image_rows = db.execute(select(AnimalImage.animal_id, AnimalImage.id).where(AnimalImage.animal_id.in_(animal_ids))).all() if animal_ids else []
    images_by_animal, image_ids = {}, []
    for animal_id, image_id in image_rows:
        images_by_animal.setdefault(animal_id, []).append(image_id)
        image_ids.append(image_id)
    versions_by_image = {}
    if image_ids:
        for image_id, version in db.execute(select(ImageEmbedding.image_id, ImageEmbedding.model_version).where(ImageEmbedding.image_id.in_(image_ids))).all():
            versions_by_image.setdefault(image_id, set()).add(version)
    version = model_state().get("model_version")
    return {animal.id: _effective_profile_status(animal.status, images_by_animal.get(animal.id, []), versions_by_image, version) for animal in animals}


@app.get("/api/session")
def session_status(request: Request):
    cfg = get_settings()
    configured = len(cfg.get("operator_password", "")) >= 12 and len(cfg.get("session_secret", "")) >= 32
    return {"authenticated": configured and _session_valid(request.cookies.get(SESSION_COOKIE), cfg.get("session_secret", "")), "configured": configured}


@app.post("/api/login")
def login(request: Request, response: Response, credentials: dict):
    cfg = get_settings()
    password, secret = cfg.get("operator_password", ""), cfg.get("session_secret", "")
    if len(password) < 12 or len(secret) < 32:
        raise HTTPException(503, "Set MUZZLEID_OPERATOR_PASSWORD (12+ characters) and MUZZLEID_SESSION_SECRET (32+ characters) in the private .env file.")
    check_rate_limit(request, "login", 8, 300)
    submitted = credentials.get("password") if isinstance(credentials, dict) else None
    if not isinstance(submitted, str) or not hmac.compare_digest(submitted.encode(), password.encode()):
        raise HTTPException(401, "The operator password is incorrect.")
    now = time.time()
    payload = f"{int(now) + 8 * 60 * 60}.{secrets.token_urlsafe(24)}"
    token = f"{payload}.{_session_signature(payload, secret)}"
    response.set_cookie(SESSION_COOKIE, token, max_age=8 * 60 * 60, httponly=True, secure=cfg.get("cookie_secure", False), samesite="strict", path="/")
    return {"authenticated": True}


@app.post("/api/logout", dependencies=[Depends(require_session)])
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=get_settings().get("cookie_secure", False), samesite="strict")
    return {"authenticated": False}

static_dir = ROOT / "web"
if static_dir.exists():
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(static_dir / "index.html")


@app.get("/api/health", dependencies=[Depends(require_session)])
def health():
    cfg = get_settings()
    database_available = False
    try:
        with SessionFactory() as db:
            db.execute(text("SELECT 1"))
        database_available = True
    except Exception:
        log.exception("Database health check failed")
    pipeline = model_state()
    model_available = pipeline["available"]
    engine_ready = database_available and model_available and faiss_available() and ENGINE_STARTUP_STATE.get("initialized", False)
    return {
        "status": "ok" if database_available else "degraded",
        "database_available": database_available,
        "model_available": model_available,
        "detector_available": False,
        "index_available": engine_ready,
        "gallery_index_persisted": get_settings()["index_path"].is_file(),
        "calibrated_thresholds_available": model_available,
        "verification_available": engine_ready,
        "biometric_engine_reason": None if engine_ready else (ENGINE_STARTUP_STATE.get("reason") or pipeline.get("reason") or "Biometric runtime dependencies are unavailable."),
        "decision_thresholds": "Prototype calibration" if model_available else "Not available",
        "model_version": pipeline.get("model_version"),
        "model_limitations": pipeline.get("limitations", []),
        "mode": "Prototype; full-image embedding, no muzzle detector",
    }


@app.get("/api/animals", dependencies=[Depends(require_session)])
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
        ids = [animal.id for animal in animals]
        image_rows = db.execute(select(AnimalImage.animal_id, AnimalImage.id).where(AnimalImage.animal_id.in_(ids))).all() if ids else []
        image_ids_by_animal = {}
        for animal_id, image_id in image_rows:
            image_ids_by_animal.setdefault(animal_id, []).append(image_id)
        all_image_ids = [image_id for _, image_id in image_rows]
        embeddings = db.execute(select(ImageEmbedding.image_id, ImageEmbedding.model_version).where(ImageEmbedding.image_id.in_(all_image_ids))).all() if all_image_ids else []
        versions_by_image = {}
        for image_id, version in embeddings:
            versions_by_image.setdefault(image_id, set()).add(version)
        current_version = model_state().get("model_version")
        return {"items": [{"id": a.id, "name": a.name, "breed": a.breed, "ear_tag": a.ear_tag, "status": _effective_profile_status(a.status, image_ids_by_animal.get(a.id, []), versions_by_image, current_version), "image_count": len(image_ids_by_animal.get(a.id, [])), "created_at": a.created_at.isoformat()} for a in animals], "total": total, "offset": offset, "limit": limit}


@app.get("/api/animal-options", dependencies=[Depends(require_session)])
def animal_options():
    with SessionFactory() as db:
        rows = db.execute(select(Animal.id, Animal.name, Animal.ear_tag).order_by(Animal.name, Animal.id).limit(5000)).all()
        return [{"id": animal_id, "label": " · ".join(part for part in (name or "Unnamed animal", ear_tag) if part)} for animal_id, name, ear_tag in rows]


@app.get("/api/animals/{animal_id}", dependencies=[Depends(require_session)])
def get_animal(animal_id: str):
    with SessionFactory() as db:
        animal = db.get(Animal, animal_id)
        if animal is None:
            raise HTTPException(404, "Animal record not found.")
        image_ids = [image.id for image in animal.images]
        versions_by_image = {}
        if image_ids:
            for image_id, version in db.execute(select(ImageEmbedding.image_id, ImageEmbedding.model_version).where(ImageEmbedding.image_id.in_(image_ids))).all():
                versions_by_image.setdefault(image_id, set()).add(version)
        status = _effective_profile_status(animal.status, image_ids, versions_by_image, model_state().get("model_version"))
        return {"id": animal.id, "name": animal.name, "breed": animal.breed, "ear_tag": animal.ear_tag, "sex": animal.sex, "date_of_birth": animal.date_of_birth, "status": status, "organization_id": animal.organization_id, "created_at": animal.created_at.isoformat(), "images": [{"id": image.id, "width": image.width, "height": image.height, "format": image.image_format, "quality": __import__("json").loads(image.quality_json or "{}"), "content_sha256": image.content_sha256} for image in animal.images]}


@app.patch("/api/animals/{animal_id}", dependencies=[Depends(require_session)])
def update_animal(animal_id: str, payload: AnimalUpdate):
    changes = payload.model_dump(exclude_unset=True)
    if "date_of_birth" in changes:
        dob = changes["date_of_birth"]
        if dob and dob > date.today():
            raise HTTPException(422, "Date of birth cannot be in the future.")
        changes["date_of_birth"] = dob.isoformat() if dob else ""
    with SessionFactory.begin() as db:
        animal = db.get(Animal, animal_id)
        if animal is None:
            raise HTTPException(404, "Animal record not found.")
        for field, value in changes.items():
            setattr(animal, field, value or "")
    return {"id": animal_id, "updated": sorted(changes)}


@app.delete("/api/animals/{animal_id}", status_code=204, dependencies=[Depends(require_session)])
def delete_animal(animal_id: str):
    cfg = get_settings()
    with SessionFactory.begin() as db:
        animal = db.get(Animal, animal_id)
        if animal is None:
            raise HTTPException(404, "Animal record not found.")
        images = list(animal.images)
        paths = [_safe_upload_path(cfg["upload_dir"], image.storage_name) for image in images]
        image_ids = [image.id for image in images]
        verification_ids = list(db.scalars(select(Verification.id).where(Verification.animal_id == animal_id)).all())
        if verification_ids:
            db.execute(delete(OperationalAlert).where(OperationalAlert.verification_id.in_(verification_ids)))
        db.execute(delete(OperationalAlert).where(OperationalAlert.animal_id == animal_id))
        db.execute(update(Verification).where(Verification.animal_id == animal_id).values(animal_id=None))
        db.execute(delete(InsurancePolicy).where(InsurancePolicy.animal_id == animal_id))
        if image_ids:
            db.execute(delete(ImageEmbedding).where(ImageEmbedding.image_id.in_(image_ids)))
        db.execute(delete(RegistrationRequest).where(RegistrationRequest.animal_id == animal_id))
        db.delete(animal)
    for path in paths:
        path.unlink(missing_ok=True)
    return Response(status_code=204)


@app.post("/api/animals/{animal_id}/reindex", dependencies=[Depends(require_session)])
def reindex_animal(animal_id: str, request: Request):
    """Rebuild this animal's embeddings from its authenticated, encrypted reference images."""
    check_rate_limit(request, "reindex", 6, 60)
    pipeline = model_state()
    if not pipeline["available"]:
        raise HTTPException(503, "A trained model and matching calibration are required before reindexing.")
    cfg = get_settings()
    try:
        key = data_key_bytes(cfg.get("data_key", ""))
        with SessionFactory() as db:
            animal = db.get(Animal, animal_id)
            if animal is None:
                raise HTTPException(404, "Animal record not found.")
            images = sorted(animal.images, key=lambda image: image.id)
            if not images:
                raise HTTPException(409, "This animal has no reference images to reindex.")
            raw_images = []
            for image in images:
                path = _safe_upload_path(cfg["upload_dir"], image.storage_name)
                if not path.is_file():
                    raise HTTPException(409, "A stored reference image is missing; repair storage before reindexing.")
                raw = decrypt_bytes(path.read_bytes(), key, f"muzzleid-upload:{image.id}")
                if sha256(raw).hexdigest() != image.content_sha256:
                    raise HTTPException(409, "A stored reference image failed its integrity check.")
                quality = inspect_image(raw)
                if not quality["valid"]:
                    raise HTTPException(409, "A stored reference image is no longer valid; repair the record before reindexing.")
                raw_images.append(raw)
            vectors, version, _ = embed_images(raw_images)
            encrypted_vectors = [__import__("base64").urlsafe_b64encode(encrypt_bytes(__import__("json").dumps(vector, separators=(",", ":")).encode(), key, f"muzzleid-embedding:{image.id}:{version}")).decode("ascii") for image, vector in zip(images, vectors)]
        with SessionFactory.begin() as db:
            animal = db.get(Animal, animal_id)
            if animal is None:
                raise HTTPException(404, "Animal record was removed during reindexing.")
            for image, encrypted in zip(images, encrypted_vectors):
                existing = db.get(ImageEmbedding, (image.id, version))
                if existing is None:
                    db.add(ImageEmbedding(image_id=image.id, model_version=version, vector_encrypted=encrypted))
                else:
                    existing.vector_encrypted = encrypted
            animal.status = "prototype_ready"
        return {"id": animal_id, "status": "prototype_ready", "model_version": version, "image_count": len(images), "reindexed": True}
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Animal embedding reindex failed")
        raise HTTPException(503, "The encrypted reference images could not be reindexed.") from exc


@app.get("/api/animals/{animal_id}/images/{image_id}", dependencies=[Depends(require_session)])
def animal_image(animal_id: str, image_id: str):
    cfg = get_settings()
    try:
        key = data_key_bytes(cfg.get("data_key", ""))
    except RuntimeError as exc:
        raise HTTPException(503, "Encrypted image access is not configured.") from exc
    with SessionFactory() as db:
        image = db.scalar(select(AnimalImage).where(AnimalImage.id == image_id, AnimalImage.animal_id == animal_id))
        if image is None:
            raise HTTPException(404, "Animal image not found.")
        path = _safe_upload_path(cfg["upload_dir"], image.storage_name)
        if not path.is_file():
            raise HTTPException(503, "Stored image file is missing.")
        try:
            raw = decrypt_bytes(path.read_bytes(), key, f"muzzleid-upload:{image.id}")
        except Exception as exc:
            log.exception("Encrypted reference image failed integrity validation")
            raise HTTPException(503, "Stored image failed integrity validation.") from exc
    media_type = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(image.image_format)
    if not media_type:
        raise HTTPException(415, "Stored image format is not supported.")
    return Response(raw, media_type=media_type, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"})


@app.post("/api/animals", dependencies=[Depends(require_session)])
async def register_animal(request: Request, name: str = Form("", max_length=120), breed: str = Form("", max_length=120), ear_tag: str = Form("", max_length=120), sex: str = Form(""), date_of_birth: str = Form("", max_length=10), images: list[UploadFile] = File(...), idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128)):
    check_rate_limit(request, "register", 12, 60)
    if not images or len(images) > 8:
        raise HTTPException(422, "Upload between 1 and 8 images.")
    if sex not in {"", "Female", "Male"}:
        raise HTTPException(422, "Sex must be Female or Male.")
    if date_of_birth:
        try:
            parsed_dob = date.fromisoformat(date_of_birth)
        except ValueError as exc:
            raise HTTPException(422, "Date of birth must be a valid ISO date.") from exc
        if parsed_dob > date.today():
            raise HTTPException(422, "Date of birth cannot be in the future.")
    cfg = get_settings()
    try:
        encryption_key = data_key_bytes(cfg.get("data_key", ""))
    except RuntimeError as exc:
        raise HTTPException(503, "Encrypted image storage is not configured. Set MUZZLEID_DATA_KEY before registering records.") from exc
    with SessionFactory() as db:
        prior = db.get(RegistrationRequest, idempotency_key)
        if prior:
            animal = db.get(Animal, prior.animal_id)
            if animal is None:
                raise HTTPException(409, "This idempotency key references a missing animal record; contact the operator to repair the database.")
            return {"id": prior.animal_id, "status": animal.status, "image_count": len(animal.images), "persisted": True, "replayed": True, "message": "This registration request was already saved."}
    animal_id = str(uuid4())
    saved: list[tuple[Path, AnimalImage]] = []
    raw_images: list[bytes] = []
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
            raw_images.append(raw)
            image_id = str(uuid4())
            storage_name = f"{image_id}.enc"
            path = cfg["upload_dir"] / storage_name
            image_row = AnimalImage(
                id=image_id,
                animal_id=animal_id,
                storage_name=storage_name,
                content_sha256=digest,
                width=quality["width"],
                height=quality["height"],
                image_format=quality["format"],
                quality_json=__import__("json").dumps(quality["quality"], separators=(",", ":")),
            )
            saved.append((path, image_row))
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(encrypt_bytes(raw, encryption_key, f"muzzleid-upload:{image_id}"))
            temporary.replace(path)
        vectors = []
        model_version = None
        pipeline_ready = model_state()["available"]
        if pipeline_ready:
            try:
                vectors, model_version, _ = embed_images(raw_images)
            except Exception as exc:
                log.exception("Reference-image embedding failed")
                raise HTTPException(503, "The biometric model could not process these images. No record was saved; contact the operator.") from exc
        with SessionFactory.begin() as db:
            animal = Animal(id=animal_id, name=name[:120], breed=breed[:120], ear_tag=ear_tag[:120], sex=sex[:32], date_of_birth=date_of_birth[:32], status="prototype_ready" if vectors else "pending_model")
            db.add(animal)
            db.add_all([item for _, item in saved])
            if vectors:
                db.add_all([ImageEmbedding(image_id=item.id, model_version=model_version, vector_encrypted=__import__("base64").urlsafe_b64encode(encrypt_bytes(__import__("json").dumps(vector, separators=(",", ":")).encode(), encryption_key, f"muzzleid-embedding:{item.id}:{model_version}")).decode("ascii")) for (_, item), vector in zip(saved, vectors)])
            db.add(RegistrationRequest(idempotency_key=idempotency_key, animal_id=animal_id))
        profile_status = "prototype_ready" if vectors else "pending_model"
        message = "Animal record and biometric profile are saved." if vectors else "Animal record and images are saved. Biometric profile creation is pending a configured model."
        return {"id": animal_id, "status": profile_status, "image_count": len(saved), "persisted": True, "replayed": False, "message": message}
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


@app.post("/api/verify", dependencies=[Depends(require_session)])
async def verify_animal(request: Request, image: UploadFile = File(...)):
    check_rate_limit(request, "verify", 30, 60)
    raw = await image.read(get_settings()["max_upload_bytes"] + 1)
    if len(raw) > get_settings()["max_upload_bytes"]:
        raise HTTPException(413, "Image must be 12 MB or smaller.")
    quality = inspect_image(raw)
    if not quality["valid"]:
        raise HTTPException(422, quality["reason"])
    try:
        vectors, version, _ = embed_images([raw])
    except RuntimeError as exc:
        raise HTTPException(503, "Biometric verification is unavailable: trained model and matching calibration artifacts are not configured.") from exc
    except Exception as exc:
        log.exception("Verification embedding failed")
        raise HTTPException(503, "Biometric verification is unavailable because the model failed to process the image.") from exc
    try:
        encryption_key = data_key_bytes(get_settings().get("data_key", ""))
        with SessionFactory.begin() as db:
            gallery = _load_current_gallery(db, version, encryption_key)
            animal_id, similarity, decision = match_embedding(vectors[0], gallery, index_path=get_settings()["index_path"], key=encryption_key)
            event_id = str(uuid4())
            db.add(Verification(id=event_id, animal_id=animal_id if decision != "UNKNOWN" else None, decision=decision, similarity=similarity, reason="Prototype full-image embedding with training-identity image-split calibration; human review required."))
            if decision == "REVIEW":
                db.add(OperationalAlert(id=str(uuid4()), fingerprint=f"verification-review:{event_id}", animal_id=animal_id, verification_id=event_id, category="verification_review", severity="medium", message="A prototype match requires human review. Verify the animal in person."))
    except Exception as exc:
        log.exception("Encrypted gallery lookup failed")
        raise HTTPException(503, "The encrypted biometric gallery is unavailable. No identity decision was made.") from exc
    return {"id": event_id, "animal_id": animal_id if decision != "UNKNOWN" else None, "decision": decision, "similarity": similarity, "model_version": version, "prototype_only": True, "message": "Prototype result only. Confirm identity in person; do not use for ownership or insurance decisions."}


@app.get("/api/history", dependencies=[Depends(require_session)])
def history():
    with SessionFactory() as db:
        rows = db.execute(select(Verification).order_by(Verification.created_at.desc()).limit(100)).scalars().all()
        return [{"id": x.id, "animal_id": x.animal_id, "decision": x.decision, "similarity": x.similarity, "reason": x.reason, "created_at": x.created_at.isoformat()} for x in rows]


def _policy_json(policy):
    return {"id": policy.id, "animal_id": policy.animal_id, "provider": policy.provider, "policy_number": policy.policy_number, "coverage_amount": policy.coverage_amount, "start_date": policy.start_date, "end_date": policy.end_date, "status": policy.status, "notes": policy.notes, "created_at": policy.created_at.isoformat()}


def _refresh_policy_statuses(db):
    today = date.today().isoformat()
    db.execute(update(InsurancePolicy).where(InsurancePolicy.status == "active", InsurancePolicy.end_date < today).values(status="expired"))
    db.execute(update(InsurancePolicy).where(InsurancePolicy.status == "pending", InsurancePolicy.end_date < today).values(status="expired"))
    db.execute(update(InsurancePolicy).where(InsurancePolicy.status == "pending", InsurancePolicy.start_date <= today, InsurancePolicy.end_date >= today).values(status="active"))


@app.get("/api/insurance/policies", dependencies=[Depends(require_session)])
def insurance_policies(status: Literal["all", "pending", "active", "expired", "cancelled", "claim_reported"] = "all"):
    with SessionFactory.begin() as db:
        _refresh_policy_statuses(db)
        statement = select(InsurancePolicy).order_by(InsurancePolicy.end_date.asc(), InsurancePolicy.created_at.desc())
        if status != "all":
            statement = statement.where(InsurancePolicy.status == status)
        return [_policy_json(policy) for policy in db.scalars(statement).all()]


@app.post("/api/insurance/policies", status_code=201, dependencies=[Depends(require_session)])
def create_insurance_policy(payload: InsurancePolicyCreate):
    if payload.end_date < payload.start_date:
        raise HTTPException(422, "Policy end date must be on or after its start date.")
    with SessionFactory.begin() as db:
        if db.get(Animal, payload.animal_id) is None:
            raise HTTPException(404, "Animal record not found.")
        policy = InsurancePolicy(id=str(uuid4()), animal_id=payload.animal_id, provider=payload.provider, policy_number=payload.policy_number, coverage_amount=payload.coverage_amount, start_date=payload.start_date.isoformat(), end_date=payload.end_date.isoformat(), status="expired" if payload.end_date < date.today() else "pending" if payload.start_date > date.today() else "active", notes=payload.notes)
        db.add(policy)
        db.flush()
        result = _policy_json(policy)
    return result


@app.patch("/api/insurance/policies/{policy_id}", dependencies=[Depends(require_session)])
def update_insurance_policy(policy_id: str, payload: InsurancePolicyUpdate):
    changes = {field: value for field, value in payload.model_dump(exclude_unset=True).items() if value is not None}
    with SessionFactory.begin() as db:
        policy = db.get(InsurancePolicy, policy_id)
        if policy is None:
            raise HTTPException(404, "Insurance policy record not found.")
        start_date = date.fromisoformat(policy.start_date)
        end_date = changes.get("end_date", date.fromisoformat(policy.end_date))
        if end_date < start_date:
            raise HTTPException(422, "Policy end date must be on or after its start date.")
        requested_status = changes.get("status")
        if requested_status == "active" and not (start_date <= date.today() <= end_date):
            raise HTTPException(422, "A policy can be marked active only during its effective date range.")
        if requested_status == "pending" and start_date <= date.today():
            raise HTTPException(422, "A policy can be pending only before its start date.")
        if requested_status == "expired" and end_date >= date.today():
            raise HTTPException(422, "A policy can be marked expired only after its end date.")
        for field, value in changes.items():
            setattr(policy, field, value.isoformat() if isinstance(value, date) else value)
        if policy.status in {"pending", "active", "expired"}:
            policy.status = "expired" if end_date < date.today() else "pending" if start_date > date.today() else "active"
        db.flush()
        result = _policy_json(policy)
    return result


@app.delete("/api/insurance/policies/{policy_id}", status_code=204, dependencies=[Depends(require_session)])
def delete_insurance_policy(policy_id: str):
    with SessionFactory.begin() as db:
        policy = db.get(InsurancePolicy, policy_id)
        if policy is None:
            raise HTTPException(404, "Insurance policy record not found.")
        db.delete(policy)
    return Response(status_code=204)


def _refresh_duplicate_tag_alerts(db):
    duplicates = db.execute(select(Animal.ear_tag, func.count(Animal.id)).where(Animal.ear_tag != "").group_by(Animal.ear_tag).having(func.count(Animal.id) > 1)).all()
    active_fingerprints = set()
    for tag, _count in duplicates:
        animals = db.scalars(select(Animal).where(Animal.ear_tag == tag)).all()
        for animal in animals:
            fingerprint = f"duplicate-ear-tag:{sha256(tag.encode('utf-8')).hexdigest()}:{animal.id}"
            active_fingerprints.add(fingerprint)
            values = {"id": str(uuid4()), "fingerprint": fingerprint, "animal_id": animal.id, "category": "duplicate_ear_tag", "severity": "high", "message": f"Ear tag {tag} is assigned to more than one animal record.", "status": "open"}
            insert_fn = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
            db.execute(insert_fn(OperationalAlert).values(**values).on_conflict_do_nothing(index_elements=["fingerprint"]))
    open_duplicates = db.scalars(select(OperationalAlert).where(OperationalAlert.category == "duplicate_ear_tag", OperationalAlert.status == "open")).all()
    for alert in open_duplicates:
        if alert.fingerprint not in active_fingerprints:
            alert.status = "resolved"
            alert.resolved_at = datetime.now(timezone.utc)


@app.get("/api/alerts", dependencies=[Depends(require_session)])
def alerts(status: Literal["all", "open", "resolved"] = "open"):
    with SessionFactory.begin() as db:
        _refresh_duplicate_tag_alerts(db)
        statement = select(OperationalAlert).order_by(OperationalAlert.created_at.desc())
        if status != "all":
            statement = statement.where(OperationalAlert.status == status)
        rows = db.scalars(statement.limit(500)).all()
        return [{"id": row.id, "animal_id": row.animal_id, "verification_id": row.verification_id, "category": row.category, "severity": row.severity, "message": row.message, "status": row.status, "created_at": row.created_at.isoformat(), "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None} for row in rows]


@app.patch("/api/alerts/{alert_id}", dependencies=[Depends(require_session)])
def update_alert(alert_id: str, payload: AlertUpdate):
    with SessionFactory.begin() as db:
        alert = db.get(OperationalAlert, alert_id)
        if alert is None:
            raise HTTPException(404, "Alert not found.")
        alert.status = payload.status
        alert.resolved_at = datetime.now(timezone.utc) if payload.status == "resolved" else None
        return {"id": alert.id, "status": alert.status, "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None}


@app.get("/api/analytics", dependencies=[Depends(require_session)])
def analytics():
    since = datetime.now(timezone.utc) - timedelta(days=30)
    today = datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc)
    with SessionFactory.begin() as db:
        _refresh_policy_statuses(db)
        total_animals = db.scalar(select(func.count()).select_from(Animal)) or 0
        statuses = _current_profile_statuses(db)
        animal_status = {}
        for status in statuses.values():
            animal_status[status] = animal_status.get(status, 0) + 1
        decisions = dict(db.execute(select(Verification.decision, func.count()).where(Verification.created_at >= since).group_by(Verification.decision)).all())
        verified_today = db.scalar(select(func.count()).select_from(Verification).where(Verification.created_at >= today)) or 0
        policy_status = dict(db.execute(select(InsurancePolicy.status, func.count()).group_by(InsurancePolicy.status)).all())
        open_alerts = db.scalar(select(func.count()).select_from(OperationalAlert).where(OperationalAlert.status == "open")) or 0
        registration_rows = db.execute(select(func.date(Animal.created_at), func.count()).where(Animal.created_at >= since).group_by(func.date(Animal.created_at)).order_by(func.date(Animal.created_at))).all()
        registrations = [{"date": str(day), "count": count} for day, count in registration_rows]
        return {"total_animals": total_animals, "animal_profiles": animal_status, "verification_today": verified_today, "verification_last_30_days": {decision: decisions.get(decision, 0) for decision in ("VERIFIED", "REVIEW", "UNKNOWN")}, "policies": policy_status, "open_alerts": open_alerts, "registrations_last_30_days": registrations}


def _csv_safe(value):
    text_value = "" if value is None else str(value)
    if text_value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text_value
    return text_value


def _csv_response(filename, columns, rows):
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_safe(value) for value in row])
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/api/reports/animals.csv", dependencies=[Depends(require_session)])
def report_animals():
    with SessionFactory() as db:
        statuses = _current_profile_statuses(db)
        rows = [(animal.id, animal.name, animal.ear_tag, animal.breed, animal.sex, animal.date_of_birth, statuses[animal.id], animal.created_at) for animal in db.scalars(select(Animal).order_by(Animal.created_at)).all()]
        return _csv_response("muzzleid-animals.csv", ("id", "name", "ear_tag", "breed", "sex", "date_of_birth", "profile_status", "created_at"), rows)


@app.get("/api/reports/verifications.csv", dependencies=[Depends(require_session)])
def report_verifications():
    with SessionFactory() as db:
        rows = db.execute(select(Verification.id, Verification.animal_id, Verification.decision, Verification.similarity, Verification.reason, Verification.created_at).order_by(Verification.created_at)).all()
        return _csv_response("muzzleid-verifications.csv", ("id", "animal_id", "decision", "similarity", "reason", "created_at"), rows)


@app.get("/api/settings", dependencies=[Depends(require_session)])
def settings_status():
    cfg = get_settings()
    pipeline = model_state()
    try:
        data_key_bytes(cfg.get("data_key", ""))
        encryption_configured = True
    except RuntimeError:
        encryption_configured = False
    return {"operator_password_configured": len(cfg.get("operator_password", "")) >= 12, "session_secret_configured": len(cfg.get("session_secret", "")) >= 32, "data_encryption_configured": encryption_configured, "cookie_secure": cfg.get("cookie_secure", False), "model_available": pipeline["available"], "model_version": pipeline.get("model_version"), "faiss_available": faiss_available(), "database_type": "postgresql" if cfg["database_url"].startswith("postgresql") else "sqlite", "limitations": pipeline.get("limitations", [])}
