"""File-processing pipeline: read features -> measure -> persist."""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from pyproj import CRS
from shapely.geometry import mapping
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import FeatureRecord, FeatureStatus, FileRecord, FileStatus, utcnow
from app.services import reader
from app.services.measure import crs_label, measure_geometry

log = logging.getLogger("geo.processing")


def process_file(session_factory: sessionmaker[Session], settings: Settings, file_id: str) -> None:
    """Run the full pipeline for one file. Never raises: failures are recorded on the file row."""
    with session_factory() as session:
        rec = session.get(FileRecord, file_id)
        if rec is None:
            return
        rec.status = FileStatus.PROCESSING
        session.commit()
        try:
            _ingest(session, settings, rec)
            rec.status = FileStatus.COMPLETED
            rec.error = None
        except reader.ReadError as exc:
            session.rollback()
            _mark_failed(session, rec, str(exc))
        except Exception as exc:  # noqa: BLE001 - last line of defence, logged below
            session.rollback()
            log.exception("Unexpected failure processing file %s", file_id)
            _mark_failed(session, rec, f"Unexpected error while processing file ({type(exc).__name__}).")
        rec.completed_at = utcnow()
        session.commit()


def _mark_failed(session: Session, rec: FileRecord, message: str) -> None:
    session.execute(delete(FeatureRecord).where(FeatureRecord.file_id == rec.id))
    rec.status = FileStatus.FAILED
    rec.error = message
    rec.feature_count = 0
    rec.layers, rec.geometry_type_counts, rec.crs = [], {}, None


def _ingest(session: Session, settings: Settings, rec: FileRecord) -> None:
    assume = CRS.from_user_input(rec.assume_crs) if rec.assume_crs else None
    base = Path(rec.storage_dir)
    layers_out: list[dict] = []
    type_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    crs_labels: set[str] = set()
    any_assumed = False
    index = 0
    batch: list[FeatureRecord] = []

    for rel in rec.datasets:
        path = base / rel
        for layer in reader.list_layers(path):
            info, features = reader.read_layer(path, layer, rec.file_type, assume, settings.read_chunk_size)
            label = crs_label(info.crs)
            layer_count, layer_types = 0, Counter()
            for feat in features:
                geom = feat.geometry
                if feat.read_error:
                    result = _unsupported(feat.read_error)
                else:
                    result = measure_geometry(geom, info.crs)
                gtype = geom.geom_type if geom is not None else None
                batch.append(
                    FeatureRecord(
                        file_id=rec.id,
                        idx=index,
                        fid=feat.fid,
                        layer=layer,
                        geometry_type=gtype,
                        crs=label,
                        geometry=mapping(geom) if geom is not None and not geom.is_empty else None,
                        properties=feat.properties,
                        measurement_status=result.status,
                        measurements=result.measurements,
                        message=result.message,
                        warnings=result.warnings,
                    )
                )
                index += 1
                layer_count += 1
                type_counts[gtype or "None"] += 1
                layer_types[gtype or "None"] += 1
                status_counts[result.status] += 1
                if len(batch) >= settings.insert_batch_size:
                    session.add_all(batch)
                    session.flush()
                    batch.clear()
            if layer_count == 0:
                continue  # empty folders are common in KML; don't list them
            crs_labels.add(label or "UNKNOWN")
            any_assumed |= info.crs_assumed
            layers_out.append(
                {"name": layer, "dataset": info.dataset, "feature_count": layer_count,
                 "crs": label, "geometry_types": dict(layer_types)}
            )

    if batch:
        session.add_all(batch)
        session.flush()

    rec.feature_count = index
    rec.layers = layers_out
    rec.geometry_type_counts = dict(type_counts)
    rec.crs_assumed = any_assumed
    rec.crs = (next(iter(crs_labels)) if len(crs_labels) == 1 else "MIXED") if crs_labels else None

    warnings = list(rec.warnings or [])
    if index == 0:
        warnings.append("File was read successfully but contains no features.")
    for status, text in (
        (FeatureStatus.SKIPPED, "feature(s) skipped (null or empty geometry)"),
        (FeatureStatus.UNSUPPORTED, "feature(s) have unsupported geometry"),
        (FeatureStatus.ERROR, "feature(s) failed to measure"),
    ):
        if status_counts[status]:
            warnings.append(f"{status_counts[status]} {text}.")
    if any_assumed:
        warnings.append(f"CRS was not present in the file; assumed {rec.assume_crs}.")
    rec.warnings = warnings
    session.commit()


def _unsupported(message: str):
    from app.services.measure import MeasureResult

    return MeasureResult(FeatureStatus.UNSUPPORTED, message=message)
