"""Environment-backed application settings."""
from functools import lru_cache
from pathlib import Path
import os
from sqlalchemy.engine import URL

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


@lru_cache
def get_settings():
    database_url = os.getenv("DATABASE_URL")
    if not database_url and os.getenv("POSTGRES_HOST"):
        database_url = URL.create(
            "postgresql+psycopg",
            username=os.getenv("POSTGRES_USER", "muzzleid"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
            host=os.environ["POSTGRES_HOST"],
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            database=os.getenv("POSTGRES_DB", "muzzleid"),
        ).render_as_string(hide_password=False)
    max_upload_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(12 * 1024 * 1024)))
    max_registration_bytes = int(os.getenv("MAX_REGISTRATION_BYTES", str(32 * 1024 * 1024)))
    if not 1 <= max_upload_bytes <= 25 * 1024 * 1024:
        raise RuntimeError("MAX_UPLOAD_BYTES must be between 1 byte and 25 MiB.")
    if not max_upload_bytes <= max_registration_bytes <= 128 * 1024 * 1024:
        raise RuntimeError("MAX_REGISTRATION_BYTES must be at least MAX_UPLOAD_BYTES and no more than 128 MiB.")
    return {
        "database_url": database_url or f"sqlite:///{ROOT / 'muzzleid.db'}",
        "model_path": Path(os.getenv("MODEL_PATH", str(ROOT / "models" / "best_model.pt"))).resolve(),
        "calibration_path": Path(os.getenv("CALIBRATION_PATH", str(ROOT / "models" / "calibration.json"))).resolve(),
        "upload_dir": Path(os.getenv("UPLOAD_DIR", str(ROOT / "storage" / "uploads"))),
        "index_path": Path(os.getenv("GALLERY_INDEX_PATH", str(ROOT / "storage" / "gallery.index.enc"))).resolve(),
        "max_upload_bytes": max_upload_bytes,
        "max_registration_bytes": max_registration_bytes,
        "operator_password": os.getenv("MUZZLEID_OPERATOR_PASSWORD", ""),
        "session_secret": os.getenv("MUZZLEID_SESSION_SECRET", ""),
        "data_key": os.getenv("MUZZLEID_DATA_KEY", ""),
        "cookie_secure": os.getenv("COOKIE_SECURE", "false").lower() == "true",
    }
