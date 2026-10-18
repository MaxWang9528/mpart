"""Split a file into fixed-size parts and join them back, verified with SHA-256.

Parts are raw byte slices of the original file named ``<name>.part001``,
``<name>.part002``, ... alongside a small JSON manifest ``<name>.mpart.json``
that records the order, size and hash of every part.
"""

import hashlib
import json
import re
from pathlib import Path

MANIFEST_SUFFIX = ".mpart.json"
FORMAT_VERSION = 1
BUF_SIZE = 8 * 1024 * 1024

_PART_RE = re.compile(r"^(?P<name>.+)\.part\d+$")


class MpartError(Exception):
    """A user-facing error (missing parts, corruption, bad manifest...)."""


class Cancelled(Exception):
    """Raised when the cancel event is set mid-operation."""


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def part_count(total_size, part_size):
    return max(1, -(-total_size // part_size))


def manifest_path_for(src, out_dir):
    return Path(out_dir) / (Path(src).name + MANIFEST_SUFFIX)


def split_file(src, out_dir, part_size, progress=None, cancel=None):
    """Split ``src`` into parts of at most ``part_size`` bytes in ``out_dir``.

    ``progress(done_bytes, total_bytes)`` is called as data is written and
    ``cancel`` is an optional ``threading.Event``. Returns the manifest path.
    """
    src = Path(src)
    out_dir = Path(out_dir)
    if part_size <= 0:
        raise ValueError("part_size must be positive")

    total = src.stat().st_size
    count = part_count(total, part_size)
    width = max(3, len(str(count)))
    out_dir.mkdir(parents=True, exist_ok=True)

    parts = []
    written = []
    done = 0
    try:
        with src.open("rb") as f:
            for i in range(1, count + 1):
                name = f"{src.name}.part{i:0{width}d}"
                path = out_dir / name
                remaining = min(part_size, total - done)
                h = hashlib.sha256()
                with path.open("wb") as out:
                    written.append(path)
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
                parts.append({"name": name, "size": path.stat().st_size, "sha256": h.hexdigest()})

        manifest = {
            "mpart": FORMAT_VERSION,
            "name": src.name,
            "size": total,
            "part_size": part_size,
            "parts": parts,
        }
        mpath = manifest_path_for(src, out_dir)
        written.append(mpath)
        mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except BaseException:
        for p in written:
            p.unlink(missing_ok=True)
        raise

    if progress:
        progress(total, total)
    return mpath


def find_manifest(path):
    """Return the manifest for ``path``, which may be the manifest or any part."""
    path = Path(path)
    if path.name.endswith(MANIFEST_SUFFIX):
        return path
    m = _PART_RE.match(path.name)
    if m:
        candidate = path.with_name(m.group("name") + MANIFEST_SUFFIX)
        if candidate.exists():
            return candidate
    raise MpartError(f"Could not find a {MANIFEST_SUFFIX} manifest for '{path.name}'.")


def load_manifest(manifest_path):
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MpartError(f"Could not read manifest: {e}") from e
    if not isinstance(data, dict) or data.get("mpart") != FORMAT_VERSION:
        raise MpartError("Not a supported mpart manifest.")
    return data


def output_path_for(manifest_path, out_dir=None):
    manifest_path = Path(manifest_path)
    data = load_manifest(manifest_path)
    # Only ever use the bare filename so a manifest can't write outside out_dir.
    return Path(out_dir or manifest_path.parent) / Path(data["name"]).name


def join_file(manifest_path, out_dir=None, progress=None, cancel=None):
    """Reassemble the file described by ``manifest_path`` into ``out_dir``.

    Every part is checked against its recorded size and SHA-256. The output is
    written to a temporary file and only renamed into place once fully
    verified. Returns the path of the rebuilt file.
    """
    manifest_path = Path(manifest_path)
    data = load_manifest(manifest_path)
    parts_dir = manifest_path.parent
    out_path = output_path_for(manifest_path, out_dir)
    total = data["size"]

    problems = []
    for part in data["parts"]:
        p = parts_dir / Path(part["name"]).name
        if not p.exists():
            problems.append(f"missing: {part['name']}")
        elif p.stat().st_size != part["size"]:
            problems.append(f"wrong size: {part['name']}")
    if problems:
        raise MpartError("Cannot join, some parts are not usable:\n" + "\n".join(problems))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".mpart-tmp")
    done = 0
    try:
        with tmp.open("wb") as out:
            for part in data["parts"]:
                h = hashlib.sha256()
                with (parts_dir / Path(part["name"]).name).open("rb") as f:
                    while chunk := f.read(BUF_SIZE):
                        _check_cancel(cancel)
                        out.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                if h.hexdigest() != part["sha256"]:
                    raise MpartError(f"Part '{part['name']}' is corrupted (checksum mismatch).")
        tmp.replace(out_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return out_path


def format_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1000
