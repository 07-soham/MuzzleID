"""FastAPI service for the secured, research-only MuzzleID prototype."""
from contextlib import asynccontextmanager
from collections import OrderedDict, deque
from datetime import date, datetime, timedelta, timezone
import csv
from io import StringIO
import hmac
import base64
import hashlib
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
from sqlalchemy.exc import IntegrityError
from src.config import ROOT, get_settings
from src.database.models import Animal, AnimalImage, ImageEmbedding, RegistrationRequest, Verification, InsurancePolicy, OperationalAlert, Farmer, FarmerRequest, FarmerRequestEvent, make_session_factory
from src.inference.engine import embed_images, faiss_available, match_embedding, model_state
from src.inference.decision import decide
from src.inference.faiss_gallery import ensure_gallery_index
from src.inference.quality import inspect_image
from src.security.crypto import data_key_bytes, decrypt_bytes, encrypt_bytes, encrypted_muzzle_id

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


class FarmerSignup(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    full_name: str = Field(min_length=2, max_length=120)
    mobile: str = Field(min_length=8, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    password: str = Field(min_length=12, max_length=128)


class FarmerLogin(BaseModel):
    identifier: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class FarmerProfileUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    full_name: str = Field(min_length=2, max_length=120)
    mobile: str = Field(min_length=8, max_length=32)
    email: str | None = Field(default=None, max_length=254)


class FarmerPasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class FarmerRequestCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    kind: Literal["claim", "support"]
    animal_id: str | None = Field(default=None, max_length=36)
    description: str = Field(min_length=10, max_length=4000)


class FarmerRequestUpdate(BaseModel):
    status: Literal["submitted", "under_review", "resolved", "declined"]


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
        limit = 8 * 1024 if path in {"/api/login", "/api/farmer/login"} else max_image + 1024 * 1024 if path in {"/api/verify", "/api/farmer/verify"} else get_settings()["max_registration_bytes"] + 1024 * 1024 if path in {"/api/animals", "/api/farmer/animals"} else 1024 * 1024
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
        limit = 8 * 1024 if path in {"/api/login", "/api/farmer/login"} else get_settings()["max_upload_bytes"] + 1024 * 1024 if path in {"/api/verify", "/api/farmer/verify"} else get_settings()["max_registration_bytes"] + 1024 * 1024 if path in {"/api/animals", "/api/farmer/animals"} else 1024 * 1024
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
        if not hmac.compare_digest(signature, _session_signature(payload, secret)):
            return False
        if "|" not in payload and "." in payload:  # Existing administrator sessions remain valid.
            expires, nonce = payload.split(".", 1)
        else:
            parts = payload.split("|")
            if len(parts) == 3 and parts[0] == "admin":
                _, expires, nonce = parts
            elif len(parts) in {4, 5} and parts[0] == "farmer":
                if len(parts) == 5:
                    _, farmer_id, version, expires, nonce = parts
                    if int(version) < 0:
                        return False
                else:
                    _, farmer_id, expires, nonce = parts
                if not farmer_id:
                    return False
            else:
                return False
        return int(expires) > int(time.time()) and len(nonce) >= 20
    except (TypeError, ValueError):
        return False


def require_session(request: Request):
    secret = get_settings().get("session_secret", "")
    token = request.cookies.get(SESSION_COOKIE)
    if len(secret) < 32 or not _session_valid(token, secret):
        raise HTTPException(401, "Sign in to access MuzzleID records.")
    payload = token.rsplit(".", 1)[0]
    if "|" not in payload and "." in payload:
        return {"role": "admin", "farmer_id": None}
    parts = payload.split("|")
    if parts[0] == "admin":
        return {"role": "admin", "farmer_id": None}
    version = int(parts[2]) if len(parts) == 5 else 0
    with SessionFactory() as db:
        account = db.get(Farmer, parts[1])
        if account is None or account.session_version != version:
            raise HTTPException(401, "Farmer session expired. Sign in again.")
    return {"role": "farmer", "farmer_id": parts[1], "session_version": version}


def require_admin(principal=Depends(require_session)):
    if principal["role"] != "admin":
        raise HTTPException(403, "Administrator access is required.")
    return principal


def require_farmer(principal=Depends(require_session)):
    if principal["role"] != "farmer":
        raise HTTPException(403, "Farmer access is required.")
    return principal["farmer_id"]


def _issue_session(response: Response, role: str, subject: str | None = None, version: int = 0):
    cfg = get_settings()
    expires = int(time.time()) + 8 * 60 * 60
    nonce = secrets.token_urlsafe(24)
    payload = f"farmer|{subject}|{version}|{expires}|{nonce}" if role == "farmer" else f"admin|{expires}|{nonce}"
    token = f"{payload}.{_session_signature(payload, cfg['session_secret'])}"
    response.set_cookie(SESSION_COOKIE, token, max_age=8 * 60 * 60, httponly=True, secure=cfg.get("cookie_secure", False), samesite="strict", path="/")


def _normalize_mobile(value: str) -> str:
    stripped = value.strip()
    if not __import__("re").fullmatch(r"\+?[0-9][0-9 ()-]{6,30}", stripped):
        raise HTTPException(422, "Enter a valid mobile number with country code if needed.")
    digits = "".join(char for char in stripped if char.isdigit())
    return ("+" if stripped.startswith("+") else "") + digits


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.urlsafe_b64encode(salt).decode("ascii") + "$" + base64.urlsafe_b64encode(digest).decode("ascii")


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_text, digest_text = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


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
    secret_ready = len(cfg.get("session_secret", "")) >= 32
    principal = require_session(request) if secret_ready and _session_valid(request.cookies.get(SESSION_COOKIE), cfg.get("session_secret", "")) else None
    farmer = None
    if principal and principal["role"] == "farmer":
        with SessionFactory() as db:
            account = db.get(Farmer, principal["farmer_id"])
            if account:
                farmer = {"id": account.id, "full_name": account.full_name, "mobile": account.mobile, "email": account.email}
            else:
                principal = None
    return {"authenticated": principal is not None, "configured": configured, "role": principal["role"] if principal else None, "farmer": farmer}


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
    _issue_session(response, "admin")
    return {"authenticated": True}


@app.post("/api/farmer/signup", status_code=201)
def farmer_signup(payload: FarmerSignup, request: Request, response: Response):
    cfg = get_settings()
    if len(cfg.get("session_secret", "")) < 32:
        raise HTTPException(503, "Farmer accounts are unavailable until the session secret is configured.")
    check_rate_limit(request, "farmer-signup", 5, 900)
    mobile = _normalize_mobile(payload.mobile)
    email = payload.email.lower().strip() if payload.email else None
    if email and not __import__("re").fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(422, "Enter a valid email address or leave it blank.")
    now = datetime.now(timezone.utc)
    account = Farmer(id=str(uuid4()), full_name=payload.full_name, mobile=mobile, email=email, password_hash=_password_hash(payload.password), created_at=now)
    try:
        with SessionFactory.begin() as db:
            db.add(account)
            db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "An account already uses that mobile number or email address.") from exc
    _issue_session(response, "farmer", account.id, account.session_version)
    return {"authenticated": True, "role": "farmer", "farmer": {"id": account.id, "full_name": account.full_name, "mobile": account.mobile, "email": account.email}}


@app.post("/api/farmer/login")
def farmer_login(payload: FarmerLogin, request: Request, response: Response):
    if len(get_settings().get("session_secret", "")) < 32:
        raise HTTPException(503, "Farmer accounts are unavailable until the session secret is configured.")
    check_rate_limit(request, "farmer-login", 8, 300)
    identifier = payload.identifier.strip().lower()
    with SessionFactory() as db:
        if "@" in identifier:
            account = db.scalar(select(Farmer).where(Farmer.email == identifier))
        else:
            try:
                mobile = _normalize_mobile(payload.identifier)
            except HTTPException:
                mobile = ""
            account = db.scalar(select(Farmer).where(Farmer.mobile == mobile)) if mobile else None
        if not account or not _password_matches(payload.password, account.password_hash):
            # Keep password work comparable when the account does not exist.
            if not account:
                _password_hash(payload.password)
            raise HTTPException(401, "The mobile/email or password is incorrect.")
        farmer = {"id": account.id, "full_name": account.full_name, "mobile": account.mobile, "email": account.email}
        session_version = account.session_version
    _issue_session(response, "farmer", farmer["id"], session_version)
    return {"authenticated": True, "role": "farmer", "farmer": farmer}


@app.post("/api/logout")
def logout(response: Response, principal=Depends(require_session)):
    if principal["role"] == "farmer":
        with SessionFactory.begin() as db:
            account = db.get(Farmer, principal["farmer_id"])
            if account:
                account.session_version += 1
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=get_settings().get("cookie_secure", False), samesite="strict")
    return {"authenticated": False}


