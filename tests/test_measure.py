"""Unit tests for the measurement engine against analytically known values."""
import pytest
from pyproj import CRS, Transformer
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon, box

from app.models import FeatureStatus
from app.services.measure import measure_geometry

WGS84 = CRS.from_epsg(4326)


def test_one_degree_square_at_equator():
    r = measure_geometry(box(0, 0, 1, 1), WGS84)
    assert r.status == FeatureStatus.MEASURED
    # true ellipsoidal area is 12,308.78 km²
    assert r.measurements["area_m2"] == pytest.approx(12_308_778_361, rel=1e-3)
    assert r.measurements["max_deviation_pct"] < 0.05


def test_degrees_are_not_used_directly():
    # a naive shapely.area would return 1.0 (square degrees)
    assert measure_geometry(box(0, 0, 1, 1), WGS84).measurements["area_m2"] > 1e9


def test_area_shrinks_with_latitude():
    eq = measure_geometry(box(0, 0, 1, 1), WGS84).measurements["area_m2"]
    high = measure_geometry(box(0, 60, 1, 61), WGS84).measurements["area_m2"]
    assert high < eq * 0.55  # cos(60.5°) ≈ 0.49


def test_projected_input_with_distorting_projection():
    """1 km x 1 km in Web Mercator at 60°N is really ~0.25 km²."""
    x, y = Transformer.from_crs(4326, 3857, always_xy=True).transform(10, 60)
    r = measure_geometry(box(x, y, x + 1000, y + 1000), CRS.from_epsg(3857))
    assert r.measurements["area_m2"] == pytest.approx(250_805, rel=1e-3)


def test_utm_input():
    r = measure_geometry(box(500_000, 1_400_000, 501_000, 1_401_000), CRS.from_epsg(32643))
    assert r.measurements["area_m2"] == pytest.approx(1_000_000, rel=5e-3)


def test_polygon_with_hole_subtracts_hole():
    outer = [(0, 0), (2, 0), (2, 2), (0, 2)]
    hole = [(0.5, 0.5), (0.5, 1.5), (1.5, 1.5), (1.5, 0.5)]
    full = measure_geometry(Polygon(outer), WGS84).measurements
    holed = measure_geometry(Polygon(outer, [hole]), WGS84).measurements
    assert holed["area_m2"] == pytest.approx(36_924_174_572, rel=1e-3)
    assert holed["perimeter_m"] > full["perimeter_m"]  # hole boundary counts


def test_linestring_length():
    r = measure_geometry(LineString([(0, 0), (1, 0)]), WGS84)
    assert r.measurements["length_m"] == pytest.approx(111_319.49, rel=1e-4)
    assert "area_m2" not in r.measurements


def test_multipolygon_sums_parts():
    one = measure_geometry(box(0, 0, 1, 1), WGS84).measurements["area_m2"]
    two = measure_geometry(MultiPolygon([box(0, 0, 1, 1), box(5, 5, 6, 6)]), WGS84)
    assert two.measurements["area_m2"] > one  # roughly one + a slightly smaller square


def test_antimeridian_split_polygon():
    r = measure_geometry(MultiPolygon([box(179, 0, 180, 1), box(-180, 0, -179, 1)]), WGS84)
    assert r.status == FeatureStatus.MEASURED
    assert r.measurements["area_m2"] == pytest.approx(2 * 12_308_778_361, rel=2e-3)


def test_polar_polygon():
    r = measure_geometry(box(0, 88, 10, 89), WGS84)
    assert r.measurements["max_deviation_pct"] < 0.05


def test_invalid_bowtie_is_repaired_with_warning():
    r = measure_geometry(Polygon([(0, 0), (1, 1), (1, 0), (0, 1)]), WGS84)
    assert r.status == FeatureStatus.MEASURED
    assert r.measurements["area_m2"] == pytest.approx(6_154_387_643, rel=1e-3)
    assert any("repaired" in w for w in r.warnings)


def test_point_not_applicable():
    r = measure_geometry(Point(1, 1), WGS84)
    assert r.status == FeatureStatus.NOT_APPLICABLE and r.measurements is None


def test_null_and_empty_skipped():
    assert measure_geometry(None, WGS84).status == FeatureStatus.SKIPPED
    assert measure_geometry(Polygon(), WGS84).status == FeatureStatus.SKIPPED


def test_geometry_collection_decomposed():
    gc = GeometryCollection([Point(1, 1), LineString([(1, 1), (2, 1)]), box(0, 0, 1, 1)])
    m = measure_geometry(gc, WGS84).measurements
    assert m["area_m2"] > 0 and m["length_m"] > 0


def test_hole_winding_does_not_affect_geodesic_check():
    """Regression: KML/GDAL can return a hole wound the same way as its shell."""
    shell = [(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]           # CCW
    hole_same = [(0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5), (0.5, 0.5)]  # CCW (same as shell)
    hole_opp = hole_same[::-1]                                  # CW
    a = measure_geometry(Polygon(shell, [hole_same]), WGS84).measurements
    b = measure_geometry(Polygon(shell, [hole_opp]), WGS84).measurements
    assert a["geodesic"]["area_m2"] == pytest.approx(b["geodesic"]["area_m2"], rel=1e-9)
    assert a["max_deviation_pct"] < 0.05 and b["max_deviation_pct"] < 0.05
