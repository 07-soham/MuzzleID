"""SQLAlchemy MVP records; embeddings are kept separately in a vector index."""
from datetime import datetime, timezone
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from alembic import command
from alembic.config import Config
from src.config import get_settings


class Base(DeclarativeBase):
    pass


class Animal(Base):
    __tablename__ = "animals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    breed: Mapped[str] = mapped_column(String(120), default="")
    ear_tag: Mapped[str] = mapped_column(String(120), default="", index=True)
    sex: Mapped[str] = mapped_column(String(32), default="")
    date_of_birth: Mapped[str] = mapped_column(String(32), default="")
    organization_id: Mapped[str] = mapped_column(String(120), default="default")
    status: Mapped[str] = mapped_column(String(32), default="pending_model")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    images: Mapped[list["AnimalImage"]] = relationship(back_populates="animal", cascade="all, delete-orphan")


class AnimalImage(Base):
    __tablename__ = "animal_images"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    animal_id: Mapped[str] = mapped_column(ForeignKey("animals.id"), index=True)
    storage_name: Mapped[str] = mapped_column(String(255), unique=True)
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    image_format: Mapped[str] = mapped_column(String(8), default="JPEG")
    quality_json: Mapped[str] = mapped_column(Text, default="{}")
    animal: Mapped[Animal] = relationship(back_populates="images")


class ImageEmbedding(Base):
    """Versioned embedding derived from one reference image."""
    __tablename__ = "image_embeddings"
    image_id: Mapped[str] = mapped_column(ForeignKey("animal_images.id", ondelete="CASCADE"), primary_key=True)
    model_version: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    vector_encrypted: Mapped[str] = mapped_column(Text)


class RegistrationRequest(Base):
    """Persisted idempotency key so a retried multipart request does not create another animal."""
    __tablename__ = "registration_requests"
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    animal_id: Mapped[str] = mapped_column(ForeignKey("animals.id"), unique=True)


class Verification(Base):
    __tablename__ = "verifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    animal_id: Mapped[str | None] = mapped_column(ForeignKey("animals.id"), nullable=True)
    decision: Mapped[str] = mapped_column(String(16))
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class InsurancePolicy(Base):
    """Operator-maintained policy metadata; no identity or claim decision is implied."""
    __tablename__ = "insurance_policies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    animal_id: Mapped[str] = mapped_column(ForeignKey("animals.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(120))
    policy_number: Mapped[str] = mapped_column(String(120))
    coverage_amount: Mapped[float] = mapped_column(Float, default=0)
    start_date: Mapped[str] = mapped_column(String(10))
    end_date: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(16), default="active")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class OperationalAlert(Base):
    __tablename__ = "operational_alerts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    animal_id: Mapped[str | None] = mapped_column(ForeignKey("animals.id", ondelete="SET NULL"), nullable=True, index=True)
    verification_id: Mapped[str | None] = mapped_column(ForeignKey("verifications.id", ondelete="CASCADE"), nullable=True, unique=True)
    category: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def make_session_factory():
    url = get_settings()["database_url"]
    kwargs = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=kwargs, pool_pre_ping=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
    migration_config = Config(str(__import__("pathlib").Path(__file__).resolve().parents[2] / "alembic.ini"))
    migration_config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    migration_config.set_main_option("script_location", str(__import__("pathlib").Path(__file__).resolve().parents[2] / "migrations"))
    command.upgrade(migration_config, "head")
    return sessionmaker(bind=engine, expire_on_commit=False)
