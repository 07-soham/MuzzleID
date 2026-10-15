"""SQLAlchemy MVP records; embeddings are kept separately in a vector index."""
from datetime import datetime, timezone
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
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
    animal: Mapped[Animal] = relationship(back_populates="images")


class ImageEmbedding(Base):
    """Embedding derived from one reference image by a specific model version."""
    __tablename__ = "image_embeddings"
    image_id: Mapped[str] = mapped_column(ForeignKey("animal_images.id", ondelete="CASCADE"), primary_key=True)
    model_version: Mapped[str] = mapped_column(String(64), index=True)
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


def make_session_factory():
    url = get_settings()["database_url"]
    kwargs = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=kwargs, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
