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


@dataclass(frozen=True)
class ResponsibilityRecipe:
    source_modules: tuple[str, ...]
    target_module: str
    symbol: str
    goal: str
    behavioral_invariants: tuple[str, ...]
    functional_cases: tuple[dict, ...]
    class_name: str = ""
    target_imports: tuple[str, ...] = ()

    def __post_init__(self):
        modules = (*self.source_modules, self.target_module)
        if len(self.source_modules) < 2 or len(set(modules)) != len(modules):
            raise ValueError("Responsibility migration needs distinct source modules and a target module")
        if not all(module and all(part.isidentifier() for part in module.split(".")) for module in modules):
            raise ValueError("Invalid responsibility module")
        if not self.symbol.isidentifier() or not self.goal or not self.behavioral_invariants or (not self.functional_cases and not self.class_name):
            raise ValueError("Responsibility migration needs a symbol, goal, behavior, and cases")
        if self.class_name and not self.class_name.isidentifier():
            raise ValueError("Invalid source class")
        if any(not item.startswith("from ") or " import " not in item or "\n" in item for item in self.target_imports):
            raise ValueError("Invalid target import")
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
    responsibility_migrations: tuple[ResponsibilityRecipe, ...] = ()
    min_caller_modules: int = 2
    min_changed_files: int = 3
    include_local_calls: bool = False

    def __post_init__(self):
        for name in ("max_candidates", "min_caller_modules", "min_changed_files"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.test_timeout) not in (int, float) or not math.isfinite(self.test_timeout) or self.test_timeout <= 0:
            raise ValueError("test_timeout must be positive and finite")
        if (self.refactoring_type, self.discovery_strategy) not in {
                ("api_migration", "api_migration"), ("module_responsibility_migration", "module_responsibility_migration")}:
            raise ValueError("Unsupported refactoring and discovery strategy")
        if self.ranking_policy != "stable_path" or self.abstraction_level != "medium":
            raise ValueError("Only stable_path ranking and medium abstraction are implemented")
        relative(self.test_dir)
        if not self.test_pattern or "/" in self.test_pattern or "\\" in self.test_pattern:
            raise ValueError("test_pattern must be a filename pattern")
        if not self.api_migrations or not all(isinstance(item, MigrationRecipe) for item in self.api_migrations):
            raise ValueError("api_migrations must contain migration recipes")
        if self.refactoring_type == "module_responsibility_migration" and not self.responsibility_migrations:
            raise ValueError("responsibility_migrations must contain recipes")
        if not all(isinstance(item, ResponsibilityRecipe) for item in self.responsibility_migrations):
            raise ValueError("Invalid responsibility recipe")
        if len({(item.module, item.symbol) for item in self.api_migrations}) != len(self.api_migrations):
            raise ValueError("Duplicate migration recipe")

    @classmethod
    def load(cls, path: Path):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if set(data) - {"repository", "pipeline", "api_migrations", "responsibility_migrations"} or set(data.get("repository", {})) != {"source"}:
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
        if "responsibility_migrations" in data:
            settings["responsibility_migrations"] = tuple(ResponsibilityRecipe(
                **{**item, "source_modules": tuple(item["source_modules"]),
                   "behavioral_invariants": tuple(item["behavioral_invariants"]),
                   "functional_cases": tuple(item.get("functional_cases", [])),
                   "target_imports": tuple(item.get("target_imports", []))})
                for item in data["responsibility_migrations"])
        return cls(source=source, **settings)