def _farmer_animal_json(db, animal: Animal):
    try:
        key = data_key_bytes(get_settings().get("data_key", ""))
    except RuntimeError:
        key = None
    status = _current_profile_statuses(db).get(animal.id, animal.status)
    return {
        "id": animal.id,
        "muzzle_id": animal.muzzle_id or (encrypted_muzzle_id(animal.id, key) if key else ""),
        "name": animal.name,
        "breed": animal.breed,
        "ear_tag": animal.ear_tag,
        "sex": animal.sex,
        "date_of_birth": animal.date_of_birth,
        "status": status,
        "created_at": animal.created_at.isoformat(),
        "images": [{"id": image.id, "width": image.width, "height": image.height, "format": image.image_format, "quality": __import__("json").loads(image.quality_json or "{}")} for image in animal.images],
    }


def _farmer_request_json(item: FarmerRequest):
    return {"id": item.id, "reference": item.reference, "kind": item.kind, "animal_id": item.animal_id, "description": item.description, "status": item.status, "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat()}


@app.get("/api/farmer/profile")
def farmer_profile(farmer_id=Depends(require_farmer)):
    with SessionFactory() as db:
        account = db.get(Farmer, farmer_id)
        return {"id": account.id, "full_name": account.full_name, "mobile": account.mobile, "email": account.email, "created_at": account.created_at.isoformat()}


@app.patch("/api/farmer/profile")
def update_farmer_profile(payload: FarmerProfileUpdate, farmer_id=Depends(require_farmer)):
    mobile = _normalize_mobile(payload.mobile)
    email = payload.email.lower().strip() if payload.email else None
    if email and not __import__("re").fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(422, "Enter a valid email address or leave it blank.")
    try:
        with SessionFactory.begin() as db:
            account = db.get(Farmer, farmer_id)
            account.full_name, account.mobile, account.email = payload.full_name, mobile, email
    except IntegrityError as exc:
        raise HTTPException(409, "That mobile number or email address is already used by another account.") from exc
    return {"updated": True}


@app.post("/api/farmer/password")
def change_farmer_password(payload: FarmerPasswordChange, response: Response, farmer_id=Depends(require_farmer)):
    with SessionFactory.begin() as db:
        account = db.get(Farmer, farmer_id)
        if not _password_matches(payload.current_password, account.password_hash):
            raise HTTPException(401, "Current password is incorrect.")
        account.password_hash = _password_hash(payload.new_password)
        account.session_version += 1
        version = account.session_version
    _issue_session(response, "farmer", farmer_id, version)
    return {"updated": True}


@app.get("/api/farmer/dashboard")
def farmer_dashboard(farmer_id=Depends(require_farmer)):
    today = date.today().isoformat()
    soon = (date.today() + timedelta(days=30)).isoformat()
    with SessionFactory.begin() as db:
        _refresh_policy_statuses(db)
        animal_ids = list(db.scalars(select(Animal.id).where(Animal.owner_id == farmer_id)).all())
        policy_rows = db.execute(select(InsurancePolicy).join(Animal, Animal.id == InsurancePolicy.animal_id).where(Animal.owner_id == farmer_id).order_by(InsurancePolicy.end_date.asc())).scalars().all()
        requests = db.scalars(select(FarmerRequest).where(FarmerRequest.farmer_id == farmer_id).order_by(FarmerRequest.created_at.desc())).all()
        history = db.scalars(select(Verification).where(Verification.farmer_id == farmer_id).order_by(Verification.created_at.desc()).limit(5)).all()
        return {
            "total_cattle": len(animal_ids),
            "insured_cattle": len({p.animal_id for p in policy_rows if p.status == "active"}),
            "upcoming_expirations": [{"animal_id": p.animal_id, "end_date": p.end_date, "policy_number": p.policy_number, "provider": p.provider} for p in policy_rows if p.status == "active" and today <= p.end_date <= soon],
            "recent_verifications": [{"id": v.id, "animal_id": v.animal_id, "decision": v.decision, "created_at": v.created_at.isoformat()} for v in history],
            "open_requests": sum(item.status in {"submitted", "under_review"} for item in requests),
        }


@app.get("/api/farmer/animals")
def farmer_animals(q: str = Query("", max_length=120), farmer_id=Depends(require_farmer)):
    with SessionFactory() as db:
        statement = select(Animal).where(Animal.owner_id == farmer_id)
        if q.strip():
            needle = f"%{q.strip()}%"
            statement = statement.where(or_(Animal.name.ilike(needle), Animal.ear_tag.ilike(needle), Animal.breed.ilike(needle), Animal.muzzle_id.ilike(needle)))
        animals = db.scalars(statement.order_by(Animal.created_at.desc(), Animal.id)).all()
        return {"items": [_farmer_animal_json(db, animal) for animal in animals], "total": len(animals)}


@app.get("/api/farmer/animals/{animal_id}")
def farmer_animal_detail(animal_id: str, farmer_id=Depends(require_farmer)):
    with SessionFactory() as db:
        animal = db.scalar(select(Animal).where(Animal.id == animal_id, Animal.owner_id == farmer_id))
        if animal is None:
            raise HTTPException(404, "Animal record not found.")
        result = _farmer_animal_json(db, animal)
        result["policies"] = [
            {key: value for key, value in _policy_json(item).items() if key != "notes"}
            for item in db.scalars(select(InsurancePolicy).where(InsurancePolicy.animal_id == animal_id)).all()
        ]
        result["verifications"] = [{"id": item.id, "decision": item.decision, "similarity": item.similarity, "reason": item.reason, "created_at": item.created_at.isoformat()} for item in db.scalars(select(Verification).where(Verification.farmer_id == farmer_id, Verification.animal_id == animal_id).order_by(Verification.created_at.desc())).all()]
        return result


@app.get("/api/farmer/animals/{animal_id}/images/{image_id}")
def farmer_animal_image(animal_id: str, image_id: str, farmer_id=Depends(require_farmer)):
    cfg = get_settings()
    try:
        key = data_key_bytes(cfg.get("data_key", ""))
    except RuntimeError as exc:
        raise HTTPException(503, "Encrypted image access is not configured.") from exc
    with SessionFactory() as db:
        image = db.scalar(select(AnimalImage).join(Animal, Animal.id == AnimalImage.animal_id).where(AnimalImage.id == image_id, AnimalImage.animal_id == animal_id, Animal.owner_id == farmer_id))
        if image is None:
            raise HTTPException(404, "Animal image not found.")
        path = _safe_upload_path(cfg["upload_dir"], image.storage_name)
        if not path.is_file():
            raise HTTPException(404, "Animal image not found.")
        try:
            raw = decrypt_bytes(path.read_bytes(), key, f"muzzleid-upload:{image.id}")
        except Exception as exc:
            log.exception("Farmer reference image failed integrity validation")
            raise HTTPException(503, "Stored image failed integrity validation.") from exc
    media_type = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(image.image_format)
    if not media_type:
        raise HTTPException(415, "Stored image format is not supported.")
    return Response(raw, media_type=media_type, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"})


@app.get("/api/farmer/insurance")
def farmer_insurance(farmer_id=Depends(require_farmer)):
    with SessionFactory.begin() as db:
        _refresh_policy_statuses(db)
        rows = db.execute(select(InsurancePolicy, Animal).join(Animal, Animal.id == InsurancePolicy.animal_id).where(Animal.owner_id == farmer_id).order_by(InsurancePolicy.end_date.asc())).all()
        return [{"id": policy.id, "animal_id": policy.animal_id, "provider": policy.provider, "policy_number": policy.policy_number, "coverage_amount": policy.coverage_amount, "start_date": policy.start_date, "end_date": policy.end_date, "status": policy.status, "animal_name": animal.name, "ear_tag": animal.ear_tag, "muzzle_id": animal.muzzle_id} for policy, animal in rows]


@app.get("/api/farmer/history")
def farmer_history(farmer_id=Depends(require_farmer)):
    with SessionFactory() as db:
        rows = db.scalars(select(Verification).where(Verification.farmer_id == farmer_id).order_by(Verification.created_at.desc()).limit(100)).all()
        return [{"id": row.id, "animal_id": row.animal_id, "decision": row.decision, "created_at": row.created_at.isoformat()} for row in rows]


@app.get("/api/farmer/requests")
def farmer_requests(farmer_id=Depends(require_farmer)):
    with SessionFactory() as db:
        rows = db.scalars(select(FarmerRequest).where(FarmerRequest.farmer_id == farmer_id).order_by(FarmerRequest.created_at.desc())).all()
        return [_farmer_request_json(row) for row in rows]


@app.get("/api/farmer/requests/{request_id}/events")
def farmer_request_events(request_id: str, farmer_id=Depends(require_farmer)):
    with SessionFactory() as db:
        item = db.scalar(select(FarmerRequest).where(FarmerRequest.id == request_id, FarmerRequest.farmer_id == farmer_id))
        if item is None:
            raise HTTPException(404, "Request not found.")
        rows = db.scalars(select(FarmerRequestEvent).where(FarmerRequestEvent.request_id == request_id).order_by(FarmerRequestEvent.created_at.asc())).all()
        return [{"previous_status": row.previous_status, "status": row.new_status, "updated_by": row.actor_role, "created_at": row.created_at.isoformat()} for row in rows]


@app.post("/api/farmer/requests", status_code=201)
def create_farmer_request(payload: FarmerRequestCreate, farmer_id=Depends(require_farmer)):
    with SessionFactory.begin() as db:
        if payload.animal_id and not db.scalar(select(Animal.id).where(Animal.id == payload.animal_id, Animal.owner_id == farmer_id)):
            raise HTTPException(404, "Animal record not found.")
        if payload.kind == "claim":
            if not payload.animal_id:
                raise HTTPException(422, "Choose the insured animal for this claim request.")
            _refresh_policy_statuses(db)
            policy = db.scalar(select(InsurancePolicy).join(Animal, Animal.id == InsurancePolicy.animal_id).where(InsurancePolicy.animal_id == payload.animal_id, Animal.owner_id == farmer_id, InsurancePolicy.status.in_(["active", "claim_reported"])))
            if policy is None:
                raise HTTPException(409, "A claim can be submitted only for an animal with an active policy record.")
            if policy.status != "active":
                raise HTTPException(409, "A claim can be submitted only during the policy coverage period.")
            policy.status = "claim_reported"
        now = datetime.now(timezone.utc)
        item = FarmerRequest(id=str(uuid4()), farmer_id=farmer_id, animal_id=payload.animal_id, kind=payload.kind, reference=f"MZ-REQ-{secrets.token_hex(4).upper()}", description=payload.description, status="submitted", created_at=now, updated_at=now)
        db.add(item)
        db.flush()
        db.add(FarmerRequestEvent(id=str(uuid4()), request_id=item.id, previous_status=None, new_status="submitted", actor_role="farmer", created_at=now))
        result = _farmer_request_json(item)
    return result


@app.get("/api/admin/farmer-requests", dependencies=[Depends(require_admin)])
def admin_farmer_requests():
    with SessionFactory() as db:
        rows = db.execute(select(FarmerRequest, Farmer, Animal).join(Farmer, Farmer.id == FarmerRequest.farmer_id).outerjoin(Animal, Animal.id == FarmerRequest.animal_id).order_by(FarmerRequest.created_at.desc())).all()
        return [{**_farmer_request_json(item), "farmer_name": farmer.full_name, "farmer_mobile": farmer.mobile, "animal_name": animal.name if animal else None} for item, farmer, animal in rows]


@app.patch("/api/admin/farmer-requests/{request_id}", dependencies=[Depends(require_admin)])
def update_admin_farmer_request(request_id: str, payload: FarmerRequestUpdate):
    with SessionFactory.begin() as db:
        item = db.get(FarmerRequest, request_id)
        if item is None:
            raise HTTPException(404, "Farmer request not found.")
        previous_status = item.status
        item.status = payload.status
        item.updated_at = datetime.now(timezone.utc)
        db.add(FarmerRequestEvent(id=str(uuid4()), request_id=item.id, previous_status=previous_status, new_status=item.status, actor_role="admin", created_at=item.updated_at))
        return _farmer_request_json(item)

static_dir = ROOT / "web"
if static_dir.exists():
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(static_dir / "index.html")


@app.get("/api/health", dependencies=[Depends(require_admin)])
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


@app.get("/api/animals", dependencies=[Depends(require_admin)])
def list_animals(q: str = Query("", max_length=120), offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100), sort: str = Query("created_at", pattern="^(created_at|name|ear_tag)$"), status: Literal["all", "prototype_ready", "pending_model", "reindex_required", "stale_model", "storage_error"] = "all"):
    with SessionFactory() as db:
        statement = select(Animal)
        if q.strip():
            needle = f"%{q.strip()}%"
            statement = statement.where(or_(Animal.name.ilike(needle), Animal.ear_tag.ilike(needle), Animal.breed.ilike(needle), Animal.id.ilike(needle), Animal.muzzle_id.ilike(needle)))
        if status != "all":
            matching_ids = [animal_id for animal_id, profile_status in _current_profile_statuses(db).items() if profile_status == status]
            statement = statement.where(Animal.id.in_(matching_ids))
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
        try:
            key = data_key_bytes(get_settings().get("data_key", ""))
        except Exception:
            key = None
        return {"items": [{"id": a.id, "muzzle_id": a.muzzle_id or (encrypted_muzzle_id(a.id, key) if key else ""), "name": a.name, "breed": a.breed, "ear_tag": a.ear_tag, "status": _effective_profile_status(a.status, image_ids_by_animal.get(a.id, []), versions_by_image, current_version), "image_count": len(image_ids_by_animal.get(a.id, [])), "created_at": a.created_at.isoformat()} for a in animals], "total": total, "offset": offset, "limit": limit}


@app.get("/api/animal-options", dependencies=[Depends(require_admin)])
def animal_options():
    with SessionFactory() as db:
        try:
            key = data_key_bytes(get_settings().get("data_key", ""))
        except Exception:
            key = None
        rows = db.execute(select(Animal.id, Animal.name, Animal.ear_tag, Animal.muzzle_id).order_by(Animal.name, Animal.id).limit(5000)).all()
        return [{"id": animal_id, "muzzle_id": muzzle_id or (encrypted_muzzle_id(animal_id, key) if key else ""), "label": " · ".join(part for part in (name or "Unnamed animal", ear_tag, (muzzle_id or (encrypted_muzzle_id(animal_id, key) if key else ""))[:14]) if part)} for animal_id, name, ear_tag, muzzle_id in rows]


@app.get("/api/animals/{animal_id}", dependencies=[Depends(require_admin)])
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
        policies = db.scalars(select(InsurancePolicy).where(InsurancePolicy.animal_id == animal_id).order_by(InsurancePolicy.created_at.desc())).all()
        verifications = db.scalars(select(Verification).where(Verification.animal_id == animal_id).order_by(Verification.created_at.desc()).limit(15)).all()
        try:
            key = data_key_bytes(get_settings().get("data_key", ""))
        except Exception:
            key = None
        mid = animal.muzzle_id or (encrypted_muzzle_id(animal.id, key) if key else "")
        return {
            "id": animal.id,
            "muzzle_id": mid,
            "name": animal.name,
            "breed": animal.breed,
            "ear_tag": animal.ear_tag,
            "sex": animal.sex,
            "date_of_birth": animal.date_of_birth,
            "status": status,
            "organization_id": animal.organization_id,
            "created_at": animal.created_at.isoformat(),
            "images": [{"id": image.id, "width": image.width, "height": image.height, "format": image.image_format, "quality": __import__("json").loads(image.quality_json or "{}"), "content_sha256": image.content_sha256} for image in animal.images],
            "policies": [_policy_json(p) for p in policies],
            "verifications": [{"id": v.id, "decision": v.decision, "similarity": v.similarity, "reason": v.reason, "created_at": v.created_at.isoformat()} for v in verifications],
        }


@app.patch("/api/animals/{animal_id}", dependencies=[Depends(require_admin)])
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


@app.delete("/api/animals/{animal_id}", status_code=204, dependencies=[Depends(require_admin)])
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


@app.post("/api/animals/{animal_id}/reindex", dependencies=[Depends(require_admin)])
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


@app.get("/api/animals/{animal_id}/images/{image_id}", dependencies=[Depends(require_admin)])
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


def registration_owner(request: Request):
    principal = require_session(request)
    if request.url.path == "/api/farmer/animals":
        return require_farmer(principal)
    require_admin(principal)
    return None


@app.post("/api/animals")
@app.post("/api/farmer/animals")
async def register_animal(request: Request, name: str = Form("", max_length=120), breed: str = Form("", max_length=120), ear_tag: str = Form("", max_length=120), sex: str = Form(""), date_of_birth: str = Form("", max_length=10), images: list[UploadFile] = File(...), idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128), owner_id=Depends(registration_owner)):
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
            if owner_id is not None and animal.owner_id != owner_id:
                raise HTTPException(409, "This registration request key is already in use.")
            mid = animal.muzzle_id or encrypted_muzzle_id(prior.animal_id, encryption_key)
            return {"id": prior.animal_id, "muzzle_id": mid, "name": animal.name, "ear_tag": animal.ear_tag, "status": animal.status, "image_count": len(animal.images), "persisted": True, "replayed": True, "message": "This registration request was already saved."}
    animal_id = str(uuid4())
    muzzle_id = encrypted_muzzle_id(animal_id, encryption_key)
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
            animal = Animal(id=animal_id, muzzle_id=muzzle_id, owner_id=owner_id, name=name[:120], breed=breed[:120], ear_tag=ear_tag[:120], sex=sex[:32], date_of_birth=date_of_birth[:32], status="prototype_ready" if vectors else "pending_model")
            db.add(animal)
            # These rows are linked by foreign keys, but ImageEmbedding has no
            # ORM relationship to AnimalImage. Flush the parent records before
            # adding their embeddings so databases with FK enforcement (SQLite
            # and PostgreSQL) always see the referenced rows first.
            db.flush()
            db.add_all([item for _, item in saved])
            db.flush()
            if vectors:
                db.add_all([ImageEmbedding(image_id=item.id, model_version=model_version, vector_encrypted=__import__("base64").urlsafe_b64encode(encrypt_bytes(__import__("json").dumps(vector, separators=(",", ":")).encode(), encryption_key, f"muzzleid-embedding:{item.id}:{model_version}")).decode("ascii")) for (_, item), vector in zip(saved, vectors)])
            db.add(RegistrationRequest(idempotency_key=idempotency_key, animal_id=animal_id))
        profile_status = "prototype_ready" if vectors else "pending_model"
        message = "Animal record and biometric profile are saved." if vectors else "Animal record and images are saved. Biometric profile creation is pending a configured model."
        return {"id": animal_id, "muzzle_id": muzzle_id, "name": name, "ear_tag": ear_tag, "status": profile_status, "image_count": len(saved), "persisted": True, "replayed": False, "message": message}
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
                if owner_id is not None and (animal is None or animal.owner_id != owner_id):
                    raise HTTPException(409, "This registration request key is already in use.")
                mid = (animal.muzzle_id or encrypted_muzzle_id(prior.animal_id, encryption_key)) if animal else ""
                return {"id": prior.animal_id, "muzzle_id": mid, "status": animal.status if animal else "pending_model", "image_count": len(animal.images) if animal else 0, "persisted": animal is not None, "replayed": True, "message": "This registration request was already saved."}
        log.exception("Animal registration failed")
        raise HTTPException(500, "Registration could not be saved.")


