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
class RefactoringCandidate:
    """Source-neutral candidate; provenance stays private to the controller."""
    candidate_id: str
    repository: str
    base_commit: str
    source_type: str
    source_refs: tuple[dict, ...]
    refactoring_types: tuple[str, ...]
    affected_symbols: tuple[str, ...]
    affected_modules: tuple[str, ...]
    goal: str
    behavioral_invariants: tuple[str, ...]
    compatibility_requirements: tuple[str, ...]
    gold_source: str
    dependencies: tuple[str, ...] = ()
    evidence: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.source_type not in {"historical", "synthetic"}:
            raise ValueError("Invalid candidate source")
        if self.gold_source not in {"developer_patch", "generated_patch"}:
            raise ValueError("Invalid gold provenance")
        if len(self.base_commit) != 40 or not all(c in "0123456789abcdef" for c in self.base_commit.lower()):
            raise ValueError("Candidate base commit must be a full SHA")
        if not all((self.candidate_id, self.repository, self.refactoring_types, self.goal)):
            raise ValueError("Incomplete refactoring candidate")
        if self.source_type == "historical" and not self.source_refs:
            raise ValueError("Historical candidate needs source references")


@dataclass(frozen=True)
class RefactoringTaskSpec:
    candidate: RefactoringCandidate
    goal: str
    scope: tuple[str, ...]
    structural_requirements: tuple[str, ...]
    behavioral_invariants: tuple[str, ...]
    compatibility_requirements: tuple[str, ...]
    acceptance_strategy: str
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.goal.strip() or not self.scope or not self.acceptance_strategy:
            raise ValueError("Incomplete refactoring task")
        for path in self.scope:
            relative(path)


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
    subtasks: tuple[TaskSpec | RefactoringTaskSpec, ...]
    dependency_edges: tuple[tuple[str, str], ...]
    goal: str
    scope: tuple[str, ...]
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if len(self.subtasks) < 2:
            raise ValueError("Compound requires at least two subtasks")
        if not all(isinstance(task, (TaskSpec, RefactoringTaskSpec)) for task in self.subtasks):
            raise ValueError("Unsupported compound subtask")
        ids = [task.candidate.candidate_id for task in self.subtasks]
        if len(set(ids)) != len(ids) or not self.goal.strip():
            raise ValueError("Invalid compound task identity or goal")
        if set(self.scope) != set().union(*(set(task.scope) for task in self.subtasks)):
            raise ValueError("Compound scope must cover all subtasks")
        for path in self.scope:
            relative(path)
        if any(a not in ids or b not in ids or a == b for a, b in self.dependency_edges):
            raise ValueError("Invalid compound dependency edge")
        successors = {identity: [] for identity in ids}
        for before, after in self.dependency_edges:
            successors[before].append(after)
        visiting, visited = set(), set()
        def visit(identity):
            if identity in visiting:
                raise ValueError("Cyclic compound dependency")
            if identity in visited:
                return
            visiting.add(identity)
            for following in successors[identity]:
                visit(following)
            visiting.remove(identity)
            visited.add(identity)
        for identity in ids:
            visit(identity)


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
