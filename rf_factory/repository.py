"""Copy local working trees without modifying the input or depending on Git."""
import os
import shutil
from pathlib import Path
from .common import FactoryError, digest, dumps
from .models import Repository

IGNORED = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}


def manifest(root: Path) -> dict[str, str]:
    result = {}
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED)
        for name in dirs + sorted(names):
            path = Path(directory) / name
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise FactoryError(f"Linked paths unsupported: {path}")
            if path.is_file() and name not in IGNORED:
                result[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return result


class RepositoryPreparer:
    def prepare(self, source: Path, destination: Path) -> Repository:
        source = source.resolve()
        if not source.is_dir():
            raise FactoryError(f"Missing repository: {source}")
        if destination.resolve().is_relative_to(source):
            raise FactoryError("Output must be outside source repository")
        before = manifest(source)
        if not before:
            raise FactoryError("Empty repository")
        shutil.copytree(source, destination, ignore=lambda _, names: set(names) & IGNORED)
        after = manifest(destination)
        if before != after or before != manifest(source):
            raise FactoryError("Source changed while preparing snapshot")
        return Repository(str(source), destination, digest(dumps(after, sort_keys=True).encode()), after)
