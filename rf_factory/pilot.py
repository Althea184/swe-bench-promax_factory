"""Incremental synthesis/precheck pipeline; the original MVP Pipeline stays intact."""
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .common import FactoryError, digest, dumps, export_jsonl, write_json
from .evaluator_runner import evaluate
from .migration import ApiMigrationDiscovery, ApiMigrationPlanner, ApiMigrationGoldGenerator, PatchApplier, render_patch
from .models import PilotInstance, PrecheckResult, Repository, Task
from .oracle import BaselineOracle
from .repository import RepositoryPreparer, IGNORED


class PilotPipeline:
    def __init__(self, *, discovery=None, planner=None, generator=None, applier=None):
        self.discovery = discovery or ApiMigrationDiscovery()
        self.planner = planner or ApiMigrationPlanner()
        self.generator = generator or ApiMigrationGoldGenerator()
        self.applier = applier or PatchApplier()

    def run(self, config, output: Path):
        output = output.resolve()
        if output.is_relative_to(config.source.resolve()):
            raise FactoryError("Output must be outside the source repository")
        output.mkdir(parents=True, exist_ok=False)
        stage = "preparation"
        try:
            write_json(output / "config.json", config)
            write_json(output / "runtime.json", {"python": platform.python_version(), "factory": "0.2.0"})
            repo = RepositoryPreparer().prepare(config.source, output / "source")
            write_json(output / "repository.json", repo)
            stage = "discovery"
            candidates, diagnostics = self.discovery.discover(repo, config)
            write_json(output / "candidates.json", candidates)
            write_json(output / "discovery_diagnostics.json", diagnostics)
            stage = "baseline"
            baseline_oracle = BaselineOracle()
            baseline_plan = baseline_oracle.prepare(repo, config)
            write_json(output / "oracle_plan.json", baseline_plan)
            baseline = baseline_oracle.run(repo, baseline_plan, output / "baseline")
            plans, patches, prechecks, instances, rejections = [], [], [], [], []
            applied_count = 0
            for candidate in candidates:
                try:
                    if not baseline["passed"]:
                        raise FactoryError("Original regression tests must pass")
                    stage = "planning"
                    plan = self.planner.plan(repo, candidate, config)
                    plans.append(plan)
                    stage = "gold_generation"
                    patch = self.generator.generate(repo, plan)
                    slot = output / "gold" / candidate.candidate_id
                    slot.mkdir(parents=True)
                    patch_text = render_patch(patch.changes)
                    (slot / "gold.patch").write_text(patch_text, encoding="utf-8", newline="\n")
                    patch_record = {"candidate_id": candidate.candidate_id, "patch_sha256": patch.patch_sha256,
                                    "modified_files": [c.path for c in patch.changes],
                                    "line_changes": {c.path: {"added": c.added_lines, "deleted": c.deleted_lines} for c in patch.changes},
                                    "transformation": patch.transformation, "artifact": f"gold/{candidate.candidate_id}/gold.patch"}
                    write_json(slot / "patch.json", patch_record)
                    patches.append(patch_record)
                    stage = "gold_application"
                    gold_workspace = self.applier.apply(repo, patch, slot / "workspace")
                    applied_count += 1
                    write_json(slot / "application.json", {"passed": True, "workspace": f"gold/{candidate.candidate_id}/workspace"})
                    stage = "evaluator"
                    evaluator_dir = output / "evaluator" / candidate.candidate_id
                    evaluator_dir.mkdir(parents=True)
                    shutil.copy2(Path(__file__).with_name("evaluator_runner.py"), evaluator_dir / "runner.py")
                    shutil.copy2(Path(__file__).with_name("test_runner.py"), evaluator_dir / "test_runner.py")
                    spec = {"api_module": plan.api_module, "old_api": plan.old_api, "new_api": plan.new_api,
                            "call_counts": plan.call_counts, "base_hashes": {p: repo.files[p] for p in plan.affected_files},
                            "test_dir": config.test_dir, "test_pattern": config.test_pattern, "timeout": config.test_timeout,
                            "functional_inputs": [[12.5, "USD"], [0, "EUR"]],
                            "functional_expected": ["$12.50", "€0.00"]}
                    write_json(evaluator_dir / "spec.json", spec)
                    stage = "precheck"
                    noop_workspace = output / "precheck" / candidate.candidate_id / "noop_workspace"
                    shutil.copytree(repo.snapshot, noop_workspace, ignore=lambda _, names: set(names) & IGNORED)
                    oracle = self._evaluate(evaluator_dir, gold_workspace)
                    noop = self._evaluate(evaluator_dir, noop_workspace)
                    precheck = PrecheckResult(oracle, noop, oracle["passed"] and oracle["score"] == 1.0 and not noop["passed"] and noop["score"] == 0.0)
                    check_dir = output / "precheck" / candidate.candidate_id
                    write_json(check_dir / "oracle.json", oracle)
                    write_json(check_dir / "noop.json", noop)
                    write_json(check_dir / "result.json", precheck)
                    prechecks.append({"candidate_id": candidate.candidate_id, "passed": precheck.passed,
                                      "oracle_passed": oracle["passed"], "noop_failed": not noop["passed"]})
                    if not precheck.passed:
                        raise FactoryError("Gold PASS / no-op FAIL precheck did not hold")
                    stage = "packaging"
                    task = self._task(plan)
                    write_json(output / "problem_statement" / f"{candidate.candidate_id}.json", task)
                    portable = Repository(repo.source, "source", repo.content_sha256, repo.files)
                    identity = digest(dumps([repo.content_sha256, plan, patch.patch_sha256, spec], sort_keys=True).encode())[:24]
                    instances.append(PilotInstance("rf-" + identity, portable, task, plan, patch_record,
                        {"runner": f"evaluator/{candidate.candidate_id}/runner.py", "spec": f"evaluator/{candidate.candidate_id}/spec.json",
                         "command": f"python evaluator/{candidate.candidate_id}/runner.py <workspace> evaluator/{candidate.candidate_id}/spec.json"},
                        precheck, {"dockerfile": "Dockerfile", "python": ">=3.11", "dependencies": []}))
                except (FactoryError, OSError, ValueError, SyntaxError) as exc:
                    rejections.append({"candidate_id": candidate.candidate_id, "stage": stage, "error": str(exc)})
                    if stage == "gold_application":
                        write_json(output / "gold" / candidate.candidate_id / "application.json", {"passed": False, "error": str(exc)})
            write_json(output / "plans.json", plans)
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
                       "gold_patch_generated": len(patches), "gold_patch_success": applied_count,
                       "oracle_pass": sum(x["oracle_passed"] for x in prechecks),
                       "noop_fail": sum(x["noop_failed"] for x in prechecks),
                       "exported": len(instances), "rejected": len(rejections), "docker": docker["status"],
                       "output": str(output / "instances.jsonl")}
            write_json(output / "summary.json", summary)
            return summary
        except Exception as exc:
            write_json(output / "summary.json", {"status": "failed", "stage": stage, "error": str(exc)})
            raise

    @staticmethod
    def _evaluate(directory, workspace):
        spec = json.loads((directory / "spec.json").read_text(encoding="utf-8"))
        # Same portable evaluator code and spec used by Docker and rollout.
        return evaluate(workspace, spec)

    @staticmethod
    def _task(plan):
        callers = ", ".join(f"`{p}`" for p in plan.call_counts)
        statement = (f"Migrate the public amount-formatting API in `{plan.api_module}` from `{plan.old_api}(amount, currency)` "
                     f"to `{plan.new_api}(amount, *, currency)`. Update direct imports and call sites in {callers}; "
                     f"remove the old API. Preserve formatting, validation errors, and existing behavior. "
                     f"Keep regression tests passing and avoid unrelated changes.")
        return Task(plan.candidate, statement, plan.structural_requirements + plan.behavioral_invariants,
                    "python-api-migration-template-v1")

    @staticmethod
    def _docker(output, instances):
        dockerfile = output / "Dockerfile"
        if not instances:
            dockerfile.write_text("FROM python:3.11-slim\nWORKDIR /work\nCOPY source/ /work/\n", encoding="utf-8")
            return {"status": "not_run", "reason": "no valid instance"}
        cid = instances[0].plan.candidate.candidate_id
        dockerfile.write_text("FROM python:3.11-slim\nWORKDIR /work\nCOPY source/ /work/\n"
                              f"COPY evaluator/{cid}/ /evaluator/\n"
                              'CMD ["python", "/evaluator/runner.py", "/work", "/evaluator/spec.json"]\n', encoding="utf-8")
        image = "rf-factory-pilot:" + instances[0].instance_id
        commands = {"build": f"docker build -t {image} -f Dockerfile .",
                    "run_base_expected_fail": f"docker run --rm {image}"}
        try:
            version = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                                     capture_output=True, text=True, timeout=15)
            if version.returncode:
                return {"status": "not_available", "reason": version.stderr.strip() or version.stdout.strip(), "commands": commands}
            build = subprocess.run(["docker", "build", "-t", image, "-f", str(dockerfile), str(output)],
                                   capture_output=True, text=True, timeout=180)
            (output / "docker-build.stdout.txt").write_text(build.stdout, encoding="utf-8")
            (output / "docker-build.stderr.txt").write_text(build.stderr, encoding="utf-8")
            if build.returncode:
                return {"status": "build_failed", "returncode": build.returncode, "commands": commands}
            run = subprocess.run(["docker", "run", "--rm", image], capture_output=True, text=True, timeout=60)
            (output / "docker-run.stdout.txt").write_text(run.stdout, encoding="utf-8")
            (output / "docker-run.stderr.txt").write_text(run.stderr, encoding="utf-8")
            result = json.loads(run.stdout) if run.stdout.strip() else {}
            return {"status": "built_and_ran" if run.returncode == 1 and result.get("passed") is False else "run_failed",
                    "base_evaluator_expected_fail": run.returncode == 1 and result.get("passed") is False,
                    "image": image, "commands": commands}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": "not_available", "reason": str(exc), "commands": commands}
