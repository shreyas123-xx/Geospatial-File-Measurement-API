"""Pydantic response models (these also drive the OpenAPI docs)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class LayerOut(BaseModel):
    name: str
    dataset: str
    feature_count: int
    crs: str | None
    geometry_types: dict[str, int]


class FileOut(BaseModel):
    id: str = Field(examples=["3f2b8c1e9a4d4f6e8b7a1c2d3e4f5a6b"])
    filename: str
    file_type: str = Field(description="shapefile | kml | kmz")
    size_bytes: int
    status: str = Field(description="PENDING | PROCESSING | COMPLETED | FAILED")
    feature_count: int
    crs: str | None = Field(description="Common CRS of all layers, or MIXED")
    crs_assumed: bool = Field(description="True when the CRS came from `assume_crs`, not the file")
    layers: list[LayerOut]
    geometry_type_counts: dict[str, int]
    warnings: list[str]
    error: str | None
    created_at: datetime
    completed_at: datetime | None
    links: dict[str, str]


class FileList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[FileOut]


class GeodesicOut(BaseModel):
    area: float | None = None
    perimeter: float | None = None
    length: float | None = None


class MeasurementsOut(BaseModel):
    area: float | None = Field(None, description="Polygon area (requested area unit)")
    perimeter: float | None = Field(None, description="Polygon perimeter (requested length unit)")
    length: float | None = Field(None, description="Line length (requested length unit)")
    geodesic: GeodesicOut | None = Field(None, description="Independent ellipsoidal cross-check")
    max_deviation_pct: float | None = Field(None, description="Largest projected-vs-geodesic difference, %")
    projection: dict[str, str] | None = Field(None, description="Local projections used (PROJ strings)")


class FeatureMeasurement(BaseModel):
    index: int
    fid: int | None
    layer: str
    geometry_type: str | None
    crs: str | None
    status: str = Field(description="MEASURED | NOT_APPLICABLE | UNSUPPORTED | SKIPPED | ERROR")
    message: str | None
    warnings: list[str]
    measurements: MeasurementsOut | None
    properties: dict[str, Any] | None = None
    geometry: dict[str, Any] | None = None


class Units(BaseModel):
    area: str
    length: str


class GeometryTypeSummary(BaseModel):
    count: int
    total_area: float | None = None
    total_perimeter: float | None = None
    total_length: float | None = None


class Summary(BaseModel):
    feature_count: int
    by_status: dict[str, int]
    by_geometry_type: dict[str, GeometryTypeSummary]
    total_area: float
    total_perimeter: float
    total_length: float


class Pagination(BaseModel):
    total: int
    limit: int
    offset: int


class MeasurementsResponse(BaseModel):
    file_id: str
    filename: str
    status: str
    units: Units
    summary: Summary
    pagination: Pagination
    features: list[FeatureMeasurement]
