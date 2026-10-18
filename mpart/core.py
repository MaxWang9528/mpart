"""Split a file into self-describing ``.mpart`` parts and join them back.

Each part is named ``<filename>.<number>.mpart`` and starts with a fixed-size
header followed by a raw slice of the original file::

    MAGIC (8 bytes) | header length (uint32 LE) | header JSON | zero padding
    ... HEADER_SIZE bytes in total, then the payload ...

The header records which split the part belongs to (a random id), the original
name and size, the part's position, and the SHA-256 of its payload, so a set of
parts can be found, ordered and verified without any other files. Parts are
matched by their headers, not their filenames, so renamed parts still join.
"""

import hashlib
import json
import re
import struct
import uuid
from dataclasses import dataclass, field
from pathlib import Path

EXT = ".mpart"
MAGIC = b"MPART\x00\x01\x00"
FORMAT_VERSION = 1
HEADER_SIZE = 4096
MIN_PART_SIZE = 64 * 1024
BUF_SIZE = 8 * 1024 * 1024

_LEN = struct.Struct("<I")


class MpartError(Exception):
    """A user-facing error (missing parts, corruption, invalid files...)."""


class Cancelled(Exception):
    """Raised when the cancel event is set mid-operation."""


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


# ---- Header ----------------------------------------------------------------


@dataclass
class PartInfo:
    path: Path
    id: str
    name: str
    size: int
    index: int
    count: int
    length: int
    sha256: str


def _encode_header(fields):
    data = json.dumps(fields, separators=(",", ":")).encode("ascii")
    if len(MAGIC) + _LEN.size + len(data) > HEADER_SIZE:
        raise MpartError("File name is too long to store in a part header.")
    raw = MAGIC + _LEN.pack(len(data)) + data
    return raw + b"\0" * (HEADER_SIZE - len(raw))


def read_header(path):
    """Read and validate the header of a ``.mpart`` file."""
    path = Path(path)
    try:
        with path.open("rb") as f:
            raw = f.read(HEADER_SIZE)
        actual_length = path.stat().st_size - HEADER_SIZE
    except OSError as e:
        raise MpartError(f"{path.name}: could not read ({e.strerror or e})") from e

    if len(raw) < HEADER_SIZE or not raw.startswith(MAGIC):
        raise MpartError(f"{path.name}: not an mpart file")
    try:
        (n,) = _LEN.unpack_from(raw, len(MAGIC))
        h = json.loads(raw[len(MAGIC) + _LEN.size : len(MAGIC) + _LEN.size + n])
        if h["v"] != FORMAT_VERSION:
            raise MpartError(f"{path.name}: unsupported mpart version {h['v']}")
        info = PartInfo(
            path=path,
            id=str(h["id"]),
            # Only ever use the bare filename so a part can't write elsewhere.
            name=Path(str(h["name"])).name,
            size=int(h["size"]),
            index=int(h["index"]),
            count=int(h["count"]),
            length=int(h["length"]),
            sha256=str(h["sha256"]),
        )
    except (ValueError, KeyError, TypeError, struct.error) as e:
        raise MpartError(f"{path.name}: damaged part header") from e
    if not info.name or not 1 <= info.index <= info.count:
        raise MpartError(f"{path.name}: damaged part header")
    if actual_length != info.length:
        raise MpartError(f"{path.name}: wrong size (incomplete download?)")
    return info


# ---- Split -----------------------------------------------------------------


