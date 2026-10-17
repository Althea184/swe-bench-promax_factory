import json
import shutil
from dataclasses import replace
from pathlib import Path
from contextlib import contextmanager
from uuid import uuid4

import pytest

from rf_factory.api_migration import ApiMigrationDiscovery, ApiMigrationSpecBuilder, ApiMigrationGoldGenerator, PatchApplier, render_patch
from rf_factory.common import FactoryError
from rf_factory.config import Config
from rf_factory.evaluator import Evaluator
from rf_factory.oracle import OracleBuilder
from rf_factory.pipeline import Pipeline
from rf_factory.problem_generation import ProblemGenerator
from rf_factory.repository import RepositoryPreparer, manifest
from rf_factory.verifier import verify


FIXTURE = Path(__file__).resolve().parents[1] / "rf_factory/fixtures/api_migration_repo"
SCRATCH = Path(__file__).resolve().parents[1] / "runs/test-tmp"
SCRATCH.mkdir(parents=True, exist_ok=True)


@contextmanager
def workspace_context():
    root = SCRATCH / ("case-" + uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.fixture
def prepared():
    with workspace_context() as root:
        config = Config(FIXTURE)
        repo = RepositoryPreparer().prepare(FIXTURE, root / "source")
        candidates, diagnostics = ApiMigrationDiscovery().discover(repo, config)
        assert not diagnostics and len(candidates) == 1
        spec = ApiMigrationSpecBuilder().build(repo, candidates[0], config)
        yield root, config, repo, candidates[0], spec


def test_spec_and_problem_have_separate_audiences(prepared):
    _, config, _, candidate, spec = prepared
    assert candidate.path == "money.py"
    assert spec.refactoring_type == "api_migration"
    assert spec.metadata["call_counts"] == {"invoice.py": 2, "quote.py": 1}
    problem = ProblemGenerator().generate(spec, config)
    assert problem.abstraction_level == "medium"
    assert "amount-formatting" in problem.text
    assert "formatted output" in problem.text
    assert all(path not in problem.text for path in spec.scope)
    assert "format_amount" not in problem.text
    assert "render_amount" not in problem.text
    assert ProblemGenerator.check("Improve architecture.", spec)["passed"] is False
    assert ProblemGenerator.check("First edit money.py, then replace render_amount with format_amount.", spec)["passed"] is False


def test_gold_patch_and_alternative_solution(prepared):
    root, config, repo, _, spec = prepared
    patch = ApiMigrationGoldGenerator().generate(repo, spec)
    assert {change.path for change in patch.changes} == set(spec.scope)
    assert "+def format_amount(amount, *, currency):" in render_patch(patch.changes)
    before = manifest(repo.snapshot)
    gold = PatchApplier().apply(repo, patch, root / "gold")
    assert manifest(repo.snapshot) == before
    oracle = OracleBuilder().build(repo, spec, config)
    verifier_spec = OracleBuilder.verifier_spec(oracle)
    evaluator = Evaluator()
    gold_result = evaluator.evaluate(verify(gold, verifier_spec))
    noop_result = evaluator.evaluate(verify(repo.snapshot, verifier_spec))
    assert gold_result.passed and not noop_result.passed
    assert evaluator.precheck(gold_result, noop_result).passed
    assert noop_result.checks["regression_tests"]
    # A different correct implementation is accepted; the verifier never compares to gold bytes.
    money = gold / "money.py"
    money.write_text(money.read_text(encoding="utf-8") + "\n# independent implementation note\n", encoding="utf-8")
    assert evaluator.evaluate(verify(gold, verifier_spec)).passed


def test_crlf_gold_patch(prepared):
    root, _, _, _, _ = prepared
    source = root / "crlf"
    shutil.copytree(FIXTURE, source)
    for path in source.rglob("*.py"):
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    repo = RepositoryPreparer().prepare(source, root / "crlf_snapshot")
    config = Config(source)
    candidate = ApiMigrationDiscovery().discover(repo, config)[0][0]
    spec = ApiMigrationSpecBuilder().build(repo, candidate, config)
    patch = ApiMigrationGoldGenerator().generate(repo, spec)
    gold = PatchApplier().apply(repo, patch, root / "crlf_gold")
    assert all(b"\r\n" in (gold / path).read_bytes() for path in spec.scope)


def test_invalid_patch_rejected_before_application(prepared):
    root, _, repo, _, spec = prepared
    patch = ApiMigrationGoldGenerator().generate(repo, spec)
    broken = replace(patch, changes=(replace(patch.changes[0], before_sha256="0" * 64), *patch.changes[1:]))
    with pytest.raises(FactoryError):
        PatchApplier().apply(repo, broken, root / "bad")
    assert not (root / "bad").exists()


def test_full_pipeline_artifacts_and_stable_id(prepared):
    root, config, _, _, _ = prepared
    before = manifest(FIXTURE)
    output = root / "run"
    summary = Pipeline().run(config, output)
    assert (summary["candidates"], summary["gold_pass"], summary["noop_fail"], summary["exported"]) == (1, 1, 1, 1)
    row = json.loads((output / "instances.jsonl").read_text(encoding="utf-8"))
    assert row["schema_version"] == "rf.instance.v3"
    assert row["precheck"]["gold"]["passed"]
    assert not row["precheck"]["noop"]["passed"]
    assert (output / row["gold_patch"]["artifact"]).is_file()
    assert (output / row["verifier"]["runner"]).is_file()
    assert (output / "tasks" / row["task_spec"]["candidate"]["candidate_id"] / "problem_statement.json").is_file()
    assert manifest(FIXTURE) == before
    second = root / "second"
    Pipeline().run(config, second)
    assert row["instance_id"] == json.loads((second / "instances.jsonl").read_text(encoding="utf-8"))["instance_id"]
    with pytest.raises(FileExistsError):
        Pipeline().run(config, output)


def test_missing_source_records_failure():
    with workspace_context() as root:
        with pytest.raises(FactoryError):
            Pipeline().run(Config(root / "missing"), root / "run")
        assert json.loads((root / "run/summary.json").read_text(encoding="utf-8"))["stage"] == "repository"


def test_output_inside_source_rejected():
    with workspace_context() as root:
        source = root / "source"
        shutil.copytree(FIXTURE, source)
        with pytest.raises(FactoryError):
            Pipeline().run(Config(source), source / "run")
        assert not (source / "run").exists()
