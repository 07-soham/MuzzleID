"""Encrypt legacy plaintext reference images referenced by the local database."""
import argparse
from pathlib import Path

from sqlalchemy import select

from src.config import get_settings
from src.database.models import AnimalImage, make_session_factory
from src.security.crypto import MAGIC, data_key_bytes, encrypt_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Encrypt legacy files and update database names. Default is a read-only preview.")
    args = parser.parse_args()
    cfg = get_settings()
    key = data_key_bytes(cfg.get("data_key", ""))
    session_factory = make_session_factory()
    prepared = []
    committed = False
    cleanup_failures = 0
    try:
        with session_factory() as db:
            images = db.scalars(select(AnimalImage)).all()
            for item in images:
                source = (cfg["upload_dir"] / item.storage_name).resolve()
                if source.parent != cfg["upload_dir"].resolve() or not source.is_file():
                    raise RuntimeError("A referenced image file is missing or outside the configured upload directory.")
                content = source.read_bytes()
                if content.startswith(MAGIC):
                    continue
                target = cfg["upload_dir"] / f"{item.id}.enc"
                if target.exists() and target != source:
                    raise RuntimeError("A migration target already exists; inspect storage before retrying.")
                if args.apply:
                    temporary = target.with_suffix(".enc.tmp")
                    temporary.write_bytes(encrypt_bytes(content, key, f"muzzleid-upload:{item.id}"))
                    temporary.replace(target)
                    prepared.append((item, source, target))
                    item.storage_name = target.name
            if args.apply:
                db.commit()
                committed = True
        if args.apply:
            for _, source, target in prepared:
                if source != target:
                    try:
                        source.unlink(missing_ok=True)
                    except OSError:
                        cleanup_failures += 1
        count = len(prepared) if args.apply else sum(1 for item in images if not (cfg["upload_dir"] / item.storage_name).read_bytes().startswith(MAGIC))
        print(f"{'Encrypted' if args.apply else 'Would encrypt'} {count} existing image(s).")
        if cleanup_failures:
            print(f"WARNING: {cleanup_failures} plaintext source file(s) could not be removed; delete them manually after confirming encrypted copies.")
    except Exception:
        if not committed:
            for _, _, target in prepared:
                target.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