@app.post("/api/verify", dependencies=[Depends(require_admin)])
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
        matched_animal = None
        with SessionFactory.begin() as db:
            gallery = _load_current_gallery(db, version, encryption_key)
            animal_id, similarity, decision = match_embedding(vectors[0], gallery, index_path=get_settings()["index_path"], key=encryption_key)
            event_id = str(uuid4())
            db.add(Verification(id=event_id, animal_id=animal_id if decision != "UNKNOWN" else None, decision=decision, similarity=similarity, reason="Prototype full-image embedding with training-identity image-split calibration; human review required."))
            if decision == "REVIEW":
                db.add(OperationalAlert(id=str(uuid4()), fingerprint=f"verification-review:{event_id}", animal_id=animal_id, verification_id=event_id, category="verification_review", severity="medium", message="A prototype match requires human review. Verify the animal in person."))
            if animal_id:
                matched_record = db.get(Animal, animal_id)
                if matched_record:
                    matched_animal = {
                        "id": matched_record.id,
                        "muzzle_id": matched_record.muzzle_id or encrypted_muzzle_id(matched_record.id, encryption_key),
                        "name": matched_record.name,
                        "ear_tag": matched_record.ear_tag,
                        "breed": matched_record.breed,
                        "image_id": matched_record.images[0].id if matched_record.images else None,
                    }
    except Exception as exc:
        log.exception("Encrypted gallery lookup failed")
        raise HTTPException(503, "The encrypted biometric gallery is unavailable. No identity decision was made.") from exc
    return {"id": event_id, "animal_id": animal_id if decision != "UNKNOWN" else None, "decision": decision, "similarity": similarity, "model_version": version, "matched_animal": matched_animal, "prototype_only": True, "message": "Prototype result only. Confirm identity in person; do not use for ownership or insurance decisions."}


