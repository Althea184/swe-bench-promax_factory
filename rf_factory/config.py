import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from .common import relative


@dataclass(frozen=True)
class Config:
    source: Path
    min_function_lines: int = 10
    max_candidates: int = 10
    test_dir: str = "tests"
    test_pattern: str = "test*.py"
    test_timeout: float = 30
    require_tests: bool = True

    def __post_init__(self):
        for name in ("min_function_lines", "max_candidates"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.test_timeout) not in (int, float) or not math.isfinite(self.test_timeout) or self.test_timeout <= 0:
            raise ValueError("test_timeout must be positive and finite")
        if type(self.require_tests) is not bool:
            raise ValueError("require_tests must be boolean")
        relative(self.test_dir)
        if not self.test_pattern or "/" in self.test_pattern or "\\" in self.test_pattern:
            raise ValueError("test_pattern must be a filename pattern")

    @classmethod
    def load(cls, path: Path):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if set(data) - {"repository", "pipeline"} or set(data.get("repository", {})) != {"source"}:
            raise ValueError("Expected repository.source and optional pipeline section")
        source = (path.resolve().parent / Path(data["repository"]["source"]).expanduser()).resolve()
        return cls(source=source, **data.get("pipeline", {}))
