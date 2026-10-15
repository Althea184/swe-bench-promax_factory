import json
import shutil
import uuid
import unittest
from dataclasses import replace
from pathlib import Path
from rf_factory.common import FactoryError, dumps, export_jsonl
from rf_factory.config import Config
from rf_factory.discovery import LongFunctionDiscovery
from rf_factory.models import Candidate
from rf_factory.pipeline import Pipeline
from rf_factory.repository import RepositoryPreparer, manifest
from rf_factory.synthesis import TemplateSynthesizer
from rf_factory.validation import TaskValidator
from rf_factory.migration import ApiMigrationDiscovery, ApiMigrationPlanner, ApiMigrationGoldGenerator, PatchApplier, render_patch
from rf_factory.evaluator_runner import evaluate
from rf_factory.pilot import PilotPipeline
from rf_factory.models import PrecheckResult

FIXTURE = Path(__file__).resolve().parents[1] / "rf_factory/fixtures/tiny_repo"
PILOT_FIXTURE = FIXTURE.parent / "api_migration_repo"


class FactoryTests(unittest.TestCase):
    def setUp(self):
        scratch = FIXTURE.parents[2] / "runs" / "test-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.root = scratch / ("case-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup_root)
        self.config = Config(FIXTURE)

    def cleanup_root(self):
        if not self.root.resolve().is_relative_to((FIXTURE.parents[2] / "runs" / "test-tmp").resolve()):
            raise RuntimeError("Unsafe test cleanup path")
        shutil.rmtree(self.root)

    def prepare(self):
        repo = RepositoryPreparer().prepare(FIXTURE, self.root / "source")
        candidates, _ = LongFunctionDiscovery().discover(repo, self.config)
        return repo, TemplateSynthesizer().synthesize(candidates[0])

    def test_candidate_contract(self):
        valid = Candidate("id", "a.py", "foo", 1, 12, "abc", "long")
        self.assertEqual(json.loads(dumps(valid))["symbol"], "foo")
        for changes in ({"start_line": 0}, {"end_line": 0}, {"symbol": ""}, {"path": "../x.py"}, {"path": "C:/x.py"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(valid, **changes)

    def test_discovery_and_synthesis(self):
        repo, task = self.prepare()
        self.assertEqual(task.candidate.symbol, "invoice_total")
        self.assertIn("private helper", task.problem_statement)
        self.assertEqual(LongFunctionDiscovery().discover(repo, replace(self.config, min_function_lines=100))[0], [])

    def test_validation_rejects_stale_missing_and_incomplete(self):
        repo, task = self.prepare()
        validator = TaskValidator()
        baseline = {"passed": True, "status": "passed"}
        self.assertTrue(validator.validate(repo, task, baseline, self.config).passed)
        for broken in (replace(task, problem_statement=""), replace(task, candidate=replace(task.candidate, symbol="missing")), replace(task, candidate=replace(task.candidate, path="missing.py"))):
            self.assertFalse(validator.validate(repo, broken, baseline, self.config).passed)
        (repo.snapshot / "invoice.py").write_text("# changed", encoding="utf-8")
        self.assertFalse(validator.validate(repo, task, baseline, self.config).passed)

    def test_jsonl_unicode_and_no_overwrite(self):
        path = self.root / "out.jsonl"
        rows = [{"text": "重构\n任务"}, {"id": 2}]
        export_jsonl(path, rows)
        self.assertEqual([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()], rows)
        with self.assertRaises(FileExistsError):
            export_jsonl(path, [])

    def test_end_to_end_and_serialization(self):
        before = manifest(FIXTURE)
        output = self.root / "run with spaces"
        self.assertEqual(Pipeline().run(self.config, output)["exported"], 1)
        row = json.loads((output / "instances.jsonl").read_text(encoding="utf-8"))
        self.assertEqual([row], json.loads((output / "instances.json").read_text(encoding="utf-8")))
        self.assertEqual(row["validation"]["baseline"]["counts"]["tests_run"], 4)
        self.assertEqual(row["validation"]["behavioral_equivalence"], "not_proven")
        self.assertEqual(manifest(FIXTURE), before)
        second = self.root / "second"
        Pipeline().run(self.config, second)
        self.assertEqual(row["instance_id"], json.loads((second / "instances.jsonl").read_text(encoding="utf-8"))["instance_id"])
        with self.assertRaises(FileExistsError):
            Pipeline().run(self.config, output)

    def test_stage_replacement(self):
        class EmptyDiscovery:
            def discover(self, repo, config):
                return [], [{"reason": "injected strategy"}]
        output = self.root / "run"
        result = Pipeline(discovery=EmptyDiscovery()).run(self.config, output)
        self.assertEqual(result["status"], "no_valid_instances")
        self.assertEqual((output / "instances.jsonl").read_text(), "")

    def test_baseline_failure_skip_empty_and_timeout(self):
        for index, code in enumerate([
            "import unittest\nclass T(unittest.TestCase):\n def test_fail(self): self.fail('broken')\n",
            "import unittest\nclass T(unittest.TestCase):\n @unittest.skip('skip')\n def test_skip(self): pass\n",
            "# no tests\n", "import time\ntime.sleep(5)\n",
        ]):
            with self.subTest(index=index):
                source = self.root / f"input-{index}"
                shutil.copytree(FIXTURE, source)
                (source / "tests/test_invoice.py").write_text(code, encoding="utf-8")
                config = Config(source, test_timeout=0.2 if index == 3 else 10)
                output = self.root / f"run-{index}"
                self.assertEqual(Pipeline().run(config, output)["exported"], 0)
                self.assertTrue((output / "baseline/stderr.txt").exists())

    def test_missing_tests_optional_is_explicit(self):
        source = self.root / "input"
        source.mkdir()
        shutil.copy2(FIXTURE / "invoice.py", source)
        for required in (True, False):
            result = Pipeline().run(Config(source, require_tests=required), self.root / str(required))
            self.assertEqual(result["exported"], 0 if required else 1)

    def test_failed_stage_records_evidence(self):
        output = self.root / "failed"
        with self.assertRaises(FactoryError):
            Pipeline().run(Config(self.root / "missing"), output)
        report = json.loads((output / "summary.json").read_text())
        self.assertEqual(report["stage"], "preparation")
        self.assertEqual(report["status"], "failed")

    def test_output_inside_source_rejected_before_creation(self):
        source = self.root / "input"
        shutil.copytree(FIXTURE, source)
        with self.assertRaises(FactoryError):
            Pipeline().run(Config(source), source / "output")
        self.assertFalse((source / "output").exists())

    def test_config_relative_paths_and_invalid_options(self):
        path = self.root / "config.toml"
        path.write_text('[repository]\nsource="local repo"\n', encoding="utf-8")
        self.assertEqual(Config.load(path).source, self.root / "local repo")
        for changes in ({"max_candidates": 0}, {"test_timeout": float("nan")}, {"require_tests": "yes"}):
            with self.assertRaises(ValueError):
                replace(self.config, **changes)


class PilotTests(unittest.TestCase):
    def setUp(self):
        scratch = FIXTURE.parents[2] / "runs" / "test-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.root = scratch / ("case-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup_root)
        self.config = Config(PILOT_FIXTURE)

    def cleanup_root(self):
        if not self.root.resolve().is_relative_to((FIXTURE.parents[2] / "runs" / "test-tmp").resolve()):
            raise RuntimeError("Unsafe test cleanup path")
        shutil.rmtree(self.root)

    def prepare(self):
        repo = RepositoryPreparer().prepare(PILOT_FIXTURE, self.root / "source")
        candidates, _ = ApiMigrationDiscovery().discover(repo, self.config)
        self.assertEqual(len(candidates), 1)
        plan = ApiMigrationPlanner().plan(repo, candidates[0], self.config)
        return repo, plan

    def test_plan_gold_patch_and_application(self):
        repo, plan = self.prepare()
        self.assertEqual(plan.refactoring_type, "multi_file_api_migration")
        self.assertEqual(plan.call_counts, {"invoice.py": 2, "quote.py": 1})
        patch = ApiMigrationGoldGenerator().generate(repo, plan)
        self.assertEqual({c.path for c in patch.changes}, {"money.py", "invoice.py", "quote.py"})
        self.assertIn("+def format_amount(amount, *, currency):", render_patch(patch.changes))
        before = manifest(repo.snapshot)
        workspace = PatchApplier().apply(repo, patch, self.root / "gold")
        self.assertEqual(manifest(repo.snapshot), before)
        self.assertNotEqual(manifest(workspace), before)
        self.assertIn("currency='USD'", (workspace / "invoice.py").read_text(encoding="utf-8"))

    def test_invalid_patch_rejected_without_workspace(self):
        repo, plan = self.prepare()
        patch = ApiMigrationGoldGenerator().generate(repo, plan)
        bad = replace(patch, changes=(replace(patch.changes[0], before_sha256="0" * 64), *patch.changes[1:]))
        with self.assertRaises(FactoryError):
            PatchApplier().apply(repo, bad, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())

    def test_pipeline_rejects_invalid_gold(self):
        class BrokenGenerator:
            def generate(self, repo, plan):
                patch = ApiMigrationGoldGenerator().generate(repo, plan)
                return replace(patch, changes=(replace(patch.changes[0], before_sha256="0" * 64), *patch.changes[1:]))

        output = self.root / "broken-run"
        summary = PilotPipeline(generator=BrokenGenerator()).run(self.config, output)
        self.assertEqual(summary["exported"], 0)
        self.assertEqual(json.loads((output / "rejections.json").read_text(encoding="utf-8"))[0]["stage"], "gold_application")
        self.assertFalse(json.loads(next((output / "gold").glob("*/application.json")).read_text(encoding="utf-8"))["passed"])

    def test_evaluator_gold_and_noop(self):
        repo, plan = self.prepare()
        patch = ApiMigrationGoldGenerator().generate(repo, plan)
        gold = PatchApplier().apply(repo, patch, self.root / "gold")
        spec = {"api_module": plan.api_module, "old_api": plan.old_api, "new_api": plan.new_api,
                "call_counts": plan.call_counts, "base_hashes": {p: repo.files[p] for p in plan.affected_files},
                "test_dir": "tests", "test_pattern": "test*.py", "timeout": 20,
                "functional_inputs": [[12.5, "USD"], [0, "EUR"]], "functional_expected": ["$12.50", "€0.00"]}
        oracle = evaluate(gold, spec)
        noop = evaluate(repo.snapshot, spec)
        self.assertEqual(oracle["score"], 1.0)
        self.assertEqual(noop["score"], 0.0)
        self.assertTrue(noop["checks"]["regression_tests"])
        self.assertTrue(PrecheckResult(oracle, noop, True).passed)
        self.assertTrue(json.loads(dumps(PrecheckResult(oracle, noop, True)))["passed"])

    def test_end_to_end_pilot(self):
        before = manifest(PILOT_FIXTURE)
        output = self.root / "pilot"
        summary = PilotPipeline().run(self.config, output)
        self.assertEqual((summary["candidates"], summary["gold_patch_success"], summary["oracle_pass"],
                          summary["noop_fail"], summary["exported"]), (1, 1, 1, 1, 1))
        row = json.loads((output / "instances.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(row["schema_version"], "rf.instance.v2")
        self.assertEqual(row["precheck"]["oracle"]["score"], 1.0)
        self.assertEqual(row["precheck"]["noop"]["score"], 0.0)
        self.assertEqual(len(row["gold_patch"]["modified_files"]), 3)
        self.assertTrue((output / row["gold_patch"]["artifact"]).is_file())
        self.assertTrue((output / "Dockerfile").is_file())
        self.assertEqual(manifest(PILOT_FIXTURE), before)