@app.post("/api/farmer/verify")
async def farmer_verify_animal(request: Request, image: UploadFile = File(...), farmer_id=Depends(require_farmer)):
    check_rate_limit(request, "farmer-verify", 20, 60)
    raw = await image.read(get_settings()["max_upload_bytes"] + 1)
    if len(raw) > get_settings()["max_upload_bytes"]:
        raise HTTPException(413, "Image must be 12 MB or smaller.")
    quality = inspect_image(raw)
    if not quality["valid"]:
        raise HTTPException(422, quality["reason"])
    try:
        vectors, version, calibration = embed_images([raw])
    except RuntimeError as exc:
        raise HTTPException(503, "Biometric verification is unavailable: trained model and calibration artifacts are not configured.") from exc
    except Exception as exc:
        log.exception("Farmer verification embedding failed")
        raise HTTPException(503, "Biometric verification could not process this image.") from exc
    try:
        key = data_key_bytes(get_settings().get("data_key", ""))
        import numpy as np
        with SessionFactory.begin() as db:
            rows = db.execute(select(Animal.id, AnimalImage.id, ImageEmbedding.vector_encrypted)
                              .join(AnimalImage, AnimalImage.animal_id == Animal.id)
                              .join(ImageEmbedding, ImageEmbedding.image_id == AnimalImage.id)
                              .where(Animal.owner_id == farmer_id, Animal.status == "prototype_ready", ImageEmbedding.model_version == version)).all()
            vectors_by_animal = {}
            for animal_id, image_id, encrypted in rows:
                blob = base64.urlsafe_b64decode(encrypted.encode("ascii"))
                vector = np.asarray(__import__("json").loads(decrypt_bytes(blob, key, f"muzzleid-embedding:{image_id}:{version}").decode("utf-8")), dtype=np.float32)
                vectors_by_animal.setdefault(animal_id, []).append(vector)
            query = np.asarray(vectors[0], dtype=np.float32)
            query /= max(float(np.linalg.norm(query)), 1e-12)
            best_id, best_score = None, None
            for animal_id, refs in vectors_by_animal.items():
                center = np.mean(refs, axis=0)
                norm = float(np.linalg.norm(center))
                if norm <= 1e-12:
                    continue
                score = float(np.dot(query, center / norm))
                if best_score is None or score > best_score:
                    best_id, best_score = animal_id, score
            if best_id is None:
                decision, similarity = "UNKNOWN", None
            else:
                decision = decide(best_score, verified_threshold=float(calibration["verified_threshold"]), review_threshold=float(calibration["review_threshold"]))
                similarity = best_score
            event_id = str(uuid4())
            db.add(Verification(id=event_id, farmer_id=farmer_id, animal_id=best_id if decision != "UNKNOWN" else None, decision=decision, similarity=similarity, reason="Farmer-scoped prototype match; human confirmation required."))
            if decision == "REVIEW":
                db.add(OperationalAlert(id=str(uuid4()), fingerprint=f"verification-review:{event_id}", animal_id=best_id, verification_id=event_id, category="verification_review", severity="medium", message="A farmer-scoped prototype match requires human review."))
            animal = db.get(Animal, best_id) if best_id and decision != "UNKNOWN" else None
            matched = {"id": animal.id, "name": animal.name, "ear_tag": animal.ear_tag, "muzzle_id": animal.muzzle_id} if animal else None
    except Exception as exc:
        log.exception("Farmer-scoped encrypted gallery lookup failed")
        raise HTTPException(503, "Your enrolled biometric profiles could not be checked. No identity decision was made.") from exc
    return {"id": event_id, "animal_id": animal.id if animal else None, "decision": decision, "similarity": similarity, "matched_animal": matched, "prototype_only": True, "message": "Prototype suggestion only. Confirm identity in person; do not use as proof of ownership or for insurance decisions."}


