"""Versioned, explicit stage contracts. Coordinates are inclusive, one-based."""
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
    strategy: str = "long-function-v1"

    def __post_init__(self):
        relative(self.path)
        if not all((self.candidate_id, self.symbol, self.source_sha256, self.reason)):
            raise ValueError("Candidate fields must be nonempty")
        if type(self.start_line) is not int or type(self.end_line) is not int or not 1 <= self.start_line <= self.end_line:
            raise ValueError("Invalid candidate line range")


@dataclass(frozen=True)
class Task:
    candidate: Candidate
    problem_statement: str
    requirements: tuple[str, ...]
    synthesis_method: str = "extract-helper-template-v1"
    template_sha256: str = ""


@dataclass(frozen=True)
class OraclePlan:
    test_dir: str
    pattern: str
    timeout: float
    mode: str = "baseline-unittest"


@dataclass
class Validation:
    passed: bool
    checks: dict[str, bool]
    baseline: dict
    behavioral_equivalence: str = "not_proven"
    limitations: list[str] = field(default_factory=lambda: [
        "Only original repository tests executed; no refactored solution generated or tested.",
        "Test coverage and extraction feasibility require review; baseline passing is not equivalence.",
    ])


@dataclass(frozen=True)
class Instance:
    instance_id: str
    repository: Repository
    task: Task
    oracle: OraclePlan
    validation: Validation
    schema_version: str = "rf.instance.v1"
    task_type: str = "refactoring"
    status: str = "mvp_validated"

    def __post_init__(self):
        if not self.instance_id or not self.task.problem_statement.strip() or not self.task.requirements or not self.validation.passed:
            raise ValueError("Cannot package an incomplete or rejected task")


@dataclass(frozen=True)
class RefactoringPlan:
    candidate: Candidate
    refactoring_type: str
    api_module: str
    old_api: str
    new_api: str
    affected_files: tuple[str, ...]
    call_counts: dict[str, int]
    structural_requirements: tuple[str, ...]
    behavioral_invariants: tuple[str, ...]
    strategy: str = "python-api-migration-v1"

    def __post_init__(self):
        relative(self.api_module)
        for path in self.affected_files:
            relative(path)
        if len(self.affected_files) < 3 or not self.old_api.isidentifier() or not self.new_api.isidentifier():
            raise ValueError("API migration requires an API and at least two caller files")
        if self.old_api == self.new_api or self.api_module not in self.affected_files:
            raise ValueError("Invalid API transformation")
        if set(self.call_counts) != set(self.affected_files) - {self.api_module} or not all(n > 0 for n in self.call_counts.values()):
            raise ValueError("Each caller must contain a migration target")


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
    artifact: str = "gold/gold.patch"


@dataclass(frozen=True)
class PrecheckResult:
    oracle: dict
    noop: dict
    passed: bool


@dataclass(frozen=True)
class PilotInstance:
    instance_id: str
    repository: Repository
    task: Task
    plan: RefactoringPlan
    gold_patch: dict
    evaluator: dict
    precheck: PrecheckResult
    environment: dict
    schema_version: str = "rf.instance.v2"
    task_type: str = "multi_file_api_migration"
    status: str = "precheck_passed"

    def __post_init__(self):
        if not self.precheck.passed or not self.precheck.oracle.get("passed") or self.precheck.noop.get("passed"):
            raise ValueError("Pilot instance requires gold PASS and no-op FAIL")
