"""Reads Shapefile / KML datasets with GDAL (via pyogrio) into plain Python objects."""
from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pyogrio
import pyogrio.raw as ogr_raw
import shapely
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry.base import BaseGeometry

# Many shapefiles in the wild lack a .shx; let GDAL rebuild it instead of failing.
pyogrio.set_gdal_config_options({"SHAPE_RESTORE_SHX": "YES"})

# KML structural columns GDAL always exposes; they are style noise, not user attributes.
_KML_NOISE = {"tessellate", "extrude", "visibility", "drawOrder"}


class ReadError(Exception):
    """The dataset could not be read (corrupt file, unknown CRS, ...)."""


@dataclass
class ParsedFeature:
    fid: int | None
    layer: str
    geometry: BaseGeometry | None
    properties: dict
    crs: CRS
    read_error: str | None = None


@dataclass
class LayerInfo:
    name: str
    crs: CRS
    crs_assumed: bool
    dataset: str


def _json_safe(value):
    """Convert numpy / datetime / NaN values into JSON-serialisable Python values."""
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, np.datetime64):
        return None if np.isnat(value) else str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _resolve_crs(raw_crs: str | None, assume: CRS | None, where: str) -> tuple[CRS, bool]:
    if raw_crs:
        try:
            return CRS.from_user_input(raw_crs), False
        except CRSError as exc:
            if assume is None:
                raise ReadError(f"{where}: could not interpret CRS ({exc}).") from exc
    if assume is not None:
        return assume, True
    raise ReadError(
        f"{where}: no coordinate reference system found (shapefile without .prj?). "
        "Re-upload with the `assume_crs` form field, e.g. assume_crs=EPSG:4326."
    )


def list_layers(path: Path) -> list[str]:
    try:
        return [str(row[0]) for row in pyogrio.list_layers(str(path))]
    except Exception as exc:
        raise ReadError(f"{path.name}: unreadable geospatial file ({exc}).") from exc


def read_layer(
    path: Path, layer: str, file_type: str, assume: CRS | None, chunk_size: int
) -> tuple[LayerInfo, Iterator[ParsedFeature]]:
    """Open one layer. Returns its info plus a lazy iterator over its features (chunked)."""
    try:
        meta, _, _, _ = ogr_raw.read(
            str(path), layer=layer, read_geometry=False, max_features=1, return_fids=True
        )
    except Exception as exc:
        raise ReadError(f"{path.name} [{layer}]: unreadable layer ({exc}).") from exc

    crs, assumed = _resolve_crs(meta.get("crs"), assume, f"{path.name} [{layer}]")
    info = LayerInfo(name=layer, crs=crs, crs_assumed=assumed, dataset=path.name)

    def _iter() -> Iterator[ParsedFeature]:
        offset = 0
        while True:
            try:
                m, fids, geoms, field_data = ogr_raw.read(
                    str(path),
                    layer=layer,
                    force_2d=True,
                    return_fids=True,
                    datetime_as_string=True,
                    skip_features=offset,
                    max_features=chunk_size,
                )
            except Exception as exc:
                raise ReadError(f"{path.name} [{layer}]: failed while reading features ({exc}).") from exc
            n = len(fids)
            if n == 0:
                return
            names = [str(f) for f in m["fields"]]
            for i in range(n):
                props = {}
                for name, col in zip(names, field_data):
                    if file_type != "shapefile" and name in _KML_NOISE:
                        continue
                    v = _json_safe(col[i])
                    if v is not None:
                        props[name] = v
                wkb = geoms[i]
                geom, err = None, None
                if wkb is not None:
                    try:
                        geom = shapely.from_wkb(wkb)
                    except Exception as exc:  # e.g. curve geometries
                        err = f"Could not decode geometry: {exc}"
                yield ParsedFeature(
                    fid=int(fids[i]), layer=layer, geometry=geom, properties=props, crs=crs, read_error=err
                )
            offset += n
            if n < chunk_size:
                return

    return info, _iter()
