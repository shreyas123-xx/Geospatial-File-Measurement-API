# Geospatial File Measurement API

A FastAPI service that accepts a **Shapefile (`.zip`)**, **KML** or **KMZ**, extracts every feature
(index, geometry type, geometry, CRS, attributes) and returns **area, perimeter and length** — computed
correctly on the ellipsoid, never in raw lat/lon degrees.

**Highlights beyond the minimum requirements**

- Measurements are **cross-checked** against an independent geodesic calculation on every feature
  (`max_deviation_pct` is returned, so correctness is visible, not assumed).
- Handles **any input CRS**, including distorting ones (a "1 km²" square in Web Mercator at 60°N is
  really 0.25 km² – the API gets that right; see `samples/web_mercator_trap.zip`).
- Multi-layer files (KML folders, several `.shp` in one zip), `MultiGeometry`/`GeometryCollection`,
  polygons with holes, invalid/self-intersecting polygons (auto-repaired + warning), null geometries,
  antimeridian-crossing and polar features.
- Unit selection (`sqm, sqkm, ha, acre, sqft, sqmi` / `m, km, mi, ft, nmi`), pagination, filtering,
  per-file summaries, GeoJSON export with on-the-fly reprojection.
- **Safe uploads**: size cap, zip-slip / zip-bomb / encrypted-archive protection, content sniffing.
- **Graceful failure**: a bad feature never fails a file; a bad file never causes a 500 – it is recorded as
  `FAILED` with an actionable message (e.g. “no .prj – re-upload with `assume_crs`”).
- Optional **background processing** (`?async=true`) with a real `PENDING → PROCESSING → COMPLETED/FAILED` lifecycle.
- 37 tests (analytic ground truth + API + security), Dockerfile, OpenAPI docs at `/docs`.

---

## Setup

Requires Python 3.10+ (developed on 3.12). No system GDAL/PROJ needed — `pyogrio`, `pyproj` and `shapely`
ship wheels that bundle them.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload          # http://127.0.0.1:8000  (interactive docs: /docs)
pytest -q                              # run the test suite
python scripts/make_samples.py         # (re)generate ./samples
```

Docker:

```bash
docker build -t geo-measure . && docker run -p 8000:8000 -v geo-data:/data geo-measure
```

Configuration (environment variables, optional `.env`):

| Variable | Default | Meaning |
|---|---|---|
| `GEO_DATA_DIR` | `./data` | SQLite DB + stored uploads |
| `GEO_DATABASE_URL` | SQLite in data dir | Any SQLAlchemy URL |
| `GEO_MAX_UPLOAD_BYTES` | 100 MB | Max upload size |
| `GEO_MAX_UNCOMPRESSED_BYTES` | 1 GB | Zip-bomb guard |
| `GEO_MAX_ARCHIVE_ENTRIES` | 500 | Max files inside a zip |

---

## API

Interactive docs: `GET /docs` (Swagger) · `GET /redoc` · `GET /openapi.json`

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/files/` | Upload + process a file |
| `GET` | `/api/files/` | List uploaded files (`status`, `limit`, `offset`) |
| `GET` | `/api/files/{id}/` | File info + processing status |
| `GET` | `/api/files/{id}/measurements/` | Per-feature measurements + totals |
| `GET` | `/api/files/{id}/features/` | All features as GeoJSON (with measurements) |
| `GET` | `/api/files/{id}/features/{index}/` | One feature |
| `DELETE` | `/api/files/{id}/` | Delete file, features and stored data |
| `GET` | `/health` | Liveness probe |

### Upload — `POST /api/files/`

`multipart/form-data`

| Field | Type | |
|---|---|---|
| `file` | file | `.zip` (Shapefile), `.kml` or `.kmz` |
| `assume_crs` | form, optional | CRS to use **only if the file has none**, e.g. `EPSG:4326` |
| `async` | query, optional | `true` → return `202` immediately and process in the background |

```bash
curl -X POST http://localhost:8000/api/files/ -F "file=@samples/bengaluru_survey.kml"
```

```json
{
  "id": "dee2419fded54b998e93756791262c08",
  "filename": "bengaluru_survey.kml",
  "file_type": "kml",
  "size_bytes": 1486,
  "status": "COMPLETED",
  "feature_count": 5,
  "crs": "EPSG:4326",
  "crs_assumed": false,
  "layers": [
    {"name": "Plots", "dataset": "source.kml", "feature_count": 2, "crs": "EPSG:4326", "geometry_types": {"Polygon": 2}},
    {"name": "Infrastructure", "dataset": "source.kml", "feature_count": 3, "crs": "EPSG:4326",
     "geometry_types": {"LineString": 1, "Point": 1, "GeometryCollection": 1}}
  ],
  "geometry_type_counts": {"Polygon": 2, "LineString": 1, "Point": 1, "GeometryCollection": 1},
  "warnings": [],
  "error": null,
  "created_at": "2026-10-07T19:01:27.679927Z",
  "completed_at": "2026-10-07T19:01:27.702113Z",
  "links": {"self": "/api/files/dee2419f.../", "measurements": "/api/files/dee2419f.../measurements/", "features": "/api/files/dee2419f.../features/"}
}
```

