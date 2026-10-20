import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from .common import relative


@dataclass(frozen=True)
class MigrationRecipe:
    module: str
    symbol: str
    target_symbol: str
    parameter: str
    goal: str
    behavioral_invariants: tuple[str, ...]
    compatibility_requirements: tuple[str, ...]
    functional_cases: tuple[dict, ...]
    auto_discovered: bool = False

    def __post_init__(self):
        if not self.module or not all(part.isidentifier() for part in self.module.split(".")):
            raise ValueError("Invalid migration module")
        if not all(value.isidentifier() for value in (self.symbol, self.target_symbol, self.parameter)):
            raise ValueError("Invalid migration API or parameter")
        if not self.goal or not self.behavioral_invariants or (not self.functional_cases and not self.auto_discovered):
            raise ValueError("Migration needs goal, behavior, and functional cases")
        for case in self.functional_cases:
            if not isinstance(case.get("args"), list) or not isinstance(case.get("kwargs", {}), dict):
                raise ValueError("Functional case needs args and optional kwargs")
            if ("expected" in case) == ("raises" in case):
                raise ValueError("Functional case needs exactly one of expected or raises")


DEFAULT_MIGRATION = MigrationRecipe(
    "money", "render_amount", "format_amount", "currency",
    "Use clearer naming for the public amount-formatting API and migrate dependent callers.",
    ("Preserve formatted output and validation errors.", "Keep existing regression tests passing."),
    ("Dependent callers must use the replacement API and supply currency explicitly by name.",),
    ({"args": [12.5], "kwargs": {"currency": "USD"}, "expected": "$12.50"},
     {"args": [0], "kwargs": {"currency": "EUR"}, "expected": "€0.00"}),
)


@dataclass(frozen=True)
class Config:
    source: Path
    refactoring_type: str = "api_migration"
    discovery_strategy: str = "api_migration"
    ranking_policy: str = "stable_path"
    max_candidates: int = 10
    abstraction_level: str = "medium"
    test_dir: str = "tests"
    test_pattern: str = "test*.py"
    test_timeout: float = 30
    api_migrations: tuple[MigrationRecipe, ...] = (DEFAULT_MIGRATION,)
    min_caller_modules: int = 2
    min_changed_files: int = 3

    def __post_init__(self):
        for name in ("max_candidates", "min_caller_modules", "min_changed_files"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.test_timeout) not in (int, float) or not math.isfinite(self.test_timeout) or self.test_timeout <= 0:
            raise ValueError("test_timeout must be positive and finite")
        if self.refactoring_type != "api_migration" or self.discovery_strategy != "api_migration":
            raise ValueError("Only the API migration discovery strategy is implemented")
        if self.ranking_policy != "stable_path" or self.abstraction_level != "medium":
            raise ValueError("Only stable_path ranking and medium abstraction are implemented")
        relative(self.test_dir)
        if not self.test_pattern or "/" in self.test_pattern or "\\" in self.test_pattern:
            raise ValueError("test_pattern must be a filename pattern")
        if not self.api_migrations or not all(isinstance(item, MigrationRecipe) for item in self.api_migrations):
            raise ValueError("api_migrations must contain migration recipes")
        if len({(item.module, item.symbol) for item in self.api_migrations}) != len(self.api_migrations):
            raise ValueError("Duplicate migration recipe")

    @classmethod
    def load(cls, path: Path):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if set(data) - {"repository", "pipeline", "api_migrations"} or set(data.get("repository", {})) != {"source"}:
            raise ValueError("Expected repository.source and optional pipeline section")
        source = (path.resolve().parent / Path(data["repository"]["source"]).expanduser()).resolve()
        settings = dict(data.get("pipeline", {}))
        if "api_migrations" in data:
            settings["api_migrations"] = tuple(MigrationRecipe(
                **{**item,
                   "behavioral_invariants": tuple(item["behavioral_invariants"]),
                   "compatibility_requirements": tuple(item.get("compatibility_requirements", [])),
                   "functional_cases": tuple(item["functional_cases"])})
                for item in data["api_migrations"])
        return cls(source=source, **settings)
