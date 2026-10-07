"""HTTP routes for /api/files/."""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import timezone
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import shapely
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from shapely.geometry import mapping, shape
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import FeatureRecord, FileRecord, FileStatus
from app.schemas import (
    FeatureMeasurement, FileList, FileOut, GeometryTypeSummary, MeasurementsOut,
    MeasurementsResponse, Pagination, Summary, Units,
)
from app.services.ingest import IngestError, ingest_upload, remove_storage
from app.services.processing import process_file
from app.units import AreaUnit, LengthUnit, area_from_m2, length_from_m

router = APIRouter(prefix="/api/files", tags=["files"])

Db = Annotated[Session, Depends(get_session)]


def err(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _file_out(rec: FileRecord) -> FileOut:
    base = f"/api/files/{rec.id}/"
    return FileOut(
        id=rec.id, filename=rec.filename, file_type=rec.file_type, size_bytes=rec.size_bytes,
        status=rec.status, feature_count=rec.feature_count, crs=rec.crs, crs_assumed=rec.crs_assumed,
        layers=rec.layers or [], geometry_type_counts=rec.geometry_type_counts or {},
        warnings=rec.warnings or [], error=rec.error,
        created_at=_aware(rec.created_at), completed_at=_aware(rec.completed_at),
        links={"self": base, "measurements": base + "measurements/", "features": base + "features/"},
    )


def _get_file(db: Session, file_id: str) -> FileRecord:
    rec = db.get(FileRecord, file_id)
    if rec is None:
        raise err(404, "file_not_found", f"No file with id '{file_id}'.")
    return rec


def _require_completed(rec: FileRecord) -> None:
    if rec.status == FileStatus.COMPLETED:
        return
    if rec.status == FileStatus.FAILED:
        raise err(409, "processing_failed", rec.error or "File processing failed.", file_status=rec.status)
    raise err(409, "not_ready", "File is still being processed; retry shortly.", file_status=rec.status)


def _convert(m: dict | None, area_unit: str, length_unit: str) -> MeasurementsOut | None:
    if not m:
        return None

    def block(src: dict) -> dict:
        return {
            "area": _r(area_from_m2(src.get("area_m2"), area_unit)),
            "perimeter": _r(length_from_m(src.get("perimeter_m"), length_unit)),
            "length": _r(length_from_m(src.get("length_m"), length_unit)),
        }

    out = block(m)
    out["geodesic"] = block(m.get("geodesic", {}))
    out["max_deviation_pct"] = m.get("max_deviation_pct")
    out["projection"] = m.get("projection")
    return MeasurementsOut(**out)


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 6)


# --------------------------------------------------------------------------- upload
@router.post(
    "/", response_model=FileOut, status_code=201, summary="Upload and process a geospatial file",
    responses={202: {"description": "Accepted for background processing (`?async=true`)"},
               413: {"description": "File too large"}, 415: {"description": "Unsupported file type"},
               422: {"description": "Invalid / unreadable upload"}},
)
def upload_file(
    request: Request,
    response: Response,
    background: BackgroundTasks,
    db: Db,
    file: Annotated[UploadFile, File(description="`.zip` containing a Shapefile, `.kml`, or `.kmz`")],
    assume_crs: Annotated[str | None, Form(description="CRS to use if the file has none, e.g. EPSG:4326")] = None,
    process_async: Annotated[bool, Query(alias="async", description="Return immediately (202) and process in the background")] = False,
):
    settings = request.app.state.settings
    if assume_crs:
        try:
            CRS.from_user_input(assume_crs)
        except CRSError:
            raise err(422, "invalid_crs", f"Could not interpret assume_crs='{assume_crs}'.")

    file_id = uuid.uuid4().hex
    storage = settings.uploads_dir / file_id
    try:
        result = ingest_upload(file, storage, settings)
    except IngestError as exc:
        remove_storage(storage)
        raise err(exc.status, exc.code, exc.message)
    except Exception:
        remove_storage(storage)
        raise

    rec = FileRecord(
        id=file_id, filename=Path(file.filename or "upload").name, file_type=result.file_type,
        size_bytes=result.size_bytes, status=FileStatus.PENDING, assume_crs=assume_crs,
        warnings=result.warnings, storage_dir=str(storage), datasets=result.datasets,
    )
    db.add(rec)
    db.commit()

    factory = request.app.state.session_factory
    if process_async:
        background.add_task(process_file, factory, settings, file_id)
        response.status_code = 202
    else:
        process_file(factory, settings, file_id)
    db.expire_all()
    return _file_out(db.get(FileRecord, file_id))