Status codes: `201` processed · `202` accepted (async) · `413` too large · `415` unsupported type ·
`422` invalid upload (bad zip, no `.shp`, fake KML, unsafe paths, bad `assume_crs`, …).

> A file that is *structurally* fine but can't be *read* (corrupt `.shp`, missing CRS) is stored and returned with
> `"status": "FAILED"` and an `error` message, so the failure is inspectable later via `GET /api/files/{id}/`.

### File info — `GET /api/files/{id}/`

Returns the object shown above (`id`, `filename`, `feature_count`, `crs`, `status`, plus layers, warnings, timestamps).

### Measurements — `GET /api/files/{id}/measurements/`

| Query param | Default | |
|---|---|---|
| `area_unit` | `sqm` | `sqm, sqkm, ha, acre, sqft, sqmi` |
| `length_unit` | `m` | `m, km, mi, ft, nmi` (also used for perimeters) |
| `geometry_type` / `status` / `layer` | – | filters |
| `include_geometry` / `include_properties` | `false` | embed GeoJSON geometry (source CRS) / attributes |
| `limit` / `offset` | `100` / `0` | pagination (max limit 1000) |

```bash
curl "http://localhost:8000/api/files/$ID/measurements/?area_unit=ha&include_properties=true"
```

```json
{
  "file_id": "dee2419f...", "filename": "bengaluru_survey.kml", "status": "COMPLETED",
  "units": {"area": "ha", "length": "m"},
  "summary": {
    "feature_count": 5,
    "by_status": {"MEASURED": 4, "NOT_APPLICABLE": 1},
    "by_geometry_type": {
      "Polygon": {"count": 2, "total_area": 22.805794, "total_perimeter": 3067.782026},
      "LineString": {"count": 1, "total_length": 1464.869906},
      "Point": {"count": 1},
      "GeometryCollection": {"count": 1, "total_length": 154.954609}
    },
    "total_area": 22.805794, "total_perimeter": 3067.782026, "total_length": 1619.824514
  },
  "pagination": {"total": 5, "limit": 100, "offset": 0},
  "features": [
    {
      "index": 0, "fid": 1, "layer": "Plots", "geometry_type": "Polygon", "crs": "EPSG:4326",
      "status": "MEASURED", "warnings": [],
      "measurements": {
        "area": 4.801235,
        "perimeter": 876.510366,
        "geodesic": {"area": 4.801235, "perimeter": 876.510312},
        "max_deviation_pct": 6e-06,
        "projection": {"area": "+proj=laea +lat_0=13.0 +lon_0=78.0 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"}
      },
      "properties": {"Name": "Plot A", "description": "Residential plot", "owner": "Rao"}
    }
  ]
}
```

Per-feature `status`:

| Status | Meaning |
|---|---|
| `MEASURED` | area/perimeter and/or length computed |
| `NOT_APPLICABLE` | points – nothing to measure (still returned, geometry preserved) |
| `SKIPPED` | null or empty geometry |
| `UNSUPPORTED` | geometry could not be decoded (e.g. curve types) – `message` explains |
| `ERROR` | unexpected failure for this feature only; the rest of the file is unaffected |

### Features (GeoJSON) — `GET /api/files/{id}/features/`

A `FeatureCollection` (`application/geo+json`). Geometry is returned in the **source CRS** by default;
pass `output_crs=EPSG:4326` (or any CRS) to reproject. Each feature carries `properties` (source attributes),
`measurements` and a `meta` block (layer, source CRS, status, warnings).

### Errors

All errors share one shape:

```json
{"error": {"code": "unsupported_file_type", "message": "Unsupported file type '.txt'. Upload a .zip containing ..."}}
```

`409 not_ready` / `409 processing_failed` are returned by `/measurements/` and `/features/` when the file is not `COMPLETED`.

---

## Architecture

