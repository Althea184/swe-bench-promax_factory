"""Orchestrate the existing Historical instance route."""
import base64
import datetime as dt
import json
import shlex
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from ..core.common import FactoryError, digest, write_json
from ..core.process import command, require
from ..discovery.synthetic.usage_analysis import UsageIndex, module_name
from ..issue_gen.generator import ProblemGenerator
from ..repository import RepositoryPreparer
from ..repository.github import github_json, resolve_pr
from ..synthesis.candidate_pool import difficulty_metadata
from ..synthesis.generators.historical import discover_history
from ..validation.historical_acceptance import choose_strategy
from ..validation.synthetic_acceptance import eval_script
from ..environment.pinned import prepare_historical_environment
from ..validation.instance import historical_controls_valid


class HistoryPipeline:
    def run(self, *, output: Path, pr_url=None, repo_url=None, commit=None, issue_url=None,
            local_path: Path | None = None, preflight=False,
            base_image="python:3.11-slim", offline_build=False,
            test_argv=None, getter=github_json):
        test_argv = test_argv or ["python", "-m", "pytest", "-q", "tests"]
        output = output.resolve()
        if output.exists():
            raise FactoryError(f"Output already exists: {output}")
        if local_path and (output.is_relative_to(local_path.resolve()) or
                           local_path.resolve().is_relative_to(output)):
            raise FactoryError("Output and source must be separate")
        try:
            record = resolve_pr(pr_url=pr_url, repo_url=repo_url, commit=commit,
                                issue_url=issue_url, getter=getter)
        except FactoryError as exc:
            if not preflight:
                raise
            output.mkdir(parents=True)
            rejection = {"source": pr_url or issue_url or commit, "reason": str(exc)}
            write_json(output / "rejections.json", [rejection])
            summary = {"status": "no_eligible_pr", "candidate_count": 0,
                       "rejections": [rejection]}
            write_json(output / "summary.json", summary)
            return summary
        output.mkdir(parents=True)
        stage = "acquisition"
        try:
            source = str(local_path.resolve()) if local_path else record["repo_url"]
            clone = output / "acquired"
            require(command(["git", "-c", "core.autocrlf=false", "clone", "--no-hardlinks",
                             "--", source, str(clone)]), "History repository clone")
            require(command(["git", "config", "core.autocrlf", "false"], cwd=clone),
                    "History clone newline configuration")
            write_json(output / "source_pr.json", record)
            stage = "discovery"
            task, raw_patch, discovery = discover_history(record, clone)
            base = discovery["base_commit"]
            require(command(["git", "-c", "core.autocrlf=false", "checkout", "--detach", base],
                            cwd=clone), "Base checkout")
            if require(command(["git", "status", "--porcelain"], cwd=clone), "Clean base check"):
                raise FactoryError("Historical base checkout is dirty")
            repo = RepositoryPreparer().prepare(clone, output / "source")
            try:
                function = next(iter(discovery["evidence"]["functions_changed"]))
                symbol = function["symbol"]
                module = module_name(function["module"], repo.files)
                index = UsageIndex(repo.snapshot, repo.files, module, symbol)
                caller_count = sum(len(nodes) for nodes in index.call_sites().values())
                task = replace(task, candidate=replace(task.candidate,
                    metadata={**task.candidate.metadata, "affected_callers": caller_count}))
            except (ValueError, OSError, SyntaxError, StopIteration) as exc:
                task = replace(task, candidate=replace(task.candidate,
                    metadata={**task.candidate.metadata, "caller_analysis_error": str(exc)}))
            write_json(output / "discovery.json", discovery)
            write_json(output / "candidate.json", asdict(task.candidate))
            write_json(output / "task_spec.json", asdict(task))
            (output / "original-pr.patch").write_bytes(raw_patch.encode("utf-8"))
            write_json(output / "repository.json", {"repo": record["repo_url"],
                       "base_commit": base, "target_commit": discovery["target_commit"],
                       "snapshot_sha256": repo.content_sha256, "files": len(repo.files)})
            if preflight:
                summary = {"status": "preflight_completed", "candidate_count": 1,
                           "base_commit": base, "target_commit": discovery["target_commit"],
                           "candidate_id": task.candidate.candidate_id,
                           "merge_method": discovery["merge_method"],
                           "classification": list(task.candidate.refactoring_types),
                           "acceptance_strategy": task.acceptance_strategy}
                write_json(output / "summary.json", summary)
                for temporary in (clone, output / "source"):
                    if not temporary.resolve().is_relative_to(output):
                        raise FactoryError("Unsafe preflight cleanup path")
                    shutil.rmtree(temporary)
                return summary
            stage = "environment"
            image_name, image_id, run_script = prepare_historical_environment(output, record, base, base_image, offline_build, test_argv)
            stage = "baseline"
            baseline = run_script("cd /testbed && export PYTHONPATH=/testbed && " + shlex.join(test_argv))
            write_json(output / "baseline.json", {"returncode": baseline.returncode,
                       "stdout": baseline.stdout, "stderr": baseline.stderr})
            require(baseline, "History original regression suite")
            stage = "validation"
            strategy = choose_strategy(record, discovery["classification"], discovery["evidence"])
            test_patch = strategy["test_patch"]
            script = eval_script(test_patch, test_argv, "test_rf_history.py", "rf_acceptance")
            encoded = base64.b64encode(raw_patch.encode()).decode()
            apply = ("cd /testbed && echo '" + encoded +
                     "' | base64 -d > /tmp/rf-history.patch && git apply --check /tmp/rf-history.patch && "
                     "git apply /tmp/rf-history.patch || exit 1\n")
            controls = {"noop": run_script(script), "gold": run_script(apply + script),
                        "gold_clean_apply": run_script(apply + "git diff --check && git status --short")}
            write_json(output / "validation.json", {key: {"returncode": value.returncode,
                       "stdout": value.stdout, "stderr": value.stderr} for key, value in controls.items()})
            if not historical_controls_valid(controls, strategy):
                raise FactoryError("History Gold, No-op, or clean-apply Docker control failed")
            stage = "export"
            problem = ProblemGenerator().generate_historical(task).text
            instance_id = (f"{record['owner']}__{record['name']}-h_{base[:7]}-"
                           f"pr{record['number']}-{task.candidate.candidate_id[:8]}")
            row = {"instance_id": instance_id, "repo": f"{record['owner']}/{record['name']}",
                   "base_commit": base, "environment_setup_commit": base,
                   "created_at": int(dt.datetime.now(dt.timezone.utc).timestamp()),
                   "language": "python", "working_dir": "/testbed", "image_name": image_name,
                   "patch": raw_patch, "test_patch": test_patch,
                   "problem_statement": problem, "hints_text": ""}
            write_json(output / "swe-bench-promax.json", [row])
            write_json(output / "eval.json", {instance_id: {"instance_id": instance_id,
                       "eval_script": script}})
            (output / "instances.jsonl").write_text(json.dumps({"instance_id": instance_id,
                "repository": {"snapshot": "source"}, "problem_statement": {"text": problem},
                "environment": {"python": ">=3.11", "dependencies": []}}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            (output / "gold.patch").write_bytes(raw_patch.encode("utf-8"))
            (output / "test.patch").write_bytes(test_patch.encode("utf-8"))
            (output / "eval.sh").write_bytes(script.encode("utf-8"))
            write_json(output / "problem_evidence.json", {"pr_title": record["title"],
                       "pr_description": record["description"], "generated_problem": problem,
                       "source_url": record["url"], "public_history_leakage_risk": True})
            write_json(output / "patch_provenance.json", {"original_pr_patch_sha256": digest(raw_patch.encode()),
                       "gold_patch_sha256": digest(raw_patch.encode()),
                       "relation": "exact developer production diff", "merge_method": discovery["merge_method"]})
            write_json(output / "difficulty.json", difficulty_metadata((task,), raw_patch,
                       discovery["changed_files"]))
            summary = {"status": "completed", "instance_id": instance_id,
                       "base_commit": base, "target_commit": discovery["target_commit"],
                       "pr_url": record["url"], "image_id": image_id,
                       "controls": {key: value.returncode for key, value in controls.items()}}
            write_json(output / "summary.json", summary)
            return summary
        except Exception as exc:
            write_json(output / "rejections.json", [{"source": record["url"],
                       "stage": stage, "reason": str(exc)}])
            summary = {"status": "no_eligible_candidate" if preflight else "failed",
                       "stage": stage, "error": str(exc), "candidate_count": 0}
            write_json(output / "summary.json", summary)
            if preflight:
                for temporary in (output / "acquired", output / "source"):
                    if temporary.exists():
                        if not temporary.resolve().is_relative_to(output):
                            raise FactoryError("Unsafe preflight cleanup path")
                        shutil.rmtree(temporary)
                return summary
            raise
