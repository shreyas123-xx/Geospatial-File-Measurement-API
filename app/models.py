"""ORM models: one row per uploaded file, one row per extracted feature."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FileStatus:
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FeatureStatus:
    MEASURED = "MEASURED"                # area and/or length computed
    NOT_APPLICABLE = "NOT_APPLICABLE"    # e.g. points - nothing to measure
    UNSUPPORTED = "UNSUPPORTED"          # geometry could not be interpreted
    SKIPPED = "SKIPPED"                  # null / empty geometry
    ERROR = "ERROR"                      # unexpected failure on this feature only


class FileRecord(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    file_type: Mapped[str] = mapped_column(String(16))  # shapefile | kml | kmz
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), index=True, default=FileStatus.PENDING)
    crs: Mapped[str | None] = mapped_column(String(256), nullable=True)
    crs_assumed: Mapped[bool] = mapped_column(Boolean, default=False)
    assume_crs: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feature_count: Mapped[int] = mapped_column(Integer, default=0)
    layers: Mapped[list] = mapped_column(JSON, default=list)
    geometry_type_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_dir: Mapped[str] = mapped_column(String(1024))
    datasets: Mapped[list] = mapped_column(JSON, default=list)  # relative paths inside storage_dir
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FeatureRecord(Base):
    __tablename__ = "features"
    __table_args__ = (Index("ix_features_file_idx", "file_id", "idx", unique=True),)

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)          # 0-based position across the whole file
    fid: Mapped[int | None] = mapped_column(Integer, nullable=True)  # id inside the source layer
    layer: Mapped[str] = mapped_column(String(256))
    geometry_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    crs: Mapped[str | None] = mapped_column(String(256), nullable=True)
    geometry: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # GeoJSON, source CRS
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    measurement_status: Mapped[str] = mapped_column(String(16))
    measurements: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # SI units
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
