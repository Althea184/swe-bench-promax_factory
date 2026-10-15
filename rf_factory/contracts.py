"""Structural stage interfaces: alternative research strategies need no inheritance."""
from pathlib import Path
from typing import Protocol
from .config import Config
from .models import Candidate, GoldPatch, Instance, OraclePlan, RefactoringPlan, Repository, Task, Validation


class Preparer(Protocol):
    def prepare(self, source: Path, destination: Path) -> Repository: ...


class Discovery(Protocol):
    def discover(self, repo: Repository, config: Config) -> tuple[list[Candidate], list[dict]]: ...


class Synthesizer(Protocol):
    def synthesize(self, candidate: Candidate) -> Task: ...


class Oracle(Protocol):
    def prepare(self, repo: Repository, config: Config) -> OraclePlan: ...
    def run(self, repo: Repository, plan: OraclePlan, output: Path) -> dict: ...


class Validator(Protocol):
    def validate(self, repo: Repository, task: Task, baseline: dict, config: Config) -> Validation: ...


class Packager(Protocol):
    def package(self, repo: Repository, task: Task, plan: OraclePlan, validation: Validation) -> Instance: ...


class RefactoringPlanner(Protocol):
    def plan(self, repo: Repository, candidate: Candidate, config: Config) -> RefactoringPlan: ...


class GoldGenerator(Protocol):
    def generate(self, repo: Repository, plan: RefactoringPlan) -> GoldPatch: ...


class GoldApplier(Protocol):
    def apply(self, repo: Repository, patch: GoldPatch, workspace: Path) -> Path: ...
