"""Data passed between synthesis stages. Paths in specs are repository relative."""
from dataclasses import dataclass, field
from pathlib import Path
from .common import relative


@dataclass(frozen=True)
class Repository:
    source: str
    snapshot: Path | str
    content_sha256: str
    files: dict[str, str]


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    path: str
    symbol: str
    start_line: int
    end_line: int
    source_sha256: str
    reason: str
    strategy: str
    metrics: dict = field(default_factory=dict)

    def __post_init__(self):
        relative(self.path)
        if not all((self.candidate_id, self.symbol, self.source_sha256, self.reason, self.strategy)):
            raise ValueError("Candidate fields must be nonempty")
        if type(self.start_line) is not int or type(self.end_line) is not int or not 1 <= self.start_line <= self.end_line:
            raise ValueError("Invalid candidate line range")


@dataclass(frozen=True)
class TaskSpec:
    """Internal task contract; strategy details stay in metadata."""
    candidate: Candidate
    refactoring_type: str
    goal: str
    scope: tuple[str, ...]
    entities: dict
    structural_requirements: tuple[str, ...]
    behavioral_invariants: tuple[str, ...]
    compatibility_requirements: tuple[str, ...]
    metadata: dict

    def __post_init__(self):
        if not self.refactoring_type or not self.goal or not self.scope or not self.behavioral_invariants:
            raise ValueError("Incomplete task specification")
        for path in self.scope:
            relative(path)
        if len(set(self.scope)) != len(self.scope):
            raise ValueError("Duplicate scope path")


@dataclass(frozen=True)
class CompoundTaskSpec:
    """Private contract for one instance composed from ordered task contracts."""
    subtasks: tuple[TaskSpec, ...]
    dependency_edges: tuple[tuple[str, str], ...]
    goal: str
    scope: tuple[str, ...]
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if len(self.subtasks) != 2 or any(task.refactoring_type != "api_migration" for task in self.subtasks):
            raise ValueError("Compound MVP requires exactly two API migrations")
        ids = [task.candidate.candidate_id for task in self.subtasks]
        if len(set(ids)) != 2 or not self.goal.strip():
            raise ValueError("Invalid compound task identity or goal")
        if set(self.scope) != set().union(*(set(task.scope) for task in self.subtasks)):
            raise ValueError("Compound scope must cover both subtasks")
        for path in self.scope:
            relative(path)
        if any(a not in ids or b not in ids or a == b for a, b in self.dependency_edges):
            raise ValueError("Invalid compound dependency edge")


@dataclass(frozen=True)
class ProblemStatement:
    text: str
    abstraction_level: str
    generator: str
    quality: dict


@dataclass(frozen=True)
class FileChange:
    path: str
    before_sha256: str
    after_sha256: str
    before: str
    after: str
    added_lines: int
    deleted_lines: int


@dataclass(frozen=True)
class GoldPatch:
    changes: tuple[FileChange, ...]
    patch_sha256: str
    transformation: dict


@dataclass(frozen=True)
class Oracle:
    """Portable acceptance conditions consumed by the verifier."""
    strategy: str
    checks: dict
    test_dir: str
    test_pattern: str
    timeout: float


@dataclass(frozen=True)
class Evaluation:
    passed: bool
    score: float
    checks: dict[str, bool]
    diagnostics: dict


@dataclass(frozen=True)
class PrecheckResult:
    gold: Evaluation
    noop: Evaluation
    passed: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Instance:
    instance_id: str
    repository: Repository
    task_spec: TaskSpec
    problem_statement: ProblemStatement
    oracle: Oracle
    gold_patch: dict
    verifier: dict
    precheck: PrecheckResult
    environment: dict
    difficulty_metadata: dict = field(default_factory=dict)
    schema_version: str = "rf.instance.v3"
    status: str = "precheck_passed"

    def __post_init__(self):
        if not self.precheck.passed or not self.precheck.gold.passed or self.precheck.noop.passed:
            raise ValueError("Instance requires gold PASS and no-op FAIL")
