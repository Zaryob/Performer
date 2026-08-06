"""Building, packing and reading run bundles.

A bundle is a ``.tgz`` containing exactly one top level directory,
``run_<run_id>``, laid out as described in :mod:`oxfscope.layout`.

Reading never extracts.  Members are streamed straight out of the archive, so
a hostile or corrupt bundle cannot write anything anywhere -- relevant because
bundles are copied off target machines and opened elsewhere.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import layout, manifest as manifest_mod
from .errors import BundleError, ManifestError
from .jsonschema import load_schema

#: Refuse to read a single member larger than this.  Folded stack files from a
#: 315 thread process land in the low megabytes; anything past this is either a
#: bug or a decompression bomb.
MAX_MEMBER_BYTES = 512 * 1024 * 1024


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_rows(path: Path) -> Optional[int]:
    if path.suffix not in (".folded", ".csv"):
        return None
    rows = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                rows += 1
    if path.suffix == ".csv" and rows > 0:
        rows -= 1  # header
    return rows


class BundleBuilder:
    """Creates a run directory and turns it into an archive.

    Used by the collector during a real run and by :mod:`oxfscope.fake` to
    produce synthetic bundles.  Both go through the same code path on purpose:
    if the synthetic bundle validates, the layout is implementable.
    """

    def __init__(
        self,
        out_dir: os.PathLike | str,
        *,
        label: str,
        started_at: Optional[_dt.datetime] = None,
    ) -> None:
        if not layout.LABEL_RE.match(label):
            raise BundleError(
                f"invalid label {label!r}: letters, digits, dot, dash, underscore only"
            )
        self.label = label
        self.started_at = started_at or manifest_mod.utc_now()
        self.run_id = manifest_mod.make_run_id(label, self.started_at)
        self.out_dir = Path(out_dir)
        self.root = self.out_dir / layout.run_dir_name(self.run_id)
        if self.root.exists():
            raise BundleError(f"run directory already exists: {self.root}")
        for name in ("",) + layout.BUNDLE_DIRS:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self._written: List[str] = []

    # -- payload -------------------------------------------------------

    def path_for(self, relpath: str) -> Path:
        if not layout.is_safe_relpath(relpath):
            raise BundleError(f"unsafe bundle path {relpath!r}")
        return self.root / relpath

    def add_bytes(self, relpath: str, data: bytes) -> Path:
        target = self.path_for(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if relpath not in self._written:
            self._written.append(relpath)
        return target

    def add_text(self, relpath: str, text: str) -> Path:
        if text and not text.endswith("\n"):
            text += "\n"
        return self.add_bytes(relpath, text.encode("utf-8"))

    def add_json(self, relpath: str, document: Any) -> Path:
        payload = json.dumps(document, indent=2, sort_keys=False) + "\n"
        return self.add_bytes(relpath, payload.encode("utf-8"))

    def add_folded(self, relpath: str, entries: Iterable[Tuple[str, int]]) -> Path:
        """Write FlameGraph folded format: ``frame;frame;frame <value>``."""
        buffer = io.StringIO()
        for stack, value in entries:
            if "\n" in stack:
                raise BundleError("folded stack contains a newline")
            buffer.write(f"{stack} {value}\n")
        return self.add_bytes(relpath, buffer.getvalue().encode("utf-8"))

    def add_series(self, relpath: str, rows: Iterable[Sequence[Any]]) -> Path:
        header = layout.SERIES_HEADERS.get(relpath)
        if header is None:
            raise BundleError(f"{relpath} has no declared series header")
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            if len(row) != len(header):
                raise BundleError(
                    f"{relpath}: row has {len(row)} fields, header has {len(header)}"
                )
            writer.writerow(row)
        return self.add_bytes(relpath, buffer.getvalue().encode("utf-8"))

    def probe_log_path(self, probe: str) -> Path:
        """Where a probe's stderr goes.  Never /dev/null, never shared."""
        if not layout.PROBE_NAME_RE.match(probe):
            raise BundleError(f"invalid probe name {probe!r}")
        return self.path_for(f"{layout.DIR_RAW}/{probe}.stderr.log")

    # -- inventory + manifest ------------------------------------------

    def file_inventory(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relpath = path.relative_to(self.root).as_posix()
            if relpath == layout.MANIFEST:
                continue
            entry: Dict[str, Any] = {
                "path": relpath,
                "bytes": path.stat().st_size,
                "sha256": _sha256_of(path),
            }
            rows = _count_rows(path)
            if rows is not None:
                entry["rows"] = rows
            entries.append(entry)
        return entries

    def write_manifest(self, document: Dict[str, Any]) -> Path:
        """Attach the file inventory, validate, then write.

        Validation happens *before* the file hits disk: an invalid manifest
        must never be produced, because the viewer is entitled to trust it.
        """
        document = dict(document)
        document.setdefault("files", [])
        document["files"] = self.file_inventory()
        if document.get("run_id") != self.run_id:
            raise BundleError(
                f"manifest run_id {document.get('run_id')!r} != builder run_id "
                f"{self.run_id!r}"
            )
        problems = manifest_mod.validate_manifest(document)
        if problems:
            raise ManifestError("refusing to write an invalid manifest", problems)
        return self.add_json(layout.MANIFEST, document)

    def pack(self, *, dest_dir: Optional[os.PathLike | str] = None) -> Path:
        """Create ``oxfscope-<run_id>.tgz`` next to the run directory."""
        if not (self.root / layout.MANIFEST).is_file():
            raise BundleError("cannot pack a bundle without a manifest")
        target_dir = Path(dest_dir) if dest_dir is not None else self.out_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        archive = target_dir / layout.archive_name(self.run_id)
        arcname = self.root.name

        def _filter(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
            # Reproducible-ish and free of local uid/gid leakage.
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mode = 0o755 if info.isdir() else 0o644
            return info

        with tarfile.open(archive, "w:gz") as tar:
            tar.add(self.root, arcname=arcname, filter=_filter)
        return archive


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


class _Source:
    """Read only access to bundle contents, directory or archive backed."""

    def paths(self) -> List[str]:
        raise NotImplementedError

    def read_bytes(self, relpath: str) -> bytes:
        raise NotImplementedError

    def exists(self, relpath: str) -> bool:
        return relpath in self.paths()

    def close(self) -> None:
        pass


class _DirSource(_Source):
    def __init__(self, root: Path) -> None:
        self.root = root
        self._paths = sorted(
            p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
        )

    def paths(self) -> List[str]:
        return list(self._paths)

    def read_bytes(self, relpath: str) -> bytes:
        if not layout.is_safe_relpath(relpath):
            raise BundleError(f"unsafe bundle path {relpath!r}")
        path = self.root / relpath
        if not path.is_file():
            raise BundleError(f"{relpath} not found in bundle")
        if path.stat().st_size > MAX_MEMBER_BYTES:
            raise BundleError(f"{relpath} is implausibly large, refusing to read")
        return path.read_bytes()


class _TarSource(_Source):
    def __init__(self, archive: Path) -> None:
        self.archive = archive
        try:
            self._tar = tarfile.open(archive, "r:*")
        except tarfile.TarError as exc:
            raise BundleError(f"{archive} is not a readable tar archive: {exc}") from exc
        self._members: Dict[str, tarfile.TarInfo] = {}
        self.run_dir: Optional[str] = None
        try:
            self._index()
        except BundleError:
            self._tar.close()
            raise

    def _index(self) -> None:
        tops = set()
        for info in self._tar.getmembers():
            name = info.name
            if name.startswith("./"):
                name = name[2:]
            if not name or name == ".":
                continue
            if info.issym() or info.islnk():
                raise BundleError(
                    f"{self.archive}: contains a link member ({info.name}); "
                    "bundles are plain files only"
                )
            if info.isdev() or info.isfifo():
                raise BundleError(f"{self.archive}: contains a device/fifo member")
            if not info.isfile() and not info.isdir():
                raise BundleError(f"{self.archive}: unsupported member type in {name}")
            parts = name.split("/")
            if any(part in ("", ".", "..") for part in parts) or name.startswith("/"):
                raise BundleError(f"{self.archive}: unsafe member path {info.name!r}")
            tops.add(parts[0])
            if info.isfile():
                if info.size > MAX_MEMBER_BYTES:
                    raise BundleError(f"{self.archive}: member {name} is too large")
                self._members["/".join(parts[1:])] = info
        if len(tops) != 1:
            raise BundleError(
                f"{self.archive}: expected exactly one top level run directory, "
                f"found {sorted(tops) or 'none'}"
            )
        self.run_dir = tops.pop()

    def paths(self) -> List[str]:
        return sorted(p for p in self._members if p)

    def read_bytes(self, relpath: str) -> bytes:
        info = self._members.get(relpath)
        if info is None:
            raise BundleError(f"{relpath} not found in bundle")
        handle = self._tar.extractfile(info)
        if handle is None:
            raise BundleError(f"{relpath} is not a regular file")
        with handle:
            return handle.read(MAX_MEMBER_BYTES + 1)[:MAX_MEMBER_BYTES]

    def close(self) -> None:
        self._tar.close()


@dataclass
class FileCheck:
    path: str
    problems: List[str]


@dataclass
class ValidationReport:
    """Result of validating every schema governed file in a bundle."""

    checked: List[str]
    problems: List[FileCheck]

    @property
    def ok(self) -> bool:
        return not self.problems

    def flat(self) -> List[str]:
        return [f"{c.path}: {p}" for c in self.problems for p in c.problems]


class Bundle:
    """A run bundle opened for reading."""

    def __init__(self, source: _Source, origin: Path) -> None:
        self._source = source
        self.origin = origin
        try:
            raw = source.read_bytes(layout.MANIFEST)
        except BundleError as exc:
            source.close()
            raise BundleError(f"{origin}: no {layout.MANIFEST} in bundle") from exc
        try:
            self.manifest: Dict[str, Any] = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            source.close()
            raise ManifestError(f"{origin}: manifest.json is not valid JSON: {exc}") from exc
        if not isinstance(self.manifest, dict):
            source.close()
            raise ManifestError(f"{origin}: manifest.json is not a JSON object")

    # -- construction ---------------------------------------------------

    @classmethod
    def open(cls, path: os.PathLike | str) -> "Bundle":
        target = Path(path)
        if target.is_dir():
            if not (target / layout.MANIFEST).is_file():
                raise BundleError(f"{target} is not a run directory (no manifest.json)")
            return cls(_DirSource(target), target)
        if not target.is_file():
            raise BundleError(f"{target} does not exist")
        return cls(_TarSource(target), target)

    def __enter__(self) -> "Bundle":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        self._source.close()

    # -- accessors ------------------------------------------------------

    @property
    def run_id(self) -> str:
        return str(self.manifest.get("run_id", "<unknown>"))

    @property
    def label(self) -> str:
        return str(self.manifest.get("label", "<unknown>"))

    @property
    def status(self) -> str:
        return str(self.manifest.get("status", "unknown"))

    def paths(self) -> List[str]:
        return self._source.paths()

    def exists(self, relpath: str) -> bool:
        return self._source.exists(relpath)

    def read_bytes(self, relpath: str) -> bytes:
        return self._source.read_bytes(relpath)

    def read_text(self, relpath: str) -> str:
        return self._source.read_bytes(relpath).decode("utf-8", errors="replace")

    def read_json(self, relpath: str) -> Any:
        try:
            return json.loads(self.read_text(relpath))
        except json.JSONDecodeError as exc:
            raise BundleError(f"{relpath}: invalid JSON: {exc}") from exc

    def iter_folded(self, relpath: str) -> Iterator[Tuple[str, int]]:
        """Yield ``(stack, value)`` pairs from a folded stack file."""
        for lineno, line in enumerate(self.read_text(relpath).splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            stack, _, value = line.rpartition(" ")
            if not stack:
                raise BundleError(f"{relpath}:{lineno}: malformed folded line")
            try:
                yield stack, int(value)
            except ValueError as exc:
                raise BundleError(
                    f"{relpath}:{lineno}: folded value {value!r} is not an integer"
                ) from exc

    def series_rows(self, relpath: str) -> List[Dict[str, str]]:
        header = layout.SERIES_HEADERS.get(relpath)
        text = self.read_text(relpath)
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            raise BundleError(f"{relpath} is empty")
        if header is not None and tuple(rows[0]) != header:
            raise BundleError(
                f"{relpath}: unexpected header {rows[0]}, expected {list(header)}"
            )
        return [dict(zip(rows[0], row)) for row in rows[1:] if row]

    # -- validation -----------------------------------------------------

    def validate(self, *, verify_hashes: bool = False) -> ValidationReport:
        checked: List[str] = []
        failures: List[FileCheck] = []

        problems = manifest_mod.validate_manifest(self.manifest)
        checked.append(layout.MANIFEST)
        if problems:
            failures.append(FileCheck(layout.MANIFEST, problems))

        for relpath in self.paths():
            if relpath == layout.MANIFEST:
                continue
            kind: Optional[str] = None
            document: Any = None
            if relpath.endswith(".json"):
                try:
                    document = self.read_json(relpath)
                except BundleError as exc:
                    failures.append(FileCheck(relpath, [str(exc)]))
                    checked.append(relpath)
                    continue
                if isinstance(document, dict):
                    kind = document.get("kind")
            schema_name = layout.schema_for(relpath, kind)
            if schema_name is None:
                if relpath in layout.SERIES_HEADERS:
                    checked.append(relpath)
                    try:
                        self.series_rows(relpath)
                    except BundleError as exc:
                        failures.append(FileCheck(relpath, [str(exc)]))
                continue
            checked.append(relpath)
            validator = load_schema(layout.schema_path(schema_name))
            errors = [str(e) for e in validator.validate(document)]
            if errors:
                failures.append(FileCheck(relpath, errors))

        failures.extend(self._inventory_problems(verify_hashes=verify_hashes))
        return ValidationReport(checked=checked, problems=failures)

    def _inventory_problems(self, *, verify_hashes: bool) -> List[FileCheck]:
        entries = self.manifest.get("files")
        if not isinstance(entries, list):
            return []
        present = set(self.paths())
        listed = set()
        problems: List[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not isinstance(path, str):
                continue
            listed.add(path)
            if path not in present:
                problems.append(f"listed in manifest.files but missing: {path}")
                continue
            if verify_hashes and isinstance(entry.get("sha256"), str):
                actual = hashlib.sha256(self.read_bytes(path)).hexdigest()
                if actual != entry["sha256"]:
                    problems.append(f"sha256 mismatch for {path}")
        for path in sorted(present - listed - {layout.MANIFEST}):
            problems.append(f"present in bundle but not listed in manifest.files: {path}")
        return [FileCheck("manifest.json:files", problems)] if problems else []


def open_bundle(path: os.PathLike | str) -> Bundle:
    return Bundle.open(path)