@app.get("/api/history", dependencies=[Depends(require_admin)])
def history():
    with SessionFactory() as db:
        rows = db.execute(select(Verification).order_by(Verification.created_at.desc()).limit(200)).scalars().all()
        animal_ids = [x.animal_id for x in rows if x.animal_id]
        animals_map = {}
        if animal_ids:
            animal_rows = db.scalars(select(Animal).where(Animal.id.in_(animal_ids))).all()
            try:
                key = data_key_bytes(get_settings().get("data_key", ""))
            except Exception:
                key = None
            animals_map = {a.id: {"name": a.name, "ear_tag": a.ear_tag, "breed": a.breed, "muzzle_id": a.muzzle_id or (encrypted_muzzle_id(a.id, key) if key else "")} for a in animal_rows}
        return [{
            "id": x.id,
            "animal_id": x.animal_id,
            "animal_name": animals_map.get(x.animal_id, {}).get("name", ""),
            "ear_tag": animals_map.get(x.animal_id, {}).get("ear_tag", ""),
            "breed": animals_map.get(x.animal_id, {}).get("breed", ""),
            "muzzle_id": animals_map.get(x.animal_id, {}).get("muzzle_id", ""),
            "decision": x.decision,
            "similarity": x.similarity,
            "reason": x.reason,
            "created_at": x.created_at.isoformat()
        } for x in rows]


