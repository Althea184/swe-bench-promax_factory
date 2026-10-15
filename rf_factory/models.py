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
