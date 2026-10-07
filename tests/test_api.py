"""End-to-end API tests."""
import io
import zipfile

import pytest
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon, box

from tests.conftest import make_shapefile_zip

KML = ("survey.kml", "application/vnd.google-earth.kml+xml")


def upload(client, name, content, ctype="application/octet-stream", **params):
    data = params.pop("data", None)
    return client.post("/api/files/", files={"file": (name, content, ctype)}, data=data, params=params)


# ------------------------------------------------------------------ KML happy path
def test_kml_upload_info_and_measurements(client, kml_bytes):
    r = upload(client, "survey.kml", kml_bytes)
    assert r.status_code == 201, r.text
    f = r.json()
    assert f["status"] == "COMPLETED"
    assert f["feature_count"] == 4
    assert f["crs"] == "EPSG:4326"
    assert f["filename"] == "survey.kml"
    assert [l["name"] for l in f["layers"]] == ["Plots"]  # empty folder not listed
    assert f["geometry_type_counts"] == {"Polygon": 1, "LineString": 1, "Point": 1, "GeometryCollection": 1}

    info = client.get(f"/api/files/{f['id']}/").json()
    assert info == f

    m = client.get(f"/api/files/{f['id']}/measurements/").json()
    assert m["summary"]["feature_count"] == 4
    by_name = {x["index"]: x for x in m["features"]}
    poly = by_name[0]
    assert poly["geometry_type"] == "Polygon" and poly["crs"] == "EPSG:4326"
    assert poly["measurements"]["area"] == pytest.approx(12_308_778_361, rel=1e-3)
    assert by_name[1]["measurements"]["length"] == pytest.approx(111_319.49, rel=1e-3)
    assert by_name[2]["status"] == "NOT_APPLICABLE"
    assert by_name[3]["status"] == "MEASURED"  # collection decomposed
    assert m["summary"]["by_status"] == {"MEASURED": 3, "NOT_APPLICABLE": 1}
    assert m["summary"]["total_area"] > 1e10


def test_units_conversion(client, kml_bytes):
    fid = upload(client, "s.kml", kml_bytes).json()["id"]
    sqm = client.get(f"/api/files/{fid}/measurements/", params={"geometry_type": "Polygon"}).json()
    km = client.get(f"/api/files/{fid}/measurements/",
                    params={"geometry_type": "Polygon", "area_unit": "sqkm", "length_unit": "km"}).json()
    assert km["units"] == {"area": "sqkm", "length": "km"}
    assert km["features"][0]["measurements"]["area"] == pytest.approx(
        sqm["features"][0]["measurements"]["area"] / 1e6, rel=1e-6)
    assert client.get(f"/api/files/{fid}/measurements/", params={"area_unit": "bogus"}).status_code == 422


def test_filter_pagination_and_include_flags(client, kml_bytes):
    fid = upload(client, "s.kml", kml_bytes).json()["id"]
    page = client.get(f"/api/files/{fid}/measurements/", params={"limit": 2, "offset": 1}).json()
    assert [f["index"] for f in page["features"]] == [1, 2]
    assert page["pagination"] == {"total": 4, "limit": 2, "offset": 1}
    assert page["summary"]["feature_count"] == 4  # summary covers the whole filtered set
    pts = client.get(f"/api/files/{fid}/measurements/", params={"geometry_type": "Point"}).json()
    assert pts["pagination"]["total"] == 1
    full = client.get(f"/api/files/{fid}/measurements/",
                      params={"include_geometry": True, "include_properties": True, "limit": 1}).json()
    assert full["features"][0]["geometry"]["type"] == "Polygon"
    assert full["features"][0]["properties"]["Name"] == "square"
    assert "geometry" not in page["features"][0]


def test_features_geojson_and_reprojection(client, kml_bytes):
    fid = upload(client, "s.kml", kml_bytes).json()["id"]
    r = client.get(f"/api/files/{fid}/features/")
    assert r.headers["content-type"].startswith("application/geo+json")
    body = r.json()
    assert body["type"] == "FeatureCollection" and len(body["features"]) == 4
    assert body["features"][0]["measurements"]["area"] > 0
    merc = client.get(f"/api/files/{fid}/features/", params={"output_crs": "EPSG:3857", "limit": 1}).json()
    x = merc["features"][0]["geometry"]["coordinates"][0][1][0]
    assert x == pytest.approx(111_319.49, rel=1e-4)  # 1° lon in Web Mercator metres
    one = client.get(f"/api/files/{fid}/features/2/").json()
    assert one["geometry"]["type"] == "Point"
    assert client.get(f"/api/files/{fid}/features/99/").status_code == 404
    assert client.get(f"/api/files/{fid}/features/", params={"output_crs": "nope"}).status_code == 422


# ------------------------------------------------------------------ shapefile
def test_shapefile_zip_wgs84(client, tmp_path):
    z = make_shapefile_zip(tmp_path, [box(77, 12, 77.01, 12.01), box(77.1, 12.1, 77.12, 12.12)],
                           "EPSG:4326", "Polygon")
    f = upload(client, "plots.zip", z).json()
    assert f["status"] == "COMPLETED" and f["file_type"] == "shapefile" and f["feature_count"] == 2
    m = client.get(f"/api/files/{f['id']}/measurements/", params={"include_properties": True}).json()
    assert m["features"][0]["properties"]["label"] == "f0"
    # 0.01° x 0.01° at 12°N ≈ 1.087 km x 1.113 km ≈ 1.21 km²
    assert m["features"][0]["measurements"]["area"] == pytest.approx(1_210_000, rel=0.02)