def _policy_json(policy):
    return {"id": policy.id, "animal_id": policy.animal_id, "provider": policy.provider, "policy_number": policy.policy_number, "coverage_amount": policy.coverage_amount, "start_date": policy.start_date, "end_date": policy.end_date, "status": policy.status, "notes": policy.notes, "created_at": policy.created_at.isoformat()}


def _refresh_policy_statuses(db):
    today = date.today().isoformat()
    db.execute(update(InsurancePolicy).where(InsurancePolicy.status == "active", InsurancePolicy.end_date < today).values(status="expired"))
    db.execute(update(InsurancePolicy).where(InsurancePolicy.status == "pending", InsurancePolicy.end_date < today).values(status="expired"))
    db.execute(update(InsurancePolicy).where(InsurancePolicy.status == "pending", InsurancePolicy.start_date <= today, InsurancePolicy.end_date >= today).values(status="active"))


@app.get("/api/insurance/policies", dependencies=[Depends(require_admin)])
def insurance_policies(status: Literal["all", "pending", "active", "expired", "cancelled", "claim_reported"] = "all"):
    with SessionFactory.begin() as db:
        _refresh_policy_statuses(db)
        statement = select(InsurancePolicy).order_by(InsurancePolicy.end_date.asc(), InsurancePolicy.created_at.desc())
        if status != "all":
            statement = statement.where(InsurancePolicy.status == status)
        policies = db.scalars(statement).all()
        animal_ids = [p.animal_id for p in policies]
        animals_map = {}
        if animal_ids:
            animal_rows = db.scalars(select(Animal).where(Animal.id.in_(animal_ids))).all()
            try:
                key = data_key_bytes(get_settings().get("data_key", ""))
            except Exception:
                key = None
            animals_map = {a.id: {"name": a.name, "ear_tag": a.ear_tag, "muzzle_id": a.muzzle_id or (encrypted_muzzle_id(a.id, key) if key else "")} for a in animal_rows}
        result = []
        for policy in policies:
            data = _policy_json(policy)
            info = animals_map.get(policy.animal_id, {})
            data["animal_name"] = info.get("name", "")
            data["ear_tag"] = info.get("ear_tag", "")
            data["muzzle_id"] = info.get("muzzle_id", "")
            result.append(data)
        return result


