"""Strict two-revision Historical Compound synthesis."""
from __future__ import annotations

import base64
import datetime as dt
import json
import re
import shlex
from dataclasses import asdict
from pathlib import Path

from .candidate_pool import difficulty_metadata
from .common import FactoryError, digest, write_json
from .history import discover_history, resolve_base, resolve_pr
from .history_acceptance import StaticStructuralOperations
from .history_scan import CachedGitHub, cached_clone
from .models import CompoundTaskSpec
from .problem_generation import ProblemGenerator
from .real_pipeline import command, eval_script, require
from .repository import RepositoryPreparer


def commit_record(clone: Path, repo_url: str, sha: str):
    """Build provenance from a real Git object when the caller supplies commits."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise FactoryError("Historical compound commits require full SHA values")
    parents = require(command(["git", "rev-list", "--parents", "-n", "1", sha], cwd=clone),
                      "Historical commit lookup").split()
    if len(parents) != 2 or parents[0].lower() != sha.lower():
        raise FactoryError("Commit must be a single-parent historical revision")
    title = require(command(["git", "show", "-s", "--format=%s", sha], cwd=clone),
                    "Historical commit title")
    body = require(command(["git", "show", "-s", "--format=%b", sha], cwd=clone),
                   "Historical commit body")
    files = require(command(["git", "diff", "--name-only", parents[1], sha], cwd=clone),
                    "Historical commit scope").splitlines()
    owner, name = repo_url.rstrip("/").removesuffix(".git").split("/")[-2:]
    number = re.search(r"\(#(\d+)\)\s*$", title)
    return {"repo_url": repo_url, "owner": owner, "name": name,
            "number": int(number.group(1)) if number else None,
            "url": f"{repo_url}/commit/{sha}", "title": title,
            "description": body, "merge_commit": sha.lower(), "head_sha": sha.lower(),
            "pr_commits": [sha.lower()], "pr_files": files,
            "metadata_source": "git_commit", "linked_issue": None}


def historical_relation(first, second, first_discovery, second_discovery):
    """Require version adjacency and a structural relationship, separately."""
    a, b = first.candidate, second.candidate
    if a.repository != b.repository:
        raise FactoryError("Historical subtasks come from different repositories")
    if a.metadata["target_commit"] != b.base_commit:
        raise FactoryError("Historical revisions are not consecutive; intermediate commits are excluded")
    left = first_discovery["classification"]["operations"]
    right = second_discovery["classification"]["operations"]
    relocations = ([op for op in left if op["kind"] == "relocation"],
                   [op for op in right if op["kind"] == "relocation"])
    shared_sources = sorted({op["from_module"] for op in relocations[0]} &
                            {op["from_module"] for op in relocations[1]})
    destinations = sorted({op["to_module"] for group in relocations for op in group})
    if (len(relocations[0]) == len(left) and len(relocations[1]) == len(right)
            and shared_sources and len(destinations) >= 2
            and all(op.get("callers") for group in relocations for op in group)):
        return {"kind": "shared_architectural_responsibility",
                "evidence": "successive relocation of called helpers from one utility module to their owning modules",
                "shared_source_modules": shared_sources, "destination_modules": destinations,
                "historical_order": [a.metadata["target_commit"], b.metadata["target_commit"]],
                "patch_compatibility": "consecutive_first_parent"}
    symbols_a = set(a.affected_symbols)
    symbols_b = set(b.affected_symbols)
    overlap = sorted(symbols_a & symbols_b)
    shared_modules = sorted(set(a.affected_modules) & set(b.affected_modules))
    if overlap and shared_modules:
        return {"kind": "shared_symbols", "evidence": "both changes affect named production symbols",
                "shared_symbols": overlap, "shared_modules": shared_modules, "historical_order":
                [a.metadata["target_commit"], b.metadata["target_commit"]],
                "patch_compatibility": "consecutive_first_parent"}
    raise FactoryError("No evidenced call or shared architectural responsibility links the two refactorings")


def _result(result):
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


class HistoryCompoundPipeline:
    def run(self, *, output: Path, pr_urls=None, commits=None, repo_url=None,
            local_path: Path | None = None, cache: Path | None = None,
            preflight=False, base_image="python:3.11-slim", offline_build=False,
            test_argv=None, getter=None):
        if bool(pr_urls) == bool(commits) or len(pr_urls or commits) != 2:
            raise FactoryError("Specify exactly two PR URLs or two full commits")
        output = output.resolve()
        if output.exists():
            raise FactoryError(f"Output already exists: {output}")
        if local_path and (output.is_relative_to(local_path.resolve()) or
                           local_path.resolve().is_relative_to(output)):
            raise FactoryError("Output and source must be separate")
        test_argv = test_argv or ["python", "-m", "pytest", "-q", "tests"]
        if commits and (not repo_url or not re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_url)):
            raise FactoryError("Commit mode requires a GitHub --repo-url")
        cache = (cache or Path(".rf-factory-work/history-cache")).resolve()
        metadata = getter or CachedGitHub(cache)
        output.mkdir(parents=True)
        stage = "acquisition"
        try:
            records = [resolve_pr(pr_url=url, getter=metadata) for url in pr_urls] if pr_urls else None
            if records and len({item["repo_url"] for item in records}) != 1:
                raise FactoryError("Historical PRs belong to different repositories")
            repo_url = records[0]["repo_url"] if records else repo_url
            if local_path:
                source = local_path.resolve(strict=True)
            elif records:
                source = cached_clone(cache, records[0], {})
                cached_clone(cache, records[1], {})
            else:
                source = cache / "repos" / "__".join(repo_url.split("/")[-2:])
                if not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    require(command(["git", "-c", "core.autocrlf=false", "clone", "--no-hardlinks",
                                     "--", repo_url, str(source)], timeout=600), "History source clone")
            if records is None:
                records = [commit_record(source, repo_url, sha) for sha in commits]
            stage = "discovery"
            second_base, _ = resolve_base(source, records[1])
            if records[0]["merge_commit"] != second_base:
                raise FactoryError("Historical revisions are not consecutive; intermediate commits are excluded")
            discovered = [discover_history(record, source) for record in records]
            tasks = [item[0] for item in discovered]
            reports = [item[2] for item in discovered]
            relation = historical_relation(tasks[0], tasks[1], reports[0], reports[1])
            if any(task.acceptance_strategy != StaticStructuralOperations.strategy_id for task in tasks):
                raise FactoryError("Compound requires independently composable structural acceptance")
            base, middle, final = (reports[0]["base_commit"], reports[0]["target_commit"],
                                   reports[1]["target_commit"])
            for sha in (base, middle, final):
                require(command(["git", "cat-file", "-e", sha + "^{commit}"], cwd=source),
                        "Historical revision availability")
            if require(command(["git", "rev-parse", final + "^1"], cwd=source),
                       "Historical first-parent check") != middle:
                raise FactoryError("Second revision is not directly based on the first")
            patches = [item[1] for item in discovered]
            changed = [item["changed_files"] for item in reports]
            goal = ("Move related production helpers out of the shared utility module into "
                    "the modules that own their behavior, and update the affected callers.") if relation[
                    "kind"] == "shared_architectural_responsibility" else (
                    "Complete the related historical refactorings while preserving their public behavior.")
            composition = CompoundTaskSpec(tuple(tasks),
                ((tasks[0].candidate.candidate_id, tasks[1].candidate.candidate_id),), goal,
                tuple(sorted(set(changed[0]) | set(changed[1]))),
                {"repository": repo_url, "base_commit": base, "relation": relation,
                 "source_refs": [item["url"] for item in records],
                 "behavioral_invariants": list(dict.fromkeys(
                     value for task in tasks for value in task.behavioral_invariants))})
            write_json(output / "source_records.json", records)
            write_json(output / "subtasks.json", [asdict(task) for task in tasks])
            write_json(output / "composition.json", asdict(composition))
            write_json(output / "relations.json", relation)
            for index, patch in enumerate(patches):
                (output / f"original-{index}.patch").write_bytes(patch.encode())
            summary = {"status": "preflight_completed", "candidate_count": 2,
                       "pair_count": 1, "repository": repo_url, "base_commit": base,
                       "intermediate_commit": middle, "final_commit": final,
                       "relation": relation, "docker_verified": False}
            if preflight:
                write_json(output / "summary.json", summary)
                return summary
            stage = "composition"
            acquired = output / "acquired"
            require(command(["git", "-c", "core.autocrlf=false", "clone", "--no-hardlinks",
                             "--", str(source), str(acquired)], timeout=600), "Composition clone")
            require(command(["git", "config", "core.autocrlf", "false"], cwd=acquired),
                    "Composition newline configuration")
            require(command(["git", "checkout", "--detach", base], cwd=acquired), "Base checkout")
            if require(command(["git", "status", "--porcelain"], cwd=acquired), "Clean base"):
                raise FactoryError("Historical base tree is dirty")
            repository = RepositoryPreparer().prepare(acquired, output / "source")
            write_json(output / "repository.json", {"repo": repo_url,
                "base_commit": base, "target_commit": final,
                "snapshot_sha256": repository.content_sha256,
                "files": len(repository.files)})
            steps = []
            for index, (from_sha, to_sha) in enumerate(((base, middle), (middle, final))):
                require(command(["git", "checkout", "--detach", to_sha], cwd=acquired),
                        "Historical step replay")
                if require(command(["git", "status", "--porcelain"], cwd=acquired),
                           "Historical replay status"):
                    raise FactoryError("Historical replay produced a dirty tree")
                observed = require(command(["git", "diff", "--binary", "--no-ext-diff",
                                            from_sha, to_sha], cwd=acquired), "Replayed step diff") + "\n"
                if observed.encode() != patches[index].encode():
                    raise FactoryError("Historical replay differs from developer commit patch")
                steps.append({"subtask": index, "from": from_sha, "to": to_sha,
                              "changed_files": changed[index],
                              "original_patch_sha256": digest(patches[index].encode())})
            gold = require(command(["git", "diff", "--binary", "--no-ext-diff", base,
                                    final], cwd=acquired), "Unified Gold diff") + "\n"
            if not gold.strip():
                raise FactoryError("Unified Gold Patch is empty")
            write_json(output / "replay.json", steps)
            (output / "gold.patch").write_bytes(gold.encode())
            acceptance = StaticStructuralOperations()
            tests = [acceptance._test_patch(reports[i]["classification"]["operations"],
                     f"rf_acceptance/test_rf_history_{i}.py", f"test_subtask_{i}_operations")
                     for i in range(2)]
            test_patch = "".join(tests)
            script = eval_script(test_patch, test_argv, "test_rf_history_*.py", "rf_acceptance")
            (output / "test.patch").write_bytes(test_patch.encode())
            (output / "eval.sh").write_text(script, encoding="utf-8")
            stage = "environment"
            dockerfile = (f"FROM {base_image}\n"
                "RUN command -v git || (apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*)\n"
                "RUN rm -rf /testbed\nCOPY acquired/ /testbed/\nWORKDIR /testbed\n"
                "RUN python -m pip install --no-cache-dir " +
                ("--no-index --no-build-isolation " if offline_build else "") + "-e .\n"
                "ENV PYTHONPATH=/testbed PIP_NO_INDEX=1 UV_OFFLINE=1\n")
            (output / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            (output / ".dockerignore").write_text("*\n!Dockerfile\n!acquired/\n!acquired/**\n", encoding="utf-8")
            require(command(["git", "checkout", "--detach", base], cwd=acquired),
                    "Docker base checkout")
            image_name = "rf-history-compound:" + digest(
                f"{repo_url}:{base}:{base_image}:{offline_build}".encode())[:16]
            build = command(["docker", "build", "-t", image_name, str(output)], timeout=1800)
            write_json(output / "docker_build.json", _result(build))
            require(build, "Historical compound Docker build")
            image_id = require(command(["docker", "image", "inspect", "--format", "{{.Id}}",
                                        image_name]), "Historical compound image inspection")
            def run_script(value):
                return command(["docker", "run", "--rm", "--network", "none", image_name,
                                "bash", "-c", value], timeout=600)
            if require(run_script("git rev-parse HEAD"), "Docker base commit") != base:
                raise FactoryError("Docker image has the wrong base revision")
            if require(run_script("git status --porcelain"), "Docker clean tree"):
                raise FactoryError("Docker image base tree is dirty")
            write_json(output / "environment.json", {"repo_url": repo_url,
                "base_commit": base, "image_name": image_name, "image_id": image_id,
                "base_image": base_image, "offline_build": offline_build,
                "working_dir": "/testbed", "test_argv": test_argv})
            stage = "validation"
            baseline = run_script("cd /testbed && export PYTHONPATH=/testbed && " +
                                  shlex.join(test_argv))
            write_json(output / "baseline.json", _result(baseline))
            require(baseline, "Historical compound original regression suite")
            def with_patch(patch, inner):
                encoded = base64.b64encode(patch.encode()).decode()
                return ("cd /testbed && echo '" + encoded +
                        "' | base64 -d > /tmp/rf-change.patch && "
                        "git apply --check /tmp/rf-change.patch && git apply /tmp/rf-change.patch || exit 1\n" + inner)
            partial_a = patches[0]
            controls = {"noop": run_script(script),
                        "noop_0": run_script(eval_script(tests[0], test_argv,
                            "test_rf_history_0.py", "rf_acceptance")),
                        "noop_1": run_script(eval_script(tests[1], test_argv,
                            "test_rf_history_1.py", "rf_acceptance")),
                        "gold": run_script(with_patch(gold, script)),
                        "gold_clean_apply": run_script(with_patch(gold,
                            "git diff --check && git status --porcelain")),
                        "partial_0": run_script(with_patch(partial_a, script)),
                        "partial_0_own": run_script(with_patch(partial_a,
                            eval_script(tests[0], test_argv, "test_rf_history_0.py",
                                        "rf_acceptance")))}
            second_applicable = run_script(with_patch(patches[1], "git diff --check"))
            second_patch = patches[1] if second_applicable.returncode == 0 else None
            cherry_pick = None
            if second_patch is None:
                cherry_pick = run_script("cd /testbed && git cherry-pick --no-commit " + final +
                    " >/tmp/rf-cherry.log && git diff --binary --no-ext-diff HEAD")
                if cherry_pick.returncode == 0 and cherry_pick.stdout.strip():
                    second_patch = cherry_pick.stdout + "\n"
                    (output / "partial-only-1-replayed.patch").write_bytes(second_patch.encode())
            if second_patch is not None:
                controls["partial_1"] = run_script(with_patch(second_patch, script))
                controls["partial_1_own"] = run_script(with_patch(second_patch,
                    eval_script(tests[1], test_argv, "test_rf_history_1.py", "rf_acceptance")))
            else:
                write_json(output / "partial_1_skipped.json", {
                    "reason": "second revision cannot be safely replayed alone on common Base",
                    "apply_result": _result(second_applicable),
                    "cherry_pick_result": _result(cherry_pick) if cherry_pick else None})
            write_json(output / "validation.json", {key: _result(value)
                                                     for key, value in controls.items()})
            if (controls["noop"].returncode == 0 or
                controls["noop_0"].returncode == 0 or controls["noop_1"].returncode == 0 or
                controls["gold"].returncode != 0 or
                controls["gold_clean_apply"].returncode != 0 or
                controls["partial_0"].returncode == 0 or
                controls["partial_0_own"].returncode != 0 or
                "test_subtask_1_operations" not in controls["partial_0"].stderr or
                ("partial_1" in controls and (controls["partial_1"].returncode == 0 or
                 controls["partial_1_own"].returncode != 0 or
                 "test_subtask_0_operations" not in controls["partial_1"].stderr))):
                raise FactoryError("Historical compound Docker controls failed")
            stage = "export"
            problem = ProblemGenerator().generate_compound_historical(composition).text
            owner, name = repo_url.split("/")[-2:]
            identity = digest(f"{base}:{middle}:{final}:{acceptance.strategy_id}".encode())[:8]
            instance_id = f"{owner}__{name}-hc_{base[:7]}-{identity}"
            row = {"instance_id": instance_id, "repo": f"{owner}/{name}",
                   "base_commit": base, "environment_setup_commit": base,
                   "created_at": int(dt.datetime.now(dt.timezone.utc).timestamp()),
                   "language": "python", "working_dir": "/testbed",
                   "image_name": image_name, "patch": gold, "test_patch": test_patch,
                   "problem_statement": problem, "hints_text": ""}
            write_json(output / "swe-bench-promax.json", [row])
            write_json(output / "eval.json", {instance_id: {"instance_id": instance_id,
                       "eval_script": script}})
            (output / "instances.jsonl").write_text(json.dumps({"instance_id": instance_id,
                "repository": {"snapshot": "source"},
                "problem_statement": {"text": problem},
                "environment": {"python": ">=3.11", "dependencies": []}},
                ensure_ascii=False) + "\n", encoding="utf-8")
            write_json(output / "problem_evidence.json", {"source_records": records,
                "generated_problem": problem, "public_history_leakage_risk": True})
            write_json(output / "patch_provenance.json", {"original_patch_sha256":
                [digest(patch.encode()) for patch in patches],
                "gold_patch_sha256": digest(gold.encode()),
                "relation": "one Git diff from common Base to final revision",
                "replay": steps})
            write_json(output / "difficulty.json", difficulty_metadata(tasks, gold,
                       sorted(set(changed[0]) | set(changed[1])), relation))
            summary.update(status="completed", instance_id=instance_id,
                           image_id=image_id, docker_verified=True,
                           controls={key: value.returncode for key, value in controls.items()})
            write_json(output / "summary.json", summary)
            return summary
        except Exception as exc:
            write_json(output / "rejections.json", [{"stage": stage,
                       "sources": pr_urls or commits, "reason": str(exc)}])
            summary = {"status": "no_eligible_pair" if preflight else "failed",
                       "stage": stage, "error": str(exc), "pair_count": 0}
            write_json(output / "summary.json", summary)
            if preflight:
                return summary
            raise