def test_shapefile_web_mercator_is_not_measured_in_native_units(client, tmp_path):
    x, y = Transformer.from_crs(4326, 3857, always_xy=True).transform(10, 60)
    z = make_shapefile_zip(tmp_path, [box(x, y, x + 1000, y + 1000)], "EPSG:3857", "Polygon")
    f = upload(client, "merc.zip", z).json()
    assert f["crs"] == "EPSG:3857"
    area = client.get(f"/api/files/{f['id']}/measurements/").json()["features"][0]["measurements"]["area"]
    assert area == pytest.approx(250_805, rel=2e-3)  # NOT 1,000,000


def test_shapefile_lines_and_points_mixed_file_types(client, tmp_path):
    z = make_shapefile_zip(tmp_path, [LineString([(77, 12), (77.1, 12)])], "EPSG:4326", "LineString")
    f = upload(client, "roads.zip", z).json()
    assert client.get(f"/api/files/{f['id']}/measurements/").json()["features"][0]["measurements"]["length"] \
        == pytest.approx(10_880, rel=0.01)
    z = make_shapefile_zip(tmp_path, [Point(77, 12)], "EPSG:4326", "Point", name="pts")
    f = upload(client, "pts.zip", z).json()
    m = client.get(f"/api/files/{f['id']}/measurements/").json()
    assert m["features"][0]["status"] == "NOT_APPLICABLE"


def test_missing_prj_fails_cleanly_then_assume_crs_works(client, tmp_path):
    z = make_shapefile_zip(tmp_path, [box(77, 12, 77.01, 12.01)], "EPSG:4326", "Polygon", drop_prj=True)
    f = upload(client, "noprj.zip", z).json()
    assert f["status"] == "FAILED" and "assume_crs" in f["error"]
    assert any(".prj" in w for w in f["warnings"])
    r = client.get(f"/api/files/{f['id']}/measurements/")
    assert r.status_code == 409 and r.json()["error"]["code"] == "processing_failed"

    ok = upload(client, "noprj.zip", z, data={"assume_crs": "EPSG:4326"}).json()
    assert ok["status"] == "COMPLETED" and ok["crs_assumed"] is True
    assert any("assumed" in w for w in ok["warnings"])


def test_null_geometry_handled_gracefully(client, tmp_path):
    z = make_shapefile_zip(tmp_path, [box(0, 0, 1, 1), None], "EPSG:4326", "Polygon")
    f = upload(client, "nulls.zip", z).json()
    assert f["status"] == "COMPLETED" and f["feature_count"] == 2
    m = client.get(f"/api/files/{f['id']}/measurements/").json()
    assert m["summary"]["by_status"] == {"MEASURED": 1, "SKIPPED": 1}
    assert any("skipped" in w for w in f["warnings"])


# ------------------------------------------------------------------ KMZ
def test_kmz_supported(client, kml_bytes):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("doc.kml", kml_bytes)
    f = upload(client, "survey.kmz", buf.getvalue()).json()
    assert f["status"] == "COMPLETED" and f["file_type"] == "kmz" and f["feature_count"] == 4


# ------------------------------------------------------------------ validation / security
def test_unsupported_extension(client):
    r = upload(client, "data.geojson", b"{}")
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_file_type"


def test_empty_file(client):
    assert upload(client, "x.kml", b"").json()["error"]["code"] == "empty_file"


def test_fake_kml(client):
    assert upload(client, "x.kml", b"hello world").json()["error"]["code"] == "invalid_kml"


def test_not_a_zip(client):
    assert upload(client, "x.zip", b"not a zip").json()["error"]["code"] == "invalid_archive"


def test_zip_without_shapefile(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hi")
    assert upload(client, "x.zip", buf.getvalue()).json()["error"]["code"] == "no_shapefile_found"


def test_zip_slip_rejected_and_nothing_left_behind(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.shp", "x")
    r = upload(client, "evil.zip", buf.getvalue())
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsafe_archive"
    assert list(client.settings.uploads_dir.iterdir()) == []


def test_upload_size_limit(client):
    client.settings.max_upload_bytes = 100
    r = upload(client, "big.kml", b"<kml>" + b"x" * 500)
    assert r.status_code == 413


def test_corrupt_shapefile_marks_failed_not_500(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bad.shp", b"garbage" * 20)
        zf.writestr("bad.dbf", b"garbage")
    r = upload(client, "bad.zip", buf.getvalue())
    assert r.status_code == 201 and r.json()["status"] == "FAILED" and r.json()["error"]


def test_invalid_assume_crs(client, kml_bytes):
    assert upload(client, "s.kml", kml_bytes, data={"assume_crs": "banana"}).status_code == 422


# ------------------------------------------------------------------ lifecycle
def test_async_processing(client, kml_bytes):
    r = upload(client, "s.kml", kml_bytes, **{"async": "true"})
    assert r.status_code == 202
    fid = r.json()["id"]
    # TestClient runs background tasks before returning, so it is already done here
    assert client.get(f"/api/files/{fid}/").json()["status"] == "COMPLETED"


def test_list_and_delete(client, kml_bytes):
    a = upload(client, "a.kml", kml_bytes).json()["id"]
    b = upload(client, "b.kml", kml_bytes).json()["id"]
    lst = client.get("/api/files/").json()
    assert lst["total"] == 2 and {i["id"] for i in lst["items"]} == {a, b}
    assert client.delete(f"/api/files/{a}/").status_code == 204
    assert client.get(f"/api/files/{a}/").status_code == 404
    assert client.get("/api/files/", params={"status": "completed"}).json()["total"] == 1
    assert not (client.settings.uploads_dir / a).exists()


def test_unknown_id_and_health(client):
    r = client.get("/api/files/nope/")
    assert r.status_code == 404 and r.json()["error"]["code"] == "file_not_found"
    assert client.get("/api/files/nope/measurements/").status_code == 404
    assert client.get("/health").json() == {"status": "ok"}