@app.post("/api/insurance/policies", status_code=201, dependencies=[Depends(require_admin)])
def create_insurance_policy(payload: InsurancePolicyCreate):
    if payload.end_date < payload.start_date:
        raise HTTPException(422, "Policy end date must be on or after its start date.")
    with SessionFactory.begin() as db:
        animal = db.get(Animal, payload.animal_id)
        if animal is None:
            raise HTTPException(404, "Animal record not found.")
        policy = InsurancePolicy(id=str(uuid4()), animal_id=payload.animal_id, provider=payload.provider, policy_number=payload.policy_number, coverage_amount=payload.coverage_amount, start_date=payload.start_date.isoformat(), end_date=payload.end_date.isoformat(), status="expired" if payload.end_date < date.today() else "pending" if payload.start_date > date.today() else "active", notes=payload.notes)
        db.add(policy)
        db.flush()
        result = _policy_json(policy)
        try:
            key = data_key_bytes(get_settings().get("data_key", ""))
        except Exception:
            key = None
        result["animal_name"] = animal.name
        result["ear_tag"] = animal.ear_tag
        result["muzzle_id"] = animal.muzzle_id or (encrypted_muzzle_id(animal.id, key) if key else "")
    return result


@app.patch("/api/insurance/policies/{policy_id}", dependencies=[Depends(require_admin)])
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


@app.delete("/api/insurance/policies/{policy_id}", status_code=204, dependencies=[Depends(require_admin)])
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


@app.get("/api/alerts", dependencies=[Depends(require_admin)])
def alerts(status: Literal["all", "open", "resolved"] = "open"):
    with SessionFactory.begin() as db:
        _refresh_duplicate_tag_alerts(db)
        statement = select(OperationalAlert).order_by(OperationalAlert.created_at.desc())
        if status != "all":
            statement = statement.where(OperationalAlert.status == status)
        rows = db.scalars(statement.limit(500)).all()
        animal_ids = [row.animal_id for row in rows if row.animal_id]
        animals_map = {}
        if animal_ids:
            animal_rows = db.scalars(select(Animal).where(Animal.id.in_(animal_ids))).all()
            try:
                key = data_key_bytes(get_settings().get("data_key", ""))
            except Exception:
                key = None
            animals_map = {a.id: {"name": a.name, "ear_tag": a.ear_tag, "muzzle_id": a.muzzle_id or (encrypted_muzzle_id(a.id, key) if key else "")} for a in animal_rows}
        return [{
            "id": row.id,
            "animal_id": row.animal_id,
            "animal_name": animals_map.get(row.animal_id, {}).get("name", ""),
            "ear_tag": animals_map.get(row.animal_id, {}).get("ear_tag", ""),
            "muzzle_id": animals_map.get(row.animal_id, {}).get("muzzle_id", ""),
            "verification_id": row.verification_id,
            "category": row.category,
            "severity": row.severity,
            "message": row.message,
            "status": row.status,
            "created_at": row.created_at.isoformat(),
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None
        } for row in rows]


