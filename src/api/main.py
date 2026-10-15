"""FastAPI service for the secured, research-only MuzzleID prototype."""
from contextlib import asynccontextmanager
from collections import defaultdict, deque
import hmac
from hashlib import sha256
from pathlib import Path
import secrets
import time
from uuid import uuid4
import logging

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func, or_, text
from src.config import ROOT, get_settings
from src.database.models import Animal, AnimalImage, ImageEmbedding, RegistrationRequest, Verification, make_session_factory
from src.inference.engine import embed_images, match_embedding, model_state
from src.inference.quality import inspect_image
from src.security.crypto import data_key_bytes, decrypt_bytes, encrypt_bytes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("muzzleid")
SessionFactory = make_session_factory()


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_settings()["upload_dir"].mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="MuzzleID API", version="0.2.0", description="Secured prototype for full-image cattle identity embeddings; not validated for consequential decisions.", lifespan=lifespan)
SESSION_COOKIE = "muzzleid_session"
REQUEST_ATTEMPTS = defaultdict(deque)


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
        limit = 8 * 1024 if path == "/api/login" else max_image + 1024 * 1024 if path == "/api/verify" else 8 * max_image + 1024 * 1024 if path == "/api/animals" else 1024 * 1024
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
        limit = 8 * 1024 if path == "/api/login" else get_settings()["max_upload_bytes"] + 1024 * 1024 if path == "/api/verify" else 8 * get_settings()["max_upload_bytes"] + 1024 * 1024
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
    attempts = REQUEST_ATTEMPTS[(route, client_key)]
    while attempts and now - attempts[0] > window_seconds:
        attempts.popleft()
    if len(attempts) >= limit:
        raise HTTPException(429, "Too many requests. Wait before trying again.")
    attempts.append(now)
    if len(REQUEST_ATTEMPTS) > 2048:
        for key in list(REQUEST_ATTEMPTS)[:512]:
            if not REQUEST_ATTEMPTS[key] or now - REQUEST_ATTEMPTS[key][-1] > window_seconds:
                REQUEST_ATTEMPTS.pop(key, None)


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
    return {
        "status": "ok" if database_available else "degraded",
        "database_available": database_available,
        "model_available": model_available,
        "detector_available": False,
        "index_available": database_available and model_available,
        "calibrated_thresholds_available": model_available,
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
        return {"items": [{"id": a.id, "name": a.name, "breed": a.breed, "ear_tag": a.ear_tag, "status": a.status, "image_count": len(a.images), "created_at": a.created_at.isoformat()} for a in animals], "total": total, "offset": offset, "limit": limit}


@app.get("/api/animals/{animal_id}", dependencies=[Depends(require_session)])
def get_animal(animal_id: str):
    with SessionFactory() as db:
        animal = db.get(Animal, animal_id)
        if animal is None:
            raise HTTPException(404, "Animal record not found.")
        return {"id": animal.id, "name": animal.name, "breed": animal.breed, "ear_tag": animal.ear_tag, "sex": animal.sex, "date_of_birth": animal.date_of_birth, "status": animal.status, "organization_id": animal.organization_id, "created_at": animal.created_at.isoformat(), "images": [{"id": image.id, "width": image.width, "height": image.height, "content_sha256": image.content_sha256} for image in animal.images]}


@app.post("/api/animals", dependencies=[Depends(require_session)])
async def register_animal(request: Request, name: str = Form(""), breed: str = Form(""), ear_tag: str = Form(""), sex: str = Form(""), date_of_birth: str = Form(""), images: list[UploadFile] = File(...), idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128)):
    check_rate_limit(request, "register", 12, 60)
    if not images or len(images) > 8:
        raise HTTPException(422, "Upload between 1 and 8 images.")
    cfg = get_settings()
    try:
        encryption_key = data_key_bytes(cfg.get("data_key", ""))
    except RuntimeError as exc:
        raise HTTPException(503, "Encrypted image storage is not configured. Set MUZZLEID_DATA_KEY before registering records.") from exc
    with SessionFactory() as db:
        prior = db.get(RegistrationRequest, idempotency_key)
        if prior:
            animal = db.get(Animal, prior.animal_id)
            return {"id": prior.animal_id, "status": animal.status if animal else "pending_model", "image_count": len(animal.images) if animal else 0, "persisted": animal is not None, "replayed": True, "message": "This registration request was already saved."}
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
            saved.append((path, AnimalImage(id=image_id, animal_id=animal_id, storage_name=storage_name, content_sha256=digest, width=quality["width"], height=quality["height"])))
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
            rows = db.execute(select(Animal.id, ImageEmbedding.image_id, ImageEmbedding.vector_encrypted).join(AnimalImage, AnimalImage.animal_id == Animal.id).join(ImageEmbedding, ImageEmbedding.image_id == AnimalImage.id).where(Animal.status == "prototype_ready", ImageEmbedding.model_version == version)).all()
            gallery = []
            for row in rows:
                encrypted = __import__("base64").urlsafe_b64decode(row.vector_encrypted.encode("ascii"))
                vector_json = decrypt_bytes(encrypted, encryption_key, f"muzzleid-embedding:{row.image_id}:{version}").decode("utf-8")
                gallery.append((row.id, vector_json))
            animal_id, similarity, decision = match_embedding(vectors[0], gallery)
            event_id = str(uuid4())
            db.add(Verification(id=event_id, animal_id=animal_id if decision != "UNKNOWN" else None, decision=decision, similarity=similarity, reason="Prototype full-image embedding with training-identity image-split calibration; human review required."))
    except Exception as exc:
        log.exception("Encrypted gallery lookup failed")
        raise HTTPException(503, "The encrypted biometric gallery is unavailable. No identity decision was made.") from exc
    return {"id": event_id, "animal_id": animal_id if decision != "UNKNOWN" else None, "decision": decision, "similarity": similarity, "model_version": version, "prototype_only": True, "message": "Prototype result only. Confirm identity in person; do not use for ownership or insurance decisions."}


@app.get("/api/history", dependencies=[Depends(require_session)])
def history():
    with SessionFactory() as db:
        rows = db.execute(select(Verification).order_by(Verification.created_at.desc()).limit(100)).scalars().all()
        return [{"id": x.id, "animal_id": x.animal_id, "decision": x.decision, "similarity": x.similarity, "reason": x.reason, "created_at": x.created_at.isoformat()} for x in rows]