```
app/
├── main.py            # app factory, error handlers, wiring
├── config.py          # env-driven settings
├── db.py, models.py   # SQLAlchemy 2.0: FileRecord, FeatureRecord (SQLite by default)
├── schemas.py         # Pydantic response models (drive the OpenAPI docs)
├── units.py           # SI -> requested unit conversion
├── api/files.py       # HTTP layer only: validation, pagination, serialisation
└── services/
    ├── ingest.py      # streaming upload, sniffing, safe zip extraction
    ├── reader.py      # GDAL (pyogrio) -> ParsedFeature, chunked
    ├── measure.py     # CRS handling + measurement engine (pure functions, no I/O)
    └── processing.py  # orchestrates read -> measure -> persist, owns status lifecycle
```

Routes contain no geospatial logic; `measure.py` has no database or HTTP knowledge, which is what makes it
testable against analytically known values (`tests/test_measure.py`).

### File-processing flow

```
POST /api/files/
  1. ingest   stream to disk (size cap) -> sniff type -> safe-extract zip/kmz -> locate .shp / .kml
              (bad input => 4xx, nothing is persisted, temp files removed)
  2. record   FileRecord(status=PENDING)
  3. process  (inline, or BackgroundTask when ?async=true)
              status=PROCESSING
              for each dataset -> for each layer:
                  resolve CRS (file CRS, else `assume_crs`, else FAIL with guidance)
                  read features in chunks of 5000  (bounded memory on the read side)
                  per feature: measure -> FeatureRecord  (inserted in batches of 1000)
              aggregate: feature_count, layers, geometry-type counts, common CRS (or MIXED), warnings
              status=COMPLETED   |  FAILED(error) with partial rows rolled back
  4. respond  FileOut
```

### Measurement flow & CRS handling

For every feature (`services/measure.py`):

1. **Validate** – invalid polygons (bow-ties, self-intersections) are repaired with `make_valid` and a warning is attached,
   because a signed-area formula on a self-intersecting ring silently under-counts.
2. **Decompose** – `Multi*` / `GeometryCollection` are flattened; polygons contribute area + perimeter, lines contribute length, points nothing.
3. **Normalise to WGS84 lon/lat** (`always_xy=True`, so axis order can never bite). This applies to *every* input CRS,
   including projected ones: a source projection such as Web Mercator is not area-preserving, so measuring in its native units would be wrong.
4. **Project locally**, centred on the feature's centroid:
   - polygons → **Lambert Azimuthal Equal-Area** (area is preserved exactly anywhere on Earth)
   - lines → **Azimuthal Equidistant** (distances from the centre are exact; distortion negligible for realistic extents)
5. **Cross-check geodesically** with `pyproj.Geod` on the WGS84 ellipsoid and return `geodesic` values plus `max_deviation_pct`.
   For parcel-sized features (20 000 random test parcels) the worst deviation is ≈ 0.0001 %; even for 2°-wide polygons it stays below 0.05 %.
6. Everything is stored in SI units (m, m²); unit conversion happens at response time, so one stored result serves any unit request.

Accuracy checks that are pinned in the tests: 1°×1° at the equator = 12 308.78 km²; 1 km Web-Mercator square at 60°N = 0.2508 km²
(not 1 km²); polygon holes subtract; a split antimeridian polygon measures as the sum of its halves; polar polygons; hole winding order.

---

## Design decisions (and alternatives considered)

| Decision | Why | Alternatives considered |
|---|---|---|
| **LAEA for area, AEQD for length, centred per feature** | No single projection is right worldwide. Equal-area guarantees area; equidistant is the natural choice for length. Works for any location, any extent, no zone logic. | *UTM zone per feature*: familiar but distorts area slightly, needs zone selection, breaks across zone boundaries and at poles. *One global equal-area CRS (e.g. EPSG:6933)*: simple but distortion grows with latitude. *Geodesic only*: most accurate, but the brief asks for projected measurement – so it is kept as the validator instead. |
| **Geodesic cross-check returned in every result** | Makes correctness observable and catches bad CRS metadata or exotic inputs. (It already caught a real bug in development: KML hole rings wound the same way as the shell.) | Trusting the projection silently. |
| **Always reproject through WGS84, even for projected inputs** | Equal-area is guaranteed regardless of how distorting the source CRS is. | Measuring natively in projected units – wrong for Web Mercator and similar. |
| **Centre rounded to 1°** | Building a PROJ transformer costs more than the measurement; caching them gave ~3.8× end-to-end speed-up with no measurable accuracy loss (area is exact for any LAEA centre). | Exact centroid per feature (slow); one projection per file (breaks for continent-spanning files). |
| **GDAL via `pyogrio`** (not `geopandas`/`fiona`) | Single reader for Shapefile *and* KML, wheels bundle GDAL, far lighter than pulling pandas/geopandas, supports chunked reads. | `geopandas` (heavy, whole-file loads); `fastkml`/`pyshp` (two code paths, weaker CRS handling). |
| **SQLite + SQLAlchemy 2.0** | Zero-setup persistence so `GET`s work after the request, restarts and across workers; swap to Postgres via `GEO_DATABASE_URL`. | In-memory dict (lost on restart, not multi-worker safe); PostGIS (overkill for the brief, but the natural production path). |
| **Features stored as rows with GeoJSON + SI measurements** | Enables DB-level pagination/filtering and lets summaries be computed from narrow column reads. | One JSON blob per file (no pagination, memory-heavy). |
| **Sync by default, opt-in async** | Small files return the finished result in one round-trip (what the spec’s example implies). `?async=true` + status polling covers large files. | Always async (worse DX for the common case); Celery/RQ (see future scope). |
| **Two failure tiers** | Structural problems (not a zip, no `.shp`) are the client's fault → `4xx`, nothing stored. Data problems (unreadable layer, no CRS) → stored as `FAILED` with a message, so they are diagnosable. | Always 4xx (loses the audit trail); always 201 (hides client errors). |
| **No CRS ⇒ fail, unless `assume_crs`** | Guessing "it's probably lat/lon" silently produces wrong areas. An explicit opt-in is honest. (KML is always WGS84 by spec, so it never needs this.) | Silently assume EPSG:4326. |
| **Z coordinates dropped (`force_2d`)** | Area/length are planar-on-surface quantities; KML’s altitude would otherwise add noise. | 3-D lengths – out of scope. |
| **Unit conversion at the edge** | One canonical stored unit, no rounding drift. | Store per-unit values. |

