"""Baseline evidence preparation, not a solution or equivalence oracle."""
import os
import shutil
import subprocess
import sys
import json
from pathlib import Path
from dataclasses import dataclass
from .models import Oracle
from .common import write_json
from .repository import IGNORED


@dataclass(frozen=True)
class BaselinePlan:
    test_dir: str
    pattern: str
    timeout: float


class BaselineOracle:
    def prepare(self, repo, config):
        return BaselinePlan(config.test_dir, config.test_pattern, config.test_timeout)

    def run(self, repo, plan, output: Path) -> dict:
        output.mkdir(parents=True, exist_ok=True)
        if not (repo.snapshot / plan.test_dir).is_dir():
            result = {"status": "not_available", "passed": False}
            write_json(output / "result.json", result)
            return result
        workspace = output / "workspace"
        shutil.copytree(repo.snapshot, workspace, ignore=lambda _, names: set(names) & IGNORED)
        report = output / "tests.json"
        argv = [sys.executable, "-B", str(Path(__file__).with_name("test_runner.py").resolve()),
                plan.test_dir, plan.pattern, str(report.resolve())]
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
        result = {"argv": argv, "execution": "host", "isolated": False, "passed": False}
        try:
            process = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, timeout=plan.timeout)
            (output / "stdout.txt").write_bytes(process.stdout)
            (output / "stderr.txt").write_bytes(process.stderr)
            counts = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
            passed = process.returncode == 0 and counts.get("tests_run", 0) > 0 and all(
                counts.get(k) == 0 for k in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"))
            result.update(status="passed" if passed else "failed", passed=passed, returncode=process.returncode, counts=counts)
        except subprocess.TimeoutExpired as exc:
            (output / "stdout.txt").write_bytes(exc.stdout or b"")
            (output / "stderr.txt").write_bytes(exc.stderr or b"")
            result.update(status="timeout")
        except (OSError, ValueError) as exc:
            result.update(status="execution_error", error=str(exc))
        write_json(output / "result.json", result)
        return result


class OracleBuilder:
    """Build acceptance conditions from the task spec and base snapshot."""
    def build(self, repo, spec, config):
        if spec.refactoring_type == "module_responsibility_migration":
            checks = {"strategy": "module-responsibility-v1", **spec.entities,
                      "call_counts": spec.metadata["call_counts"],
                      "base_hashes": {path: repo.files[path] for path in spec.scope},
                      "functional_cases": spec.metadata["functional_cases"],
                      "module": spec.entities["target_import"], "new_api": spec.entities["symbol"]}
            return Oracle("module-responsibility-v1", checks, config.test_dir, config.test_pattern, config.test_timeout)
        if spec.refactoring_type != "api_migration":
            raise ValueError(f"Unsupported oracle strategy: {spec.refactoring_type}")
        checks = {"api_module": spec.entities["api_module"],
                  "module": spec.entities["module"],
                  "old_api": spec.entities["current_api"],
                  "new_api": spec.entities["target_api"],
                  "keyword_parameter": spec.entities["keyword_parameter"],
                  "call_counts": spec.metadata["call_counts"],
                  "base_hashes": {path: repo.files[path] for path in spec.scope},
                  "functional_cases": spec.metadata["functional_cases"]}
        return Oracle("python-api-migration-v1", checks, config.test_dir, config.test_pattern, config.test_timeout)

    @staticmethod
    def verifier_spec(oracle):
        return {**oracle.checks, "test_dir": oracle.test_dir,
                "test_pattern": oracle.test_pattern, "timeout": oracle.timeout}
