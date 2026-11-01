"""Small, auditable file storage primitives."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def digest(value: bytes | str | object) -> str:
    if not isinstance(value, (bytes, str)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def writer_lock(root: Path):
    """Fail closed on concurrent writers. A crashed process leaves an explicit lock."""
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "writer.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"Writer lock exists: {lock}; verify no build is running before removing it") from exc
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def current_release(root: Path) -> Path | None:
    pointer = root / ".iltkb" / "current.json"
    if not pointer.exists():
        return None
    data = read_json(pointer)
    name = data["release"]
    if not isinstance(name, str) or not name.isalnum():
        raise ValueError("Invalid release pointer")
    path = root / ".iltkb" / "releases" / name
    if not path.is_dir():
        raise ValueError("Current release is missing")
    manifest_hash = data.get("manifest_sha256")
    if manifest_hash is not None:
        if not isinstance(manifest_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
            raise ValueError("Invalid current manifest hash")
        if digest((path / "manifest.json").read_bytes()) != manifest_hash:
            raise ValueError("Current manifest hash mismatch")
    return path.resolve()