### Known limitations

- Very large files are read in chunks, but each layer's features are still accumulated through one DB transaction and the response endpoints are paginated — a multi-million-feature file would want streaming/COPY-style inserts.
- Features larger than a hemisphere can't use a local projection (reported as `ERROR` for that feature, not a crash).
- Polygons wound the "long way" across the antimeridian in a single ring are ambiguous in lon/lat; correctly split geometries (the norm) work.
- Shapefile attribute names are limited to 10 chars by the format; KML only exposes `ExtendedData` fields that GDAL's schema scan finds.
- No authentication / rate limiting (out of scope for the exercise).
- Background tasks run in-process; they don't survive a restart (an interrupted file stays `PROCESSING`).

---

## Performance

Measured on the dev sandbox with 20 000 random parcels (single process, SQLite):

| Stage | Throughput |
|---|---|
| Measurement engine alone | ~8 800 features / s |
| Full upload → read → measure → persist | ~3 800 features / s (20 000 polygons in 5.2 s) |
| `GET /measurements/` (1 000 features + whole-file summary) | ~0.4 s |

---

## Learnings

- **The brief's one-liner hides several CRS traps.** "Reproject then measure" isn't enough: *which* projection matters, a
  projected source CRS can be just as wrong as degrees, and axis order (lat/lon vs lon/lat) is a silent bug source — hence `always_xy`.
- **Build an independent check, not just a calculation.** The geodesic cross-check found a genuine defect during manual
  testing: `pyproj` ring areas are signed by winding order and GDAL returns KML holes wound like the shell, so holes were being *added*.
  The unit tests I wrote first had passed only because their hole happened to be wound the opposite way. A regression test now covers both.
- **Profile before optimising.** The slow part wasn't geometry maths – it was constructing PROJ transformers per feature.
  Caching them made the whole pipeline 3.8× faster.
- **KML is not a flat file.** Folders become layers, `MultiGeometry` becomes `GeometryCollection`, GDAL exposes style columns with sentinel
  values (`-1`, `NaT`) that need filtering, and empty folders are common.
- **Decide what "failure" means.** Separating client mistakes (4xx, nothing stored) from data problems (stored `FAILED`) and from
  single-feature problems (`ERROR` row) keeps the API predictable.

## Future scope

- **Job queue** (Celery/RQ/ARQ) + progress reporting + resumable processing for very large files; startup sweep that fails stale `PROCESSING` rows.
- **PostGIS** storage with spatial indexes: `bbox`/intersects filtering, server-side aggregation, tile/vector-tile output.
- **Streaming ingest** (`COPY`/bulk insert) and object storage (S3) for uploads.
- More formats: GeoJSON, GeoPackage, GPX, File Geodatabase; `.shp.zip` detection by content rather than extension.
- Extra measurements: 3-D length with elevation, bearing, centroid/bbox, compactness, polygon area by overlap/dissolve, intersections.
- Auth (API keys/OAuth), per-user quotas, rate limiting, upload virus scanning, retention/TTL cleanup of old files.
- Observability: structured logs, request IDs, Prometheus metrics; CI (lint + tests) and a pinned lockfile.
- Optional high-accuracy mode for continent-scale features (per-vertex geodesic area via `Geod` as the primary result).
