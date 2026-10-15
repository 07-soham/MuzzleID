"""Environment-backed application settings."""
from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


@lru_cache
def get_settings():
    return {
        "database_url": os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'muzzleid.db'}"),
        "model_path": Path(os.getenv("MODEL_PATH", str(ROOT / "models" / "best_model.pt"))).resolve(),
        "calibration_path": Path(os.getenv("CALIBRATION_PATH", str(ROOT / "models" / "calibration.json"))).resolve(),
        "upload_dir": Path(os.getenv("UPLOAD_DIR", str(ROOT / "storage" / "uploads"))),
        "max_upload_bytes": int(os.getenv("MAX_UPLOAD_BYTES", str(12 * 1024 * 1024))),
        "operator_password": os.getenv("MUZZLEID_OPERATOR_PASSWORD", ""),
        "session_secret": os.getenv("MUZZLEID_SESSION_SECRET", ""),
        "data_key": os.getenv("MUZZLEID_DATA_KEY", ""),
        "cookie_secure": os.getenv("COOKIE_SECURE", "false").lower() == "true",
    }
