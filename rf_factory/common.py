"""Portable serialization and filesystem primitives."""
import hashlib
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path, PurePosixPath


class FactoryError(RuntimeError):
    pass


def encode(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def dumps(value, **kwargs):
    return json.dumps(value, default=encode, ensure_ascii=False, allow_nan=False, **kwargs)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def export_jsonl(path: Path, values):
    # Exclusive creation prevents accidental replacement of earlier evidence.
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for value in values:
            stream.write(dumps(value) + "\n")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value or path.as_posix() == ".":
        raise ValueError(f"Invalid repository-relative path: {value!r}")
    return path.as_posix()
