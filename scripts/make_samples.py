"""Generate small sample inputs in ./samples (run: python scripts/make_samples.py)."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import pyogrio.raw as ogr_raw
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point, box

OUT = Path(__file__).resolve().parent.parent / "samples"
OUT.mkdir(exist_ok=True)

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Bengaluru survey</name>
<Folder><name>Plots</name>
<Placemark><name>Plot A</name><description>Residential plot</description>
<ExtendedData><Data name="owner"><value>Rao</value></Data></ExtendedData>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
77.5900,12.9700,0 77.5920,12.9700,0 77.5920,12.9720,0 77.5900,12.9720,0 77.5900,12.9700,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>Plot B (with courtyard)</name>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
77.5950,12.9700,0 77.5990,12.9700,0 77.5990,12.9740,0 77.5950,12.9740,0 77.5950,12.9700,0
</coordinates></LinearRing></outerBoundaryIs>
<innerBoundaryIs><LinearRing><coordinates>
77.5965,12.9715,0 77.5975,12.9715,0 77.5975,12.9725,0 77.5965,12.9725,0 77.5965,12.9715,0
</coordinates></LinearRing></innerBoundaryIs></Polygon></Placemark>
</Folder>
<Folder><name>Infrastructure</name>
<Placemark><name>Access road</name><LineString><coordinates>77.5880,12.9690,0 77.5930,12.9710,0 77.6000,12.9750,0</coordinates></LineString></Placemark>
<Placemark><name>Borewell</name><Point><coordinates>77.5910,12.9710,0</coordinates></Point></Placemark>
<Placemark><name>Site (mixed)</name><MultiGeometry><Point><coordinates>77.60,12.97</coordinates></Point>
<LineString><coordinates>77.60,12.97 77.601,12.971</coordinates></LineString></MultiGeometry></Placemark>
</Folder></Document></kml>
"""
(OUT / "bengaluru_survey.kml").write_text(KML)


def shp_zip(path: Path, geoms, crs: str, gtype: str) -> None:
    tmp = path.with_suffix(".d")
    tmp.mkdir(exist_ok=True)
    arr = np.array(geoms, dtype=object)
    ogr_raw.write(
        str(tmp / "layer.shp"), geometry=shapely.to_wkb(arr),
        field_data=[np.array([f"feature-{i}" for i in range(len(geoms))], dtype=object)],
        fields=["name"], crs=crs, geometry_type=gtype, driver="ESRI Shapefile",
    )
    with zipfile.ZipFile(path, "w") as zf:
        for f in sorted(tmp.iterdir()):
            zf.write(f, f.name)
            f.unlink()
    tmp.rmdir()


# UTM 43N (Bengaluru): two 1 ha squares + a 500 m line
shp_zip(OUT / "utm43n_plots.zip",
        [box(500_000, 1_434_000, 500_100, 1_434_100), box(500_200, 1_434_000, 500_300, 1_434_100)],
        "EPSG:32643", "Polygon")

# Web Mercator trap: "1 km x 1 km" at 60°N is really ~0.25 km²
x, y = Transformer.from_crs(4326, 3857, always_xy=True).transform(10, 60)
shp_zip(OUT / "web_mercator_trap.zip", [box(x, y, x + 1000, y + 1000)], "EPSG:3857", "Polygon")
print("wrote", sorted(p.name for p in OUT.iterdir()))
