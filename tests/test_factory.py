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
from rf_factory.rollout import prepare_rollout
from rf_factory.publish import publish_run
from rf_factory.candidates import filter_and_rank
from rf_factory.models import Candidate


FIXTURE = Path(__file__).resolve().parents[1] / "rf_factory/fixtures/api_migration_repo"
HARD_CONFIG = Path(__file__).resolve().parents[1] / "examples/harder-api-migration.toml"
SCRATCH = Path(__file__).resolve().parents[1] / ".rf-factory-work/test-tmp"
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


def test_candidate_filter_records_trivial_reasons():
    candidate = Candidate("one", "sample.py", "sample", 1, 2, "hash", "one use", "api-migration-v2",
                          {"caller_modules": 1, "potential_files": 2, "behavioral_invariants": 0,
                           "changes_calling_convention": False})
    selected, rejected = filter_and_rank([candidate], Config(FIXTURE))
    assert selected == []
    assert set(rejected[0]["reasons"]) == {"too few caller modules", "too few changed files",
                                          "no behavioral constraints", "mechanical rename only"}


def test_harder_batch_and_solver_export():
    with workspace_context() as root:
        config = Config.load(HARD_CONFIG)
        summary = Pipeline().run(config, root / "batch")
        assert (summary["candidates"], summary["gold_pass"], summary["noop_fail"], summary["exported"]) == (5, 5, 5, 5)
        rows = [json.loads(line) for line in (root / "batch/instances.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len({row["instance_id"] for row in rows}) == 5
        assert all(row["difficulty_metadata"]["affected_file_count"] >= 5 for row in rows)
        assert all(row["difficulty_metadata"]["dependency_depth"] >= 3 for row in rows)
        assert all(row["precheck"]["gold"]["passed"] and not row["precheck"]["noop"]["passed"] for row in rows)
        assert all(row["problem_statement"]["quality"]["passed"] for row in rows)
        chosen = rows[0]
        candidate_id = chosen["task_spec"]["candidate"]["candidate_id"]
        gold = root / "batch/gold" / candidate_id / "workspace"
        caller = next(iter(chosen["task_spec"]["metadata"]["call_counts"]))
        shutil.copy2(root / "batch/source" / caller, gold / caller)
        verifier_spec = json.loads((root / "batch" / chosen["verifier"]["spec"]).read_text(encoding="utf-8"))
        assert not Evaluator().evaluate(verify(gold, verifier_spec)).passed
        exported = prepare_rollout(root / "batch", rows[0]["instance_id"], root / "solver")
        solver = Path(exported["output"])
        assert {path.name for path in solver.iterdir()} == {"repository", "task.md", "environment.json"}
        published = publish_run(root / "batch", root / "published")
        assert {path.name for path in published} == {row["instance_id"] for row in rows}
        for path in published:
            only_row = json.loads((path / "instances.jsonl").read_text(encoding="utf-8"))
            cid = only_row["task_spec"]["candidate"]["candidate_id"]
            assert (path / only_row["gold_patch"]["artifact"]).is_file()
            assert {child.name for child in (path / "tasks").iterdir()} == {cid}
            assert json.loads((path / "repository.json").read_text(encoding="utf-8"))["snapshot"] == (
                "../_bases/" + chosen["repository"]["content_sha256"] + "/source")
            assert not (path / "source").exists()
            assert not (path / "gold" / cid / "workspace").exists()
            assert str(path / "baseline" / "tests.json") in json.loads((path / "baseline" / "result.json").read_text(encoding="utf-8"))["argv"]
            assert len(json.loads((path / "instances.json").read_text(encoding="utf-8"))) == 1
            assert prepare_rollout(path, only_row["instance_id"], root / ("solver-" + cid))["instance_id"] == only_row["instance_id"]
        assert (solver / "task.md").read_text(encoding="utf-8").strip() == rows[0]["problem_statement"]["text"]
        assert not any((solver / name).exists() for name in ("gold", "tasks", "oracle.json", "precheck"))
        with pytest.raises(FileExistsError):
            prepare_rollout(root / "batch", rows[0]["instance_id"], solver)