@app.patch("/api/alerts/{alert_id}", dependencies=[Depends(require_admin)])
def update_alert(alert_id: str, payload: AlertUpdate):
    with SessionFactory.begin() as db:
        alert = db.get(OperationalAlert, alert_id)
        if alert is None:
            raise HTTPException(404, "Alert not found.")
        alert.status = payload.status
        alert.resolved_at = datetime.now(timezone.utc) if payload.status == "resolved" else None
        return {"id": alert.id, "status": alert.status, "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None}


@app.get("/api/analytics", dependencies=[Depends(require_admin)])
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
        total_coverage = db.scalar(select(func.sum(InsurancePolicy.coverage_amount)).where(InsurancePolicy.status == "active")) or 0.0
        open_alerts = db.scalar(select(func.count()).select_from(OperationalAlert).where(OperationalAlert.status == "open")) or 0
        registration_rows = db.execute(select(func.date(Animal.created_at), func.count()).where(Animal.created_at >= since).group_by(func.date(Animal.created_at)).order_by(func.date(Animal.created_at))).all()
        registrations = [{"date": str(day), "count": count} for day, count in registration_rows]
        return {
            "total_animals": total_animals,
            "animal_profiles": animal_status,
            "verification_today": verified_today,
            "verification_last_30_days": {decision: decisions.get(decision, 0) for decision in ("VERIFIED", "REVIEW", "UNKNOWN")},
            "policies": policy_status,
            "total_active_coverage": round(float(total_coverage), 2),
            "open_alerts": open_alerts,
            "registrations_last_30_days": registrations
        }


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


@app.get("/api/reports/animals.csv", dependencies=[Depends(require_admin)])
def report_animals():
    with SessionFactory() as db:
        statuses = _current_profile_statuses(db)
        try:
            key = data_key_bytes(get_settings().get("data_key", ""))
        except Exception:
            key = None
        rows = [
            (
                animal.id,
                animal.muzzle_id or (encrypted_muzzle_id(animal.id, key) if key else ""),
                animal.name,
                animal.ear_tag,
                animal.breed,
                animal.sex,
                animal.date_of_birth,
                statuses[animal.id],
                animal.created_at,
            )
            for animal in db.scalars(select(Animal).order_by(Animal.created_at)).all()
        ]
        return _csv_response("muzzleid-animals.csv", ("id", "muzzle_id", "name", "ear_tag", "breed", "sex", "date_of_birth", "profile_status", "created_at"), rows)


@app.get("/api/reports/verifications.csv", dependencies=[Depends(require_admin)])
def report_verifications():
    with SessionFactory() as db:
        records = db.execute(select(Verification.id, Verification.animal_id, Verification.decision, Verification.similarity, Verification.reason, Verification.created_at).order_by(Verification.created_at.desc())).all()
        animal_ids = [r[1] for r in records if r[1]]
        animals_map = {}
        if animal_ids:
            animal_rows = db.scalars(select(Animal).where(Animal.id.in_(animal_ids))).all()
            try:
                key = data_key_bytes(get_settings().get("data_key", ""))
            except Exception:
                key = None
            animals_map = {a.id: {"name": a.name, "ear_tag": a.ear_tag, "muzzle_id": a.muzzle_id or (encrypted_muzzle_id(a.id, key) if key else "")} for a in animal_rows}
        rows = [
            (
                v_id,
                a_id or "",
                animals_map.get(a_id, {}).get("name", "") if a_id else "",
                animals_map.get(a_id, {}).get("ear_tag", "") if a_id else "",
                animals_map.get(a_id, {}).get("muzzle_id", "") if a_id else "",
                decision,
                similarity,
                reason,
                created_at,
            )
            for v_id, a_id, decision, similarity, reason, created_at in records
        ]
        return _csv_response("muzzleid-verifications.csv", ("verification_id", "animal_id", "animal_name", "ear_tag", "muzzle_id", "decision", "similarity", "reason", "created_at"), rows)


@app.get("/api/reports/insurance.csv", dependencies=[Depends(require_admin)])
def report_insurance():
    with SessionFactory.begin() as db:
        _refresh_policy_statuses(db)
        policies = db.scalars(select(InsurancePolicy).order_by(InsurancePolicy.created_at.desc())).all()
        animal_ids = [p.animal_id for p in policies]
        animals_map = {}
        if animal_ids:
            animal_rows = db.scalars(select(Animal).where(Animal.id.in_(animal_ids))).all()
            try:
                key = data_key_bytes(get_settings().get("data_key", ""))
            except Exception:
                key = None
            animals_map = {a.id: {"name": a.name, "ear_tag": a.ear_tag, "muzzle_id": a.muzzle_id or (encrypted_muzzle_id(a.id, key) if key else "")} for a in animal_rows}
        rows = [
            (
                p.id,
                p.animal_id,
                animals_map.get(p.animal_id, {}).get("name", ""),
                animals_map.get(p.animal_id, {}).get("ear_tag", ""),
                animals_map.get(p.animal_id, {}).get("muzzle_id", ""),
                p.provider,
                p.policy_number,
                p.coverage_amount,
                p.start_date,
                p.end_date,
                p.status,
                p.created_at,
            )
            for p in policies
        ]
        return _csv_response(
            "muzzleid-insurance-policies.csv",
            ("policy_id", "animal_id", "animal_name", "ear_tag", "muzzle_id", "provider", "policy_number", "coverage_amount", "start_date", "end_date", "status", "created_at"),
            rows,
        )


@app.get("/api/settings", dependencies=[Depends(require_admin)])
def settings_status():
    cfg = get_settings()
    pipeline = model_state()
    try:
        data_key_bytes(cfg.get("data_key", ""))
        encryption_configured = True
    except RuntimeError:
        encryption_configured = False
    return {
        "operator_password_configured": len(cfg.get("operator_password", "")) >= 12,
        "session_secret_configured": len(cfg.get("session_secret", "")) >= 32,
        "data_encryption_configured": encryption_configured,
        "cookie_secure": cfg.get("cookie_secure", False),
        "model_available": pipeline["available"],
        "model_version": pipeline.get("model_version"),
        "faiss_available": faiss_available(),
        "database_type": "postgresql" if cfg["database_url"].startswith("postgresql") else "sqlite",
        "faiss_gallery_identities": ENGINE_STARTUP_STATE.get("gallery_identities", 0),
        "embedding_dimensions": 512,
        "max_upload_mb": cfg["max_upload_bytes"] // (1024 * 1024),
        "max_registration_mb": cfg["max_registration_bytes"] // (1024 * 1024),
        "limitations": pipeline.get("limitations", []),
    }