# --------------------------------------------------------------------------- list / info / delete
@router.get("/", response_model=FileList, summary="List uploaded files")
def list_files(
    db: Db,
    status: Annotated[str | None, Query(description="Filter by status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    q = select(FileRecord)
    cq = select(func.count()).select_from(FileRecord)
    if status:
        q, cq = q.where(FileRecord.status == status.upper()), cq.where(FileRecord.status == status.upper())
    rows = db.scalars(q.order_by(FileRecord.created_at.desc()).limit(limit).offset(offset)).all()
    return FileList(total=db.scalar(cq) or 0, limit=limit, offset=offset, items=[_file_out(r) for r in rows])


@router.get("/{file_id}/", response_model=FileOut, summary="File information and processing status")
def get_file(file_id: str, db: Db):
    return _file_out(_get_file(db, file_id))


@router.delete("/{file_id}/", status_code=204, summary="Delete a file, its features and stored data")
def delete_file(file_id: str, db: Db):
    rec = _get_file(db, file_id)
    db.execute(delete(FeatureRecord).where(FeatureRecord.file_id == file_id))
    db.delete(rec)
    db.commit()
    remove_storage(rec.storage_dir)
    return Response(status_code=204)


# --------------------------------------------------------------------------- measurements
@router.get(
    "/{file_id}/measurements/", response_model=MeasurementsResponse, response_model_exclude_none=True,
    summary="Per-feature measurements plus file-level totals",
)
def get_measurements(
    file_id: str,
    db: Db,
    area_unit: Annotated[AreaUnit, Query(description="Unit for areas")] = "sqm",
    length_unit: Annotated[LengthUnit, Query(description="Unit for lengths/perimeters")] = "m",
    geometry_type: Annotated[str | None, Query(description="e.g. Polygon, LineString, Point")] = None,
    status: Annotated[str | None, Query(description="MEASURED | NOT_APPLICABLE | UNSUPPORTED | SKIPPED | ERROR")] = None,
    layer: Annotated[str | None, Query(description="Only features from this layer")] = None,
    include_geometry: Annotated[bool, Query(description="Embed GeoJSON geometry (source CRS)")] = False,
    include_properties: Annotated[bool, Query(description="Embed source attributes")] = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    rec = _get_file(db, file_id)
    _require_completed(rec)

    conds = [FeatureRecord.file_id == file_id]
    if geometry_type:
        conds.append(FeatureRecord.geometry_type == geometry_type)
    if status:
        conds.append(FeatureRecord.measurement_status == status.upper())
    if layer:
        conds.append(FeatureRecord.layer == layer)

    # file-level summary over the whole filtered set (cheap: only the needed columns)
    by_status: Counter[str] = Counter()
    agg: dict[str, dict[str, float]] = {}
    totals = {"area": 0.0, "perimeter": 0.0, "length": 0.0}
    for gtype, st, m in db.execute(
        select(FeatureRecord.geometry_type, FeatureRecord.measurement_status, FeatureRecord.measurements).where(*conds)
    ):
        by_status[st] += 1
        a = agg.setdefault(gtype or "None", {"count": 0, "area": 0.0, "perimeter": 0.0, "length": 0.0})
        a["count"] += 1
        if m:
            for key, src in (("area", "area_m2"), ("perimeter", "perimeter_m"), ("length", "length_m")):
                v = m.get(src)
                if v:
                    a[key] += v
                    totals[key] += v
    total = sum(by_status.values())

    summary = Summary(
        feature_count=total,
        by_status=dict(by_status),
        by_geometry_type={
            g: GeometryTypeSummary(
                count=int(v["count"]),
                total_area=_r(area_from_m2(v["area"], area_unit)) if v["area"] else None,
                total_perimeter=_r(length_from_m(v["perimeter"], length_unit)) if v["perimeter"] else None,
                total_length=_r(length_from_m(v["length"], length_unit)) if v["length"] else None,
            )
            for g, v in agg.items()
        },
        total_area=_r(area_from_m2(totals["area"], area_unit)),
        total_perimeter=_r(length_from_m(totals["perimeter"], length_unit)),
        total_length=_r(length_from_m(totals["length"], length_unit)),
    )

    rows = db.scalars(
        select(FeatureRecord).where(*conds).order_by(FeatureRecord.idx).limit(limit).offset(offset)
    ).all()
    items = [
        FeatureMeasurement(
            index=r.idx, fid=r.fid, layer=r.layer, geometry_type=r.geometry_type, crs=r.crs,
            status=r.measurement_status, message=r.message, warnings=r.warnings or [],
            measurements=_convert(r.measurements, area_unit, length_unit),
            properties=r.properties if include_properties else None,
            geometry=r.geometry if include_geometry else None,
        )
        for r in rows
    ]
    return MeasurementsResponse(
        file_id=rec.id, filename=rec.filename, status=rec.status,
        units=Units(area=area_unit, length=length_unit), summary=summary,
        pagination=Pagination(total=total, limit=limit, offset=offset), features=items,
    )


# --------------------------------------------------------------------------- features (GeoJSON)
def _output_crs(output_crs: str | None) -> CRS | None:
    if not output_crs:
        return None
    try:
        dst = CRS.from_user_input(output_crs)
    except CRSError:
        raise err(422, "invalid_crs", f"Could not interpret output_crs='{output_crs}'.")
    return dst


def _feature_json(r: FeatureRecord, area_unit: str, length_unit: str, dst: CRS | None, cache: dict) -> dict:
    geom = r.geometry
    if geom is not None and dst is not None and r.crs:
        key = r.crs
        if key not in cache:
            cache[key] = Transformer.from_crs(CRS.from_user_input(r.crs), dst, always_xy=True)
        t = cache[key]
        g = shape(geom)
        g = shapely.transform(g, lambda c: np.column_stack(t.transform(c[:, 0], c[:, 1])))
        geom = mapping(g)
    m = _convert(r.measurements, area_unit, length_unit)
    return {
        "type": "Feature",
        "id": r.idx,
        "geometry": geom,
        "properties": r.properties or {},
        "measurements": m.model_dump(exclude_none=True) if m else None,
        "meta": {
            "fid": r.fid, "layer": r.layer, "geometry_type": r.geometry_type, "source_crs": r.crs,
            "status": r.measurement_status, "message": r.message, "warnings": r.warnings or [],
        },
    }


@router.get("/{file_id}/features/", summary="Extracted features as a GeoJSON FeatureCollection")
def get_features(
    file_id: str,
    db: Db,
    output_crs: Annotated[str | None, Query(description="Reproject geometries (e.g. EPSG:4326). Default: source CRS")] = None,
    area_unit: AreaUnit = "sqm",
    length_unit: LengthUnit = "m",
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    rec = _get_file(db, file_id)
    _require_completed(rec)
    dst = _output_crs(output_crs)
    rows = db.scalars(
        select(FeatureRecord).where(FeatureRecord.file_id == file_id)
        .order_by(FeatureRecord.idx).limit(limit).offset(offset)
    ).all()
    cache: dict = {}
    body = {
        "type": "FeatureCollection",
        "crs": output_crs or rec.crs or "source",
        "pagination": {"total": rec.feature_count, "limit": limit, "offset": offset},
        "features": [_feature_json(r, area_unit, length_unit, dst, cache) for r in rows],
    }
    return JSONResponse(body, media_type="application/geo+json")


@router.get("/{file_id}/features/{index}/", summary="A single feature by index")
def get_feature(
    file_id: str, index: int, db: Db,
    output_crs: str | None = None, area_unit: AreaUnit = "sqm", length_unit: LengthUnit = "m",
):
    rec = _get_file(db, file_id)
    _require_completed(rec)
    dst = _output_crs(output_crs)
    r = db.scalar(select(FeatureRecord).where(FeatureRecord.file_id == file_id, FeatureRecord.idx == index))
    if r is None:
        raise err(404, "feature_not_found", f"File has no feature with index {index}.")
    return JSONResponse(_feature_json(r, area_unit, length_unit, dst, {}), media_type="application/geo+json")
