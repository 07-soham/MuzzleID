from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from src.database.models import Base


def _config(database):
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    config.set_main_option("script_location", str(root / "migrations"))
    return config


def test_upgrade_from_0005_adds_owner_links_without_assigning_old_animals(tmp_path):
    database = tmp_path / "legacy.sqlite"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE animals (id VARCHAR(36) PRIMARY KEY, name VARCHAR(120) NOT NULL DEFAULT '')"))
        connection.execute(text("CREATE TABLE verifications (id VARCHAR(36) PRIMARY KEY, animal_id VARCHAR(36), decision VARCHAR(16) NOT NULL)"))
        connection.execute(text("INSERT INTO animals (id, name) VALUES ('legacy-animal', 'Existing record')"))
    engine.dispose()

    config = _config(database)
    command.stamp(config, "0005_encrypted_muzzle_id")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database}")
    inspector = inspect(engine)
    assert "owner_id" in {col["name"] for col in inspector.get_columns("animals")}
    assert "farmer_id" in {col["name"] for col in inspector.get_columns("verifications")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT owner_id FROM animals WHERE id='legacy-animal'")).scalar_one() is None
    engine.dispose()


def test_upgrade_is_safe_after_create_all_baseline_has_current_metadata(tmp_path):
    database = tmp_path / "baseline.sqlite"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    engine.dispose()

    config = _config(database)
    command.stamp(config, "0005_encrypted_muzzle_id")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database}")
    inspector = inspect(engine)
    assert "farmer_request_events" in inspector.get_table_names()
    assert "session_version" in {col["name"] for col in inspector.get_columns("farmers")}
    engine.dispose()


def test_upgrade_repairs_farmer_table_when_0006_was_recorded_without_session_version(tmp_path):
    database = tmp_path / "incomplete-farmers.sqlite"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE farmers (
                id VARCHAR(36) PRIMARY KEY,
                full_name VARCHAR(120) NOT NULL,
                mobile VARCHAR(32) NOT NULL UNIQUE,
                email VARCHAR(254) UNIQUE,
                password_hash VARCHAR(256) NOT NULL,
                created_at DATETIME NOT NULL
            )
        """))
    engine.dispose()

    config = _config(database)
    command.stamp(config, "0006_farmer_portals")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database}")
    columns = {col["name"]: col for col in inspect(engine).get_columns("farmers")}
    assert "session_version" in columns
    assert columns["session_version"]["nullable"] is False
    engine.dispose()
