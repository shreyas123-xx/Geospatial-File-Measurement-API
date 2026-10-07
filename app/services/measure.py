"""Measurement engine.

Strategy
--------
Raw lon/lat degrees are never measured directly.  For each feature:

1. The geometry is brought to WGS84 lon/lat (``always_xy`` so axis order is never ambiguous).
   This also makes us robust to projected inputs whose projection distorts area
   (e.g. Web Mercator, where a "1 km²" square at 60°N is really ~0.25 km²).
2. A *local* projection is built, centred on the feature's centroid:
   * **Area / perimeter of polygons** -> Lambert Azimuthal Equal-Area (``laea``).
     Equal-area means polygon area is preserved exactly, anywhere on Earth.
   * **Length of lines** -> Azimuthal Equidistant (``aeqd``), where distances measured
     from the centre are exact and distortion stays tiny for realistic feature sizes.
3. As an independent cross-check every result is also computed *geodesically* on the
   WGS84 ellipsoid (``pyproj.Geod``) and the deviation is reported.

Collections (e.g. KML ``MultiGeometry``) are decomposed: polygons contribute area/perimeter,
lines contribute length, points contribute nothing.  Invalid polygons (self-intersections)
are repaired with ``make_valid`` before measuring and a warning is attached.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Geod, Transformer
from shapely.geometry import LineString
from shapely.geometry.polygon import orient
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

from app.models import FeatureStatus

WGS84 = CRS.from_epsg(4326)
GEOD = Geod(ellps="WGS84")

POLYGONAL = {"Polygon"}
LINEAR = {"LineString", "LinearRing"}
POINTAL = {"Point"}


@dataclass
class MeasureResult:
    status: str
    measurements: dict | None = None
    message: str | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CRS helpers
# ---------------------------------------------------------------------------
def crs_label(crs: CRS | None) -> str | None:
    """Human friendly CRS id: ``EPSG:4326`` when identifiable, otherwise the CRS name."""
    if crs is None:
        return None
    auth = crs.to_authority(min_confidence=70)
    if auth:
        return f"{auth[0]}:{auth[1]}"
    return crs.name or "UNKNOWN"


@lru_cache(maxsize=64)
def _to_wgs84(src_wkt: str) -> Transformer:
    return Transformer.from_crs(CRS.from_wkt(src_wkt), WGS84, always_xy=True)


@lru_cache(maxsize=4096)
def _local_projection(kind: str, lat: float, lon: float) -> tuple[Transformer, str]:
    proj = "laea" if kind == "area" else "aeqd"
    proj4 = f"+proj={proj} +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    return Transformer.from_crs(WGS84, CRS.from_proj4(proj4), always_xy=True), proj4


def _apply(transformer: Transformer, geom: BaseGeometry) -> BaseGeometry:
    def fn(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack((x, y))

    out = shapely.transform(geom, fn)
    xy = shapely.get_coordinates(out)
    if xy.size and not np.isfinite(xy).all():
        raise ValueError("projection produced non-finite coordinates (feature too large for a local projection?)")
    return out


def to_wgs84(geom: BaseGeometry, src_crs: CRS) -> BaseGeometry:
    return _apply(_to_wgs84(src_crs.to_wkt()), geom)


def _projection_center(geom_wgs: BaseGeometry) -> tuple[float, float]:
    """Centroid in lon/lat, rounded to a 1° grid so projection objects can be cached and reused.

    Building a PROJ transformer costs ~0.5 ms - more than the measurement itself - so reuse matters.
    The rounding is harmless: LAEA preserves area exactly for *any* centre, and for AEQD a ≤0.5° offset
    changes distances of realistically sized features by ~1e-5 (the geodesic cross-check reports it).

    Geometries that straddle the antimeridian (bounds span > 180°) are shifted into 0..360
    before taking the centroid so the centre isn't placed on the opposite side of the globe.
    """
    minx, miny, maxx, maxy = geom_wgs.bounds
    g = geom_wgs
    if maxx - minx > 180:
        g = shapely.transform(g, lambda c: np.column_stack((c[:, 0] % 360, c[:, 1])))
    c = g.centroid
    if c.is_empty or not np.isfinite([c.x, c.y]).all():
        lon, lat = (minx + maxx) / 2, (miny + maxy) / 2
    else:
        lon, lat = c.x, c.y
    lon = ((lon + 180) % 360) - 180
    return float(round(lat)), float(round(lon))


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _flatten(geom: BaseGeometry) -> list[BaseGeometry]:
    """Recursively explode Multi*/GeometryCollection into simple parts."""
    if geom.geom_type in ("MultiPoint", "MultiLineString", "MultiPolygon", "GeometryCollection"):
        parts: list[BaseGeometry] = []
        for g in geom.geoms:
            parts.extend(_flatten(g))
        return parts
    return [geom]


def _geodesic_perimeter(poly: BaseGeometry) -> float:
    rings = [poly.exterior, *poly.interiors]
    return float(sum(GEOD.geometry_length(LineString(r.coords)) for r in rings))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def measure_geometry(geom: BaseGeometry | None, src_crs: CRS) -> MeasureResult:
    if geom is None:
        return MeasureResult(FeatureStatus.SKIPPED, message="Feature has no geometry.")
    if geom.is_empty:
        return MeasureResult(FeatureStatus.SKIPPED, message="Geometry is empty.")

    warnings: list[str] = []
    try:
        if not geom.is_valid:
            reason = explain_validity(geom)
            geom = shapely.make_valid(geom)
            warnings.append(f"Invalid geometry ({reason}); repaired before measuring.")

        parts = _flatten(geom)
        polys = [p for p in parts if p.geom_type in POLYGONAL and not p.is_empty]
        lines = [p for p in parts if p.geom_type in LINEAR and not p.is_empty]
        points = [p for p in parts if p.geom_type in POINTAL]
        known = len(polys) + len(lines) + len(points)
        if known == 0:
            return MeasureResult(
                FeatureStatus.UNSUPPORTED,
                message=f"Geometry type '{geom.geom_type}' is not supported for measurement.",
                warnings=warnings,
            )
        if not polys and not lines:
            return MeasureResult(
                FeatureStatus.NOT_APPLICABLE,
                message="Points have no area or length.",
                warnings=warnings,
            )

        wgs = to_wgs84(geom, src_crs)
        wgs_parts = _flatten(wgs)
        wgs_polys = [p for p in wgs_parts if p.geom_type in POLYGONAL and not p.is_empty]
        wgs_lines = [p for p in wgs_parts if p.geom_type in LINEAR and not p.is_empty]
        lat, lon = _projection_center(wgs)

        out: dict = {"projection": {}}
        geodesic: dict = {}
        deviations: list[float] = []

        def _dev(proj_val: float, geo_val: float) -> None:
            if geo_val > 0:
                deviations.append(abs(proj_val - geo_val) / geo_val * 100)

        if wgs_polys:
            t_area, proj4 = _local_projection("area", lat, lon)
            projected = [_apply(t_area, p) for p in wgs_polys]
            out["area_m2"] = float(sum(p.area for p in projected))
            out["perimeter_m"] = float(sum(p.length for p in projected))
            out["projection"]["area"] = proj4
            # pyproj's ring area is signed by winding; normalise (shell CCW, holes CW) so holes
            # always subtract regardless of how the source file wound its rings.
            g_area = float(sum(abs(GEOD.geometry_area_perimeter(orient(p, 1.0))[0]) for p in wgs_polys))
            g_perim = float(sum(_geodesic_perimeter(p) for p in wgs_polys))
            geodesic["area_m2"], geodesic["perimeter_m"] = g_area, g_perim
            _dev(out["area_m2"], g_area)
            _dev(out["perimeter_m"], g_perim)

        if wgs_lines:
            t_len, proj4 = _local_projection("length", lat, lon)
            projected = [_apply(t_len, ln) for ln in wgs_lines]
            out["length_m"] = float(sum(ln.length for ln in projected))
            out["projection"]["length"] = proj4
            g_len = float(sum(GEOD.geometry_length(ln) for ln in wgs_lines))
            geodesic["length_m"] = g_len
            _dev(out["length_m"], g_len)

        out["geodesic"] = geodesic
        out["max_deviation_pct"] = round(max(deviations), 6) if deviations else 0.0

        if len(parts) > known:
            warnings.append("Some parts of the geometry are not measurable and were ignored.")
        return MeasureResult(FeatureStatus.MEASURED, measurements=out, warnings=warnings)
    except Exception as exc:  # one bad feature must never fail the whole file
        return MeasureResult(
            FeatureStatus.ERROR, message=f"Measurement failed: {exc}", warnings=warnings
        )
