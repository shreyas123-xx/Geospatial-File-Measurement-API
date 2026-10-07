"""Upload handling: streaming to disk with limits, type sniffing, safe archive extraction."""
from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from fastapi import UploadFile

from app.config import Settings

CHUNK = 1024 * 1024
SUPPORTED_HINT = "Upload a .zip containing a Shapefile (.shp/.shx/.dbf/.prj), a .kml, or a .kmz."


class IngestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class IngestResult:
    file_type: str            # shapefile | kml | kmz
    size_bytes: int
    datasets: list[str]       # paths relative to the storage dir
    warnings: list[str] = field(default_factory=list)


def _detect(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".zip":
        return "shapefile"
    if ext == ".kml":
        return "kml"
    if ext == ".kmz":
        return "kmz"
    raise IngestError(415, "unsupported_file_type", f"Unsupported file type '{ext or filename}'. {SUPPORTED_HINT}")


def _stream_to_disk(upload: UploadFile, dest: Path, max_bytes: int) -> int:
    size = 0
    with open(dest, "wb") as out:
        while chunk := upload.file.read(CHUNK):
            size += len(chunk)
            if size > max_bytes:
                raise IngestError(413, "file_too_large", f"File exceeds the {max_bytes // (1024 * 1024)} MB upload limit.")
            out.write(chunk)
    if size == 0:
        raise IngestError(422, "empty_file", "The uploaded file is empty.")
    return size


def _looks_like_kml(path: Path) -> bool:
    head = path.read_bytes()[:8192].lower()
    return b"<kml" in head


def _safe_extract(archive: Path, dest: Path, settings: Settings) -> list[str]:
    """Extract without trusting the archive: no traversal, no encrypted members, bounded size/count."""
    if not zipfile.is_zipfile(archive):
        raise IngestError(422, "invalid_archive", "File is not a valid zip archive.")
    dest_root = dest.resolve()
    extracted: list[str] = []
    total = 0
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > settings.max_archive_entries:
                raise IngestError(422, "archive_too_large", f"Archive has more than {settings.max_archive_entries} files.")
            for info in infos:
                name = info.filename.replace("\\", "/")
                pure = PurePosixPath(name)
                if pure.parts and pure.parts[0] == "__MACOSX" or pure.name.startswith("._"):
                    continue  # macOS resource-fork noise
                if pure.is_absolute() or ".." in pure.parts:
                    raise IngestError(422, "unsafe_archive", f"Archive contains an unsafe path: '{info.filename}'.")
                if info.flag_bits & 0x1:
                    raise IngestError(422, "encrypted_archive", "Encrypted archives are not supported.")
                target = (dest_root / pure).resolve()
                if not target.is_relative_to(dest_root):
                    raise IngestError(422, "unsafe_archive", f"Archive contains an unsafe path: '{info.filename}'.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    while chunk := src.read(CHUNK):
                        total += len(chunk)
                        if total > settings.max_uncompressed_bytes:
                            raise IngestError(422, "archive_too_large", "Archive is too large once uncompressed.")
                        out.write(chunk)
                extracted.append(pure.as_posix())
    except zipfile.BadZipFile as exc:
        raise IngestError(422, "invalid_archive", f"Corrupt zip archive: {exc}") from exc
    return extracted


def ingest_upload(upload: UploadFile, storage_dir: Path, settings: Settings) -> IngestResult:
    """Validate + store an upload under ``storage_dir``. Raises :class:`IngestError` on bad input."""
    filename = upload.filename or ""
    file_type = _detect(filename)
    storage_dir.mkdir(parents=True, exist_ok=True)
    tmp = storage_dir / "_upload.tmp"
    size = _stream_to_disk(upload, tmp, settings.max_upload_bytes)
    warnings: list[str] = []

    if file_type == "kml":
        if not _looks_like_kml(tmp):
            raise IngestError(422, "invalid_kml", "File does not look like KML (no <kml> root element).")
        tmp.rename(storage_dir / "source.kml")
        return IngestResult("kml", size, ["source.kml"], warnings)

    src_dir = storage_dir / "src"
    src_dir.mkdir()
    names = _safe_extract(tmp, src_dir, settings)
    tmp.unlink(missing_ok=True)

    if file_type == "kmz":
        kmls = sorted(n for n in names if n.lower().endswith(".kml"))
        if not kmls:
            raise IngestError(422, "no_kml_found", "KMZ archive does not contain a .kml file.")
        # prefer the conventional doc.kml
        kmls.sort(key=lambda n: (PurePosixPath(n).name.lower() != "doc.kml", n))
        return IngestResult("kmz", size, [f"src/{kmls[0]}"], warnings)

    shps = sorted(n for n in names if n.lower().endswith(".shp"))
    if not shps:
        raise IngestError(
            422, "no_shapefile_found",
            "Zip archive does not contain a .shp file. " + SUPPORTED_HINT,
        )
    lower = {n.lower() for n in names}
    for shp in shps:
        stem = shp[:-4].lower()
        if f"{stem}.dbf" not in lower:
            warnings.append(f"{shp}: missing .dbf sidecar - attributes will be empty.")
        if f"{stem}.prj" not in lower:
            warnings.append(f"{shp}: missing .prj sidecar - CRS unknown (use `assume_crs` if you know it).")
    return IngestResult("shapefile", size, [f"src/{n}" for n in shps], warnings)


def remove_storage(storage_dir: str | Path) -> None:
    shutil.rmtree(storage_dir, ignore_errors=True)