def part_count(total_size, part_size):
    return max(1, -(-total_size // (part_size - HEADER_SIZE)))


def _part_name(name, index, count):
    return f"{name}.{index:0{max(3, len(str(count)))}d}{EXT}"


def existing_parts(name, out_dir):
    """Files in ``out_dir`` that look like parts of a file called ``name``."""
    pattern = re.compile(re.escape(name) + r"\.\d+" + re.escape(EXT) + "$", re.IGNORECASE)
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return []
    return sorted(p for p in out_dir.iterdir() if pattern.match(p.name) and p.is_file())


def split_file(src, out_dir, part_size, progress=None, cancel=None):
    """Split ``src`` into ``.mpart`` files of at most ``part_size`` bytes each.

    ``progress(done_bytes, total_bytes)`` is called as data is written and
    ``cancel`` is an optional ``threading.Event``. Returns the part paths.
    """
    src = Path(src)
    out_dir = Path(out_dir)
    if part_size < MIN_PART_SIZE:
        raise MpartError(f"Part size must be at least {format_size(MIN_PART_SIZE)}.")

    total = src.stat().st_size
    payload_size = part_size - HEADER_SIZE
    count = part_count(total, part_size)
    split_id = uuid.uuid4().hex
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    done = 0
    try:
        with src.open("rb") as f:
            for index in range(1, count + 1):
                path = out_dir / _part_name(src.name, index, count)
                length = min(payload_size, total - done)
                fields = {
                    "v": FORMAT_VERSION,
                    "id": split_id,
                    "name": src.name,
                    "size": total,
                    "index": index,
                    "count": count,
                    "length": length,
                    "sha256": "0" * 64,
                }
                h = hashlib.sha256()
                with path.open("wb") as out:
                    written.append(path)
                    # Placeholder header; rewritten once the payload hash is known.
                    out.write(_encode_header(fields))
                    remaining = length
                    while remaining:
                        _check_cancel(cancel)
                        chunk = f.read(min(BUF_SIZE, remaining))
                        if not chunk:
                            raise MpartError("Source file changed size while splitting.")
                        out.write(chunk)
                        h.update(chunk)
                        remaining -= len(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                    fields["sha256"] = h.hexdigest()
                    out.seek(0)
                    out.write(_encode_header(fields))
    except BaseException:
        for p in written:
            p.unlink(missing_ok=True)
        raise

    if progress:
        progress(total, total)
    return written


# ---- Join ------------------------------------------------------------------


@dataclass
class PartSet:
    """All parts found for one split, keyed by part number."""

    id: str
    name: str
    size: int
    count: int
    parts: dict = field(default_factory=dict)
    duplicates: list = field(default_factory=list)

    def add(self, info):
        if (info.name, info.size, info.count) != (self.name, self.size, self.count):
            self.duplicates.append(info.path)  # inconsistent header, ignore it
        elif info.index in self.parts:
            self.duplicates.append(info.path)  # e.g. "x.001 (1).mpart"
        else:
            self.parts[info.index] = info

    @property
    def missing(self):
        return [i for i in range(1, self.count + 1) if i not in self.parts]

    @property
    def complete(self):
        return not self.missing

    @property
    def paths(self):
        return [p.path for p in self.parts.values()] + self.duplicates

    def describe_missing(self):
        nums = ", ".join(str(i) for i in self.missing[:10])
        more = "…" if len(self.missing) > 10 else ""
        return f"{self.name}: missing part(s) {nums}{more} of {self.count}"


def find_sets(directory):
    """Group every ``.mpart`` file in ``directory`` by split.

    Returns ``(sets, problems)`` where ``problems`` lists unreadable files.
    """
    sets = {}
    problems = []
    directory = Path(directory)
    if not directory.is_dir():
        raise MpartError(f"Folder not found: {directory}")
    for path in sorted(directory.iterdir()):
        if not (path.suffix.lower() == EXT and path.is_file()):
            continue
        try:
            info = read_header(path)
        except MpartError as e:
            problems.append(str(e))
            continue
        s = sets.get(info.id)
        if s is None:
            s = sets[info.id] = PartSet(info.id, info.name, info.size, info.count)
        s.add(info)
    return list(sets.values()), problems


def find_set_for(part_path):
    """Find the set that ``part_path`` belongs to, using its own folder."""
    info = read_header(part_path)
    sets, _ = find_sets(Path(part_path).parent)
    for s in sets:
        if s.id == info.id:
            return s
    raise MpartError(f"{Path(part_path).name}: could not find its sibling parts")


def join_set(part_set, out_path, progress=None, cancel=None):
    """Reassemble a complete ``PartSet`` into ``out_path``.

    Every part is checked against its SHA-256. The output is written to a
    temporary file and only renamed into place once fully verified.
    """
    if not part_set.complete:
        raise MpartError(part_set.describe_missing())
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    total = part_set.size
    done = 0
    try:
        with tmp.open("wb") as out:
            for index in range(1, part_set.count + 1):
                info = part_set.parts[index]
                h = hashlib.sha256()
                with info.path.open("rb") as f:
                    f.seek(HEADER_SIZE)
                    while chunk := f.read(BUF_SIZE):
                        _check_cancel(cancel)
                        out.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                if h.hexdigest() != info.sha256:
                    raise MpartError(f"{info.path.name} is corrupted (checksum mismatch).")
        if done != total:
            raise MpartError(f"{part_set.name}: parts don't add up to the original size.")
        tmp.replace(out_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return out_path


# ---- Helpers ---------------------------------------------------------------


def unique_path(path):
    """``path`` if it is free, otherwise ``name (2).ext``, ``name (3).ext``..."""
    path = Path(path)
    candidate = path
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def format_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1000
