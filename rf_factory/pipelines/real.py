"""Conservative, Docker-gated synthesis from a fixed public Python Git revision."""
from __future__ import annotations

import ast
import base64
import datetime as dt
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from ..discovery.synthetic.api_migration import ApiMigrationDiscovery
from ..synthesis.generators.api_migration import (ApiMigrationGoldGenerator,
                            ApiMigrationSpecBuilder, render_patch)
from ..core.common import FactoryError, digest, write_json
from ..core.config import Config
from ..issue_gen.generator import ProblemGenerator
from ..repository import RepositoryPreparer


from ..core.process import command, require
from ..discovery.synthetic.recipes import discover_recipes
from ..validation.synthetic_acceptance import test_patch_for, eval_script
from ..environment.pinned import prepare_real_environment
from ..validation.instance import real_controls_valid


class RealPipeline:
    def run(self, *, repo_url: str, output: Path, commit: str | None = None,
            local_path: Path | None = None, test_argv: list[str] | None = None,
            max_candidates: int = 5, base_image: str = "python:3.11-slim",
            offline_build: bool = False, compound: bool = False,
            preflight: bool = False):
        if preflight and not compound:
            raise FactoryError("Discovery preflight currently requires --compound")
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", repo_url):
            raise FactoryError("repo_url must identify a public GitHub repository")
        if commit is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise FactoryError("base commit must be a full 40-character SHA")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", base_image):
            raise FactoryError("Invalid Docker base image")
        test_argv = test_argv or ["python", "-m", "unittest", "discover", "-s", "tests"]
        if type(max_candidates) is not int or max_candidates < 1 or not test_argv:
            raise FactoryError("max_candidates and test command must be nonempty")
        output = output.resolve()
        if output.exists():
            raise FactoryError(f"Output already exists: {output}")
        if local_path and (output.is_relative_to(local_path.resolve()) or local_path.resolve().is_relative_to(output)):
            raise FactoryError("Output and source must be separate")
        output.mkdir(parents=True)
        stage = "acquisition"
        try:
            source = str(local_path.resolve()) if local_path else repo_url
            clone = output / "acquired"
            clone_command = ["git"]
            if compound:
                clone_command += ["-c", "core.autocrlf=false"]
            require(command(clone_command + ["clone", "--no-hardlinks", "--", source, str(clone)]), "git clone")
            if commit:
                require(command(["git", "checkout", "--detach", commit], cwd=clone), "git checkout")
            base_commit = require(command(["git", "rev-parse", "HEAD"], cwd=clone), "git rev-parse")
            if commit and base_commit.lower() != commit.lower():
                raise FactoryError("Checkout differs from requested commit")
            if require(command(["git", "status", "--porcelain"], cwd=clone), "git status"):
                raise FactoryError("Acquired repository is dirty")
            repo = RepositoryPreparer().prepare(clone, output / "source")
            write_json(output / "repository.json", {"repo": repo_url, "base_commit": base_commit,
                         "snapshot_sha256": repo.content_sha256, "files": len(repo.files)})
            stage = "discovery"
            if preflight:
                recipes, diagnostics, discovery_rejections = discover_recipes(repo, include_rejections=True)
            else:
                recipes, diagnostics = discover_recipes(repo)
            write_json(output / "discovery_diagnostics.json", diagnostics)
            write_json(output / "discovered_recipes.json", [dict(module=r.module, symbol=r.symbol,
                       parameter=r.parameter) for r in recipes])
            if preflight:
                from .compound_preflight import discovery_preflight
                report = discovery_preflight(repo, recipes[:max_candidates], test_argv,
                                             diagnostics, discovery_rejections, repo_url, base_commit)
                report.update(repo_url=repo_url, base_commit=base_commit,
                              discovered_recipe_count=len(recipes),
                              considered_recipe_count=min(len(recipes), max_candidates))
                write_json(output / "preflight.json", report)
                summary = {"status": "preflight_completed", "base_commit": base_commit,
                           "candidate_count": report["candidate_count"],
                           "pair_count": report["pair_count"],
                           "preflight": str(output / "preflight.json")}
                write_json(output / "summary.json", summary)
                for temporary in (clone, output / "source"):
                    if not temporary.resolve().is_relative_to(output):
                        raise FactoryError("Unsafe preflight cleanup path")
                    shutil.rmtree(temporary)
                return summary
            if not recipes:
                raise FactoryError("No supported API migration candidate found")
            stage = "environment"
            image_name, image_id, run_script = prepare_real_environment(output, repo_url, base_commit, base_image, offline_build, test_argv)
            stage = "baseline"
            baseline = run_script("cd /testbed && export PYTHONPATH=/testbed && " + shlex.join(test_argv))
            write_json(output / "baseline.json", {"returncode": baseline.returncode,
                       "stdout": baseline.stdout, "stderr": baseline.stderr})
            require(baseline, "Original regression suite")
            stage = "synthesis"
            if compound:
                from .compound import synthesize_compound
                result = synthesize_compound(repo, recipes[:max_candidates], output, clone,
                                             repo_url, base_commit, image_name, image_id,
                                             test_argv, run_script)
                return result
            rejected = []
            for recipe in recipes[:max_candidates]:
                config = Config(output / "source", api_migrations=(recipe,), min_caller_modules=1,
                                min_changed_files=2)
                candidates, _ = ApiMigrationDiscovery().discover(repo, config)
                if not candidates:
                    continue
                candidate = candidates[0]
                task = output / "tasks" / candidate.candidate_id
                task.mkdir(parents=True)
                try:
                    spec = ApiMigrationSpecBuilder().build(repo, candidate, config)
                    problem = ProblemGenerator().generate(spec, config)
                    gold = ApiMigrationGoldGenerator().generate(repo, spec)
                    gold_patch = render_patch(gold.changes)
                    test_patch = test_patch_for(spec)
                    script = eval_script(test_patch, test_argv)
                    (task / "gold.patch").write_text(gold_patch, encoding="utf-8")
                    (task / "test.patch").write_text(test_patch, encoding="utf-8")
                    (task / "eval.sh").write_text(script, encoding="utf-8")
                    encoded = base64.b64encode(gold_patch.encode()).decode()
                    gold_script = ("cd /testbed && echo '" + encoded +
                                   "' | base64 -d > /tmp/rf-gold.patch && git apply /tmp/rf-gold.patch || exit 1\n" + script)
                    noop = run_script(script)
                    golden = run_script(gold_script)
                    for label, result in (("noop", noop), ("gold", golden)):
                        write_json(task / f"{label}.json", {"returncode": result.returncode,
                                   "stdout": result.stdout, "stderr": result.stderr})
                    if not real_controls_valid(noop, golden):
                        rejected.append({"candidate_id": candidate.candidate_id,
                                         "error": "gold/no-op control failed"})
                        continue
                    owner, name = repo_url.removesuffix(".git").rstrip("/").split("/")[-2:]
                    instance_id = f"{owner}__{name}-c_{base_commit[:7]}-{candidate.candidate_id[:8]}"
                    row = {"instance_id": instance_id, "repo": f"{owner}/{name}",
                           "base_commit": base_commit, "environment_setup_commit": base_commit,
                           "created_at": int(dt.datetime.now(dt.timezone.utc).timestamp()),
                           "language": "python", "working_dir": "/testbed", "image_name": image_name,
                           "patch": gold_patch, "test_patch": test_patch,
                           "problem_statement": problem.text, "hints_text": ""}
                    write_json(output / "swe-bench-promax.json", [row])
                    from ..synthesis.candidate_pool import synthetic_candidate, difficulty_metadata
                    unified = synthetic_candidate(spec, repository=repo_url,
                                                  base_commit=base_commit, files=repo.files)
                    from dataclasses import asdict
                    write_json(output / "unified_candidates.json", [asdict(unified.candidate)])
                    write_json(output / "difficulty.json", difficulty_metadata(
                               (unified,), gold_patch, [change.path for change in gold.changes]))
                    write_json(output / "eval.json", {instance_id: {"instance_id": instance_id,
                               "eval_script": script}})
                    solver_row = {"instance_id": instance_id,
                                  "repository": {"snapshot": "source"},
                                  "problem_statement": {"text": problem.text},
                                  "environment": {"python": ">=3.11", "dependencies": []}}
                    (output / "instances.jsonl").write_text(json.dumps(solver_row, ensure_ascii=False) + "\n",
                                                                   encoding="utf-8")
                    write_json(output / "rejections.json", rejected)
                    summary = {"status": "completed", "instance_id": instance_id,
                               "base_commit": base_commit, "image_name": image_name,
                               "image_id": image_id,
                               "candidate": {"path": candidate.path, "symbol": candidate.symbol,
                                             "callers": spec.metadata["call_counts"]},
                               "gold_pass": True, "noop_fail": True}
                    write_json(output / "summary.json", summary)
                    return summary
                except (FactoryError, OSError, ValueError, SyntaxError) as exc:
                    rejected.append({"candidate_id": candidate.candidate_id, "error": str(exc)})
            write_json(output / "rejections.json", rejected)
            raise FactoryError("No candidate passed Docker gold/no-op controls")
        except Exception as exc:
            write_json(output / "summary.json", {"status": "failed", "stage": stage, "error": str(exc)})
            raise
