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

from .api_migration import (ApiMigrationDiscovery, ApiMigrationGoldGenerator,
                            ApiMigrationSpecBuilder, render_patch)
from .common import FactoryError, digest, write_json
from .config import Config, MigrationRecipe
from .problem_generation import ProblemGenerator
from .repository import RepositoryPreparer
from .usage_analysis import UsageIndex, module_name


def command(argv, *, cwd=None, timeout=900):
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError(f"{argv[0]} execution failed: {exc}") from exc


def require(result, operation):
    if result.returncode:
        raise FactoryError(f"{operation} failed ({result.returncode}): {result.stderr[-2000:]}")
    return result.stdout.strip()


def discover_recipes(repo, *, include_rejections=False):
    """Infer simple keyword-only migrations from definitions and their static calls."""
    recipes, diagnostics, rejections = [], [], []
    files = [path for path in repo.files if path.endswith(".py") and not
             any(part in {"tests", "test", "fixtures"} for part in Path(path).parts)]
    for path in sorted(files):
        try:
            tree = ast.parse((repo.snapshot / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError) as exc:
            diagnostics.append({"path": path, "error": str(exc)})
            continue
        module = module_name(path, repo.files)
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            identity = {"path": path, "module": module, "symbol": fn.name, "line": fn.lineno}
            if fn.name.startswith("_"):
                if include_rejections:
                    rejections.append({**identity, "reason": "private top-level function"})
                continue
            args = fn.args
            if (fn.decorator_list or args.posonlyargs or len(args.args) != 2 or
                    len(args.defaults) > 1 or args.kwonlyargs or args.vararg or args.kwarg):
                if include_rejections:
                    rejections.append({**identity, "reason": "unsupported signature or decorators"})
                continue
            try:
                index = UsageIndex(repo.snapshot, files, module, fn.name)
                sites = index.call_sites()
                if not sites:
                    if include_rejections:
                        rejections.append({**identity, "reason": "no statically resolved production callers"})
                    continue
                if any(len(call.args) != 2 or call.keywords
                       for calls in sites.values() for call in calls):
                    if include_rejections:
                        rejections.append({**identity, "reason": "unsupported production call shape"})
                    continue
            except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                diagnostics.append({"path": path, "symbol": fn.name, "error": str(exc)})
                if include_rejections:
                    rejections.append({**identity, "reason": f"call graph analysis failed: {exc}"})
                continue
            parameter = args.args[1].arg
            recipes.append((len(sites), sum(map(len, sites.values())),
                            MigrationRecipe(module, fn.name, fn.name, parameter,
                                f"Make the {fn.name} API require an explicit named {parameter} argument and migrate its callers.",
                                ("Preserve the existing behavior and validation of this API.",
                                 "Keep the original project regression suite passing."),
                                ("All production callers must use the named argument.",),
                                (), auto_discovered=True)))
    recipes.sort(key=lambda item: (-item[0], -item[1], item[2].module, item[2].symbol))
    result = ([item[2] for item in recipes], diagnostics)
    return (*result, rejections) if include_rejections else result


def test_patch_for(spec, path="tests/test_rf_migration.py"):
    """New test checks the requirement independently of the reference patch."""
    content = '''import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_PATH = %r
SYMBOL = %r
PARAMETER = %r
CALLERS = %r


class TestRefactoringContract(unittest.TestCase):
    def test_public_api_requires_named_argument(self):
        tree = ast.parse((ROOT / API_PATH).read_text(encoding="utf-8"))
        matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == SYMBOL]
        self.assertEqual(len(matches), 1)
        fn = matches[0]
        self.assertEqual([arg.arg for arg in fn.args.kwonlyargs], [PARAMETER])
        self.assertEqual(len(fn.args.args), 1)

    def test_static_callers_migrated(self):
        for path, count in CALLERS.items():
            with self.subTest(path=path):
                tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
                calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                         and (getattr(node.func, "id", None) == SYMBOL or
                              getattr(node.func, "attr", None) == SYMBOL)]
                self.assertGreaterEqual(len(calls), count)
                self.assertTrue(all(len(node.args) == 1 and len(node.keywords) == 1 and
                                    node.keywords[0].arg == PARAMETER for node in calls))
''' % (spec.entities["api_module"], spec.entities["target_api"],
       spec.entities["keyword_parameter"], spec.metadata["call_counts"])
    lines = content.splitlines(keepends=True)
    patch = (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
             f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" +
             "".join("+" + line for line in lines))
    return patch


def eval_script(test_patch, test_argv, test_pattern="test_rf_migration.py",
                test_dir="tests"):
    encoded = base64.b64encode(test_patch.encode()).decode()
    return ("#!/bin/bash\nset -uo pipefail\ncd /testbed\n"
            "export PIP_NO_INDEX=1 UV_OFFLINE=1 PYTHONPATH=/testbed\n"
            f"echo '{encoded}' | base64 -d > /tmp/rf-test.patch\n"
            "if ! git apply /tmp/rf-test.patch; then echo OMNIGRIL_TEST_PATCH_APPLY_FAILED=1; exit 1; fi\n"
            f"{shlex.join(test_argv)} && python -m unittest discover -s {shlex.quote(test_dir)} -p {shlex.quote(test_pattern)}\n"
            "rc=$?\necho OMNIGRIL_EXIT_CODE=$rc\nexit $rc\n")


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
                from .compound import discovery_preflight
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
            dockerfile = (f"FROM {base_image}\n"
                          "RUN command -v git || (apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*)\n"
                          "RUN rm -rf /testbed\n"
                          "COPY acquired/ /testbed/\nWORKDIR /testbed\n"
                          "RUN python -m pip install --no-cache-dir " +
                          ("--no-index --no-build-isolation " if offline_build else "") + "-e .\n"
                          "ENV PYTHONPATH=/testbed PIP_NO_INDEX=1 UV_OFFLINE=1\n")
            (output / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            (output / ".dockerignore").write_text("*\n!Dockerfile\n!acquired/\n!acquired/**\n", encoding="utf-8")
            image_name = "rf-promax:" + digest(f"{repo_url}:{base_commit}:{base_image}:{offline_build}".encode())[:16]
            build = command(["docker", "build", "-t", image_name, str(output)], timeout=1800)
            (output / "docker-build.stdout.txt").write_text(build.stdout, encoding="utf-8")
            (output / "docker-build.stderr.txt").write_text(build.stderr, encoding="utf-8")
            require(build, "Docker image build")
            image_id = require(command(["docker", "image", "inspect", "--format", "{{.Id}}", image_name]),
                               "Docker image inspection")

            def run_script(script):
                return command(["docker", "run", "--rm", "--network", "none", image_name,
                                "bash", "-c", script], timeout=600)

            image_commit = require(run_script("cd /testbed && git rev-parse HEAD"), "Image commit check")
            if image_commit != base_commit:
                raise FactoryError("Docker image contains the wrong base commit")
            write_json(output / "environment.json", {"repo_url": repo_url,
                       "image_name": image_name, "image_id": image_id,
                       "base_image": base_image, "base_commit": base_commit,
                       "working_dir": "/testbed", "offline_build": offline_build,
                       "test_argv": test_argv})

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
                    if noop.returncode == 0 or golden.returncode != 0:
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
                    from .candidate_pool import synthetic_candidate, difficulty_metadata
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
