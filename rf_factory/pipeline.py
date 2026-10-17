"""The single repository-to-instance synthesis pipeline."""
import json
import platform
import shutil
import subprocess
from pathlib import Path

from .common import FactoryError, export_jsonl, write_json
from .api_migration import ApiMigrationDiscovery, ApiMigrationSpecBuilder, ApiMigrationGoldGenerator, PatchApplier, render_patch
from .repository import RepositoryPreparer, IGNORED
from .candidates import filter_and_rank
from .oracle import BaselineOracle, OracleBuilder
from .problem_generation import ProblemGenerator
from .verifier import verify
from .evaluator import Evaluator
from .instance_builder import InstanceBuilder


class Pipeline:
    def __init__(self, *, discovery=None, spec_builder=None, generator=None, applier=None,
                 problem_generator=None, oracle_builder=None, evaluator=None, instance_builder=None):
        self.discovery = discovery or ApiMigrationDiscovery()
        self.spec_builder = spec_builder or ApiMigrationSpecBuilder()
        self.generator = generator or ApiMigrationGoldGenerator()
        self.applier = applier or PatchApplier()
        self.problem_generator = problem_generator or ProblemGenerator()
        self.oracle_builder = oracle_builder or OracleBuilder()
        self.evaluator = evaluator or Evaluator()
        self.instance_builder = instance_builder or InstanceBuilder()

    def run(self, config, output: Path):
        output = output.resolve()
        if output.is_relative_to(config.source.resolve()):
            raise FactoryError("Output must be outside the source repository")
        output.mkdir(parents=True, exist_ok=False)
        stage = "repository"
        try:
            write_json(output / "config.json", config)
            write_json(output / "runtime.json", {"python": platform.python_version(), "factory": "0.3.0"})
            repo = RepositoryPreparer().prepare(config.source, output / "source")
            write_json(output / "repository.json", repo)
            stage = "discovery"
            candidates, diagnostics = self.discovery.discover(repo, config)
            write_json(output / "candidates.json", candidates)
            write_json(output / "discovery_diagnostics.json", diagnostics)
            stage = "candidate_filtering"
            candidates = filter_and_rank(candidates, config)
            write_json(output / "selected_candidates.json", candidates)
            stage = "baseline"
            baseline_oracle = BaselineOracle()
            baseline_plan = baseline_oracle.prepare(repo, config)
            write_json(output / "baseline" / "plan.json", baseline_plan)
            baseline = baseline_oracle.run(repo, baseline_plan, output / "baseline")
            specs, patches, prechecks, instances, rejections = [], [], [], [], []
            for candidate in candidates:
                try:
                    if not baseline["passed"]:
                        raise FactoryError("Original regression tests must pass")
                    stage = "specification"
                    spec = self.spec_builder.build(repo, candidate, config)
                    specs.append(spec)
                    task_dir = output / "tasks" / candidate.candidate_id
                    write_json(task_dir / "spec.json", spec)
                    stage = "gold_generation"
                    patch = self.generator.generate(repo, spec)
                    gold_dir = output / "gold" / candidate.candidate_id
                    gold_dir.mkdir(parents=True)
                    (gold_dir / "gold.patch").write_text(render_patch(patch.changes), encoding="utf-8", newline="\n")
                    patch_record = {"candidate_id": candidate.candidate_id, "patch_sha256": patch.patch_sha256,
                                    "modified_files": [change.path for change in patch.changes],
                                    "line_changes": {change.path: {"added": change.added_lines, "deleted": change.deleted_lines} for change in patch.changes},
                                    "transformation": patch.transformation,
                                    "artifact": f"gold/{candidate.candidate_id}/gold.patch"}
                    write_json(gold_dir / "patch.json", patch_record)
                    patches.append(patch_record)
                    stage = "gold_application"
                    gold_workspace = self.applier.apply(repo, patch, gold_dir / "workspace")
                    write_json(gold_dir / "application.json", {"passed": True, "workspace": f"gold/{candidate.candidate_id}/workspace"})
                    stage = "problem_generation"
                    problem = self.problem_generator.generate(spec, config)
                    write_json(task_dir / "problem_statement.json", problem)
                    stage = "oracle"
                    oracle = self.oracle_builder.build(repo, spec, config)
                    write_json(task_dir / "oracle.json", oracle)
                    verifier_dir = output / "verifier" / candidate.candidate_id
                    verifier_dir.mkdir(parents=True)
                    shutil.copy2(Path(__file__).with_name("verifier.py"), verifier_dir / "runner.py")
                    shutil.copy2(Path(__file__).with_name("test_runner.py"), verifier_dir / "test_runner.py")
                    write_json(verifier_dir / "spec.json", self.oracle_builder.verifier_spec(oracle))
                    stage = "verification"
                    noop_workspace = output / "precheck" / candidate.candidate_id / "noop_workspace"
                    shutil.copytree(repo.snapshot, noop_workspace, ignore=lambda _, names: set(names) & IGNORED)
                    verifier_spec = json.loads((verifier_dir / "spec.json").read_text(encoding="utf-8"))
                    gold_report = verify(gold_workspace, verifier_spec)
                    noop_report = verify(noop_workspace, verifier_spec)
                    stage = "evaluation"
                    gold_result = self.evaluator.evaluate(gold_report)
                    noop_result = self.evaluator.evaluate(noop_report)
                    precheck = self.evaluator.precheck(gold_result, noop_result)
                    check_dir = output / "precheck" / candidate.candidate_id
                    write_json(check_dir / "gold.json", gold_result)
                    write_json(check_dir / "noop.json", noop_result)
                    write_json(check_dir / "result.json", precheck)
                    prechecks.append({"candidate_id": candidate.candidate_id, "passed": precheck.passed,
                                      "gold_passed": gold_result.passed, "noop_failed": not noop_result.passed})
                    if not precheck.passed:
                        raise FactoryError("Gold PASS / no-op FAIL precheck did not hold")
                    stage = "instance_building"
                    verifier_record = {"runner": f"verifier/{candidate.candidate_id}/runner.py",
                                       "spec": f"verifier/{candidate.candidate_id}/spec.json",
                                       "command": f"python verifier/{candidate.candidate_id}/runner.py <workspace> verifier/{candidate.candidate_id}/spec.json"}
                    instances.append(self.instance_builder.build(repo, spec, problem, oracle, patch_record, verifier_record, precheck))
                except (FactoryError, OSError, ValueError, SyntaxError) as exc:
                    rejections.append({"candidate_id": candidate.candidate_id, "stage": stage, "error": str(exc)})
                    if stage == "gold_application":
                        write_json(output / "gold" / candidate.candidate_id / "application.json", {"passed": False, "error": str(exc)})
            write_json(output / "task_specs.json", specs)
            write_json(output / "gold" / "patches.json", patches)
            write_json(output / "precheck" / "results.json", prechecks)
            write_json(output / "rejections.json", rejections)
            stage = "docker"
            docker = self._docker(output, instances)
            write_json(output / "docker.json", docker)
            stage = "export"
            write_json(output / "instances.json", instances)
            export_jsonl(output / "instances.jsonl", instances)
            summary = {"status": "completed" if instances else "no_valid_instances", "candidates": len(candidates),
                       "gold_patch_generated": len(patches), "gold_patch_success": len([p for p in patches if (output / "gold" / p["candidate_id"] / "workspace").exists()]),
                       "gold_pass": sum(x["gold_passed"] for x in prechecks), "noop_fail": sum(x["noop_failed"] for x in prechecks),
                       "exported": len(instances), "rejected": len(rejections), "docker": docker["status"],
                       "output": str(output / "instances.jsonl")}
            write_json(output / "summary.json", summary)
            return summary
        except Exception as exc:
            write_json(output / "summary.json", {"status": "failed", "stage": stage, "error": str(exc)})
            raise

    @staticmethod
    def _docker(output, instances):
        dockerfile = output / "Dockerfile"
        (output / ".dockerignore").write_text("*\n!Dockerfile\n!source/\n!source/**\n!verifier/\n!verifier/**\n", encoding="utf-8")
        if not instances:
            dockerfile.write_text("FROM python:3.11-slim\nWORKDIR /work\nCOPY source/ /work/\n", encoding="utf-8")
            return {"status": "not_run", "reason": "no valid instance"}
        cid = instances[0].task_spec.candidate.candidate_id
        dockerfile.write_text("FROM python:3.11-slim\nWORKDIR /work\nCOPY source/ /work/\n"
                              f"COPY verifier/{cid}/ /verifier/\n"
                              'CMD ["python", "/verifier/runner.py", "/work", "/verifier/spec.json"]\n', encoding="utf-8")
        image = "rf-factory:" + instances[0].instance_id
        commands = {"build": f"docker build -t {image} -f Dockerfile .", "run_base_expected_fail": f"docker run --rm {image}"}
        try:
            version = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True, timeout=15)
            if version.returncode:
                return {"status": "not_available", "reason": version.stderr.strip() or version.stdout.strip(), "commands": commands}
            build = subprocess.run(["docker", "build", "-t", image, "-f", str(dockerfile), str(output)], capture_output=True, text=True, timeout=180)
            (output / "docker-build.stdout.txt").write_text(build.stdout, encoding="utf-8")
            (output / "docker-build.stderr.txt").write_text(build.stderr, encoding="utf-8")
            if build.returncode:
                return {"status": "build_failed", "returncode": build.returncode, "commands": commands}
            run = subprocess.run(["docker", "run", "--rm", image], capture_output=True, text=True, timeout=60)
            (output / "docker-run.stdout.txt").write_text(run.stdout, encoding="utf-8")
            (output / "docker-run.stderr.txt").write_text(run.stderr, encoding="utf-8")
            result = json.loads(run.stdout) if run.stdout.strip() else {}
            passed = run.returncode == 1 and result.get("checks") and not all(result["checks"].values())
            return {"status": "built_and_ran" if passed else "run_failed", "base_verifier_expected_fail": bool(passed),
                    "image": image, "commands": commands}
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return {"status": "not_available", "reason": str(exc), "commands": commands}
