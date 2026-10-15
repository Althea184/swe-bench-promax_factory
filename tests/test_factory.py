import json
import shutil
import tempfile
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

FIXTURE = Path(__file__).resolve().parents[1] / "rf_factory/fixtures/tiny_repo"


class FactoryTests(unittest.TestCase):
    def setUp(self):
        scratch = FIXTURE.parents[2] / "runs" / "test-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = Config(FIXTURE)

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
