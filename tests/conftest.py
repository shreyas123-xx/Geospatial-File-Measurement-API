from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import pyogrio.raw as ogr_raw
import pytest
import shapely
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as c:
        c.settings = settings
        yield c


def make_shapefile_zip(tmp_path: Path, geoms, crs: str | None, geometry_type: str, props=None,
                       name="data", drop_prj=False) -> bytes:
    """Write a shapefile with GDAL and return it zipped, as upload bytes."""
    folder = tmp_path / f"shp_{name}"
    folder.mkdir(exist_ok=True)
    arr = np.array(geoms, dtype=object)
    names = np.array([f"f{i}" for i in range(len(geoms))], dtype=object)
    ogr_raw.write(
        str(folder / f"{name}.shp"), geometry=shapely.to_wkb(arr), field_data=[names],
        fields=["label"], crs=crs, geometry_type=geometry_type, driver="ESRI Shapefile",
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in folder.iterdir():
            if drop_prj and f.suffix == ".prj":
                continue
            zf.write(f, f.name)
    return buf.getvalue()


KML_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Folder><name>Plots</name>
<Placemark><name>square</name><description>1 deg square at equator</description>
<Polygon><outerBoundaryIs><LinearRing><coordinates>0,0,0 1,0,0 1,1,0 0,1,0 0,0,0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>road</name><LineString><coordinates>0,0 1,0</coordinates></LineString></Placemark>
<Placemark><name>well</name><Point><coordinates>0.5,0.5,10</coordinates></Point></Placemark>
<Placemark><name>mixed</name><MultiGeometry><Point><coordinates>1,1</coordinates></Point>
<LineString><coordinates>1,1 2,1</coordinates></LineString></MultiGeometry></Placemark>
</Folder>
<Folder><name>Empty</name></Folder>
</Document></kml>"""


@pytest.fixture()
def kml_bytes() -> bytes:
    return KML_SAMPLE.encode()
