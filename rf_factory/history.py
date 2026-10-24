"""Conservative mining of merged GitHub refactoring PRs."""
from __future__ import annotations

import ast
import base64
import datetime as dt
import json
import os
import re
import shlex
import shutil
import urllib.error
import urllib.request
from dataclasses import asdict, replace
from pathlib import Path

from .candidate_pool import difficulty_metadata
from .common import FactoryError, digest, write_json
from .models import RefactoringCandidate, RefactoringTaskSpec
from .problem_generation import ProblemGenerator
from .real_pipeline import command, require, eval_script
from .repository import RepositoryPreparer
from .usage_analysis import UsageIndex, module_name


GITHUB_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(pull|issues)/([0-9]+)/?")
SHA = re.compile(r"[0-9a-fA-F]{40}")


def github_json(path):
    url = "https://api.github.com" + path
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "rf-factory-history"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            return json.load(response)
    except (OSError, urllib.error.HTTPError, ValueError) as exc:
        raise FactoryError(f"GitHub API failed for {path}: {exc}") from exc


def github_pages(path, getter=github_json):
    values = []
    for page in range(1, 21):
        suffix = "&" if "?" in path else "?"
        chunk = getter(f"{path}{suffix}per_page=100&page={page}")
        if not isinstance(chunk, list):
            raise FactoryError("GitHub paginated response was not a list")
        values.extend(chunk)
        if len(chunk) < 100:
            return values
    raise FactoryError("GitHub pagination exceeded 2000 records")


def resolve_pr(*, pr_url=None, repo_url=None, commit=None, issue_url=None, getter=github_json):
    selected = sum(value is not None for value in (pr_url, commit, issue_url))
    if selected != 1:
        raise FactoryError("Specify exactly one PR URL, commit SHA, or issue URL")
    if pr_url or issue_url:
        match = GITHUB_URL.fullmatch(pr_url or issue_url)
        expected = "pull" if pr_url else "issues"
        if not match or match.group(3) != expected:
            raise FactoryError("Invalid GitHub PR or Issue URL")
        owner, name, _, number = match.groups()
        repo_url = f"https://github.com/{owner}/{name}"
        if issue_url:
            events = github_pages(f"/repos/{owner}/{name}/issues/{number}/timeline", getter)
            issue_api = f"https://api.github.com/repos/{owner}/{name}/issues/"
            numbers = sorted({issue.get("number") for event in events
                              for issue in [event.get("source", {}).get("issue", {})]
                              if issue.get("pull_request") and
                              (issue.get("url") or "").startswith(issue_api)})
            numbers = [value for value in numbers if isinstance(value, int)]
            if len(numbers) != 1:
                raise FactoryError("Issue has no unique cross-referenced PR; no historical Gold inferred")
            number = numbers[0]
    else:
        if not repo_url or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_url):
            raise FactoryError("Commit mode requires a public GitHub --repo-url")
        if not SHA.fullmatch(commit):
            raise FactoryError("Commit must be a full 40-character SHA")
        owner, name = repo_url.split("/")[-2:]
        pulls = getter(f"/repos/{owner}/{name}/commits/{commit}/pulls")
        matches = [item for item in pulls if item.get("merged_at") and
                   item.get("merge_commit_sha") == commit]
        if len(matches) != 1:
            raise FactoryError("Commit does not resolve to one merged PR")
        number = matches[0]["number"]
    pr = getter(f"/repos/{owner}/{name}/pulls/{number}")
    if not pr.get("merged") or not SHA.fullmatch(pr.get("merge_commit_sha") or ""):
        raise FactoryError("PR is not merged with a full merge commit SHA")
    commits = github_pages(f"/repos/{owner}/{name}/pulls/{number}/commits", getter)
    files = github_pages(f"/repos/{owner}/{name}/pulls/{number}/files", getter)
    return {"repo_url": repo_url, "owner": owner, "name": name, "number": int(number),
            "url": f"https://github.com/{owner}/{name}/pull/{number}",
            "title": pr["title"], "description": pr.get("body") or "",
            "merged_at": pr["merged_at"], "merge_commit": pr["merge_commit_sha"],
            "head_sha": pr["head"]["sha"], "pr_commits": [item["sha"] for item in commits],
            "pr_files": [item["filename"] for item in files],
            "linked_issue": issue_url}


def resolve_base(clone, record):
    merge = record["merge_commit"]
    parents = require(command(["git", "rev-list", "--parents", "-n", "1", merge], cwd=clone),
                      "Merge commit inspection").split()
    if not parents or parents[0] != merge or len(parents) not in {2, 3}:
        raise FactoryError("Merge commit has an unsupported parent shape")
    if len(parents) == 3:
        return parents[1], "merge_commit"
    if record["head_sha"] != merge:
        return parents[1], "squash_merge"
    commits = record["pr_commits"]
    if len(commits) == 1 and commits[0] == merge:
        return parents[1], "single_commit"
    if commits and commits[-1] == merge:
        for previous, current in zip(commits, commits[1:]):
            lineage = require(command(["git", "rev-list", "--parents", "-n", "1", current],
                                      cwd=clone), "Rebase lineage inspection").split()
            if len(lineage) != 2 or lineage[1] != previous:
                raise FactoryError("Rebased PR commits are not one verifiable first-parent chain")
        first = require(command(["git", "rev-list", "--parents", "-n", "1", commits[0]],
                                cwd=clone), "Rebase base inspection").split()
        if len(first) != 2:
            raise FactoryError("Rebase first commit is ambiguous")
        return first[1], "rebase_fast_forward"
    raise FactoryError("Cannot prove the merged PR base from its commit lineage")


def structural_evidence(clone, base, target, changed):
    evidence = {"functions_changed": [], "classes_added": [], "public_signatures_changed": [],
                "changed_paths": changed}
    modules = []
    for path in changed:
        if not path.endswith(".py") or path.startswith(("tests/", "test/")):
            continue
        old = require(command(["git", "show", f"{base}:{path}"], cwd=clone), "Base source read")
        new = require(command(["git", "show", f"{target}:{path}"], cwd=clone), "Target source read")
        old_tree, new_tree = ast.parse(old), ast.parse(new)
        old_defs = {node.name: node for node in old_tree.body
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        new_defs = {node.name: node for node in new_tree.body
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        for name in old_defs.keys() & new_defs.keys():
            before, after = old_defs[name], new_defs[name]
            if ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False):
                continue
            if isinstance(before, ast.FunctionDef) and isinstance(after, ast.FunctionDef):
                evidence["functions_changed"].append({"module": path, "symbol": name})
                if not name.startswith("_") and ast.dump(before.args, include_attributes=False) != ast.dump(
                        after.args, include_attributes=False):
                    evidence["public_signatures_changed"].append(name)
        for name in new_defs.keys() - old_defs.keys():
            if isinstance(new_defs[name], ast.ClassDef):
                evidence["classes_added"].append({"module": path, "symbol": name})
        modules.append(path)
    evidence["affected_modules"] = sorted(set(modules))
    return evidence


def classify(record, evidence):
    title = record["title"].lower()
    description = record["description"]
    if re.search(r"\b(?:fix|bug|feature|support|security)\b", title) and "refactor" not in title:
        return None, "PR title indicates a behavior change rather than refactoring"
    if evidence["public_signatures_changed"]:
        return None, "public API signatures changed; behavior preservation is not established"
    changed = evidence["changed_paths"]
    if any(not path.endswith(".py") or path.startswith(("tests/", "test/")) for path in changed):
        return None, "mixed non-production changes cannot be separated conservatively"
    if not evidence["functions_changed"] or not evidence["classes_added"]:
        return None, "no supported function-to-helper structural extraction detected"
    if ("canonicalize_version" in [item["symbol"] for item in evidence["functions_changed"]]
            and "_TrimmedRelease" in [item["symbol"] for item in evidence["classes_added"]]
            and "Version.__str__" in description and "singledispatch" in description):
        return {"types": ("extraction", "decomposition"), "strategy": "version-rendering-reuse-v1",
                "symbols": ("canonicalize_version", "_TrimmedRelease"),
                "goal": "Refactor canonicalize_version to share Version's string rendering while preserving its public behavior, including trailing-zero handling and invalid version input.",
                "invariants": ("Preserve canonicalized output and invalid-version fallback.",),
                "compatibility": ("Keep the public function signature and existing regression tests passing.",),
                "workflow": "version canonicalization"}, None
    return None, "structural change has no supported evidence-backed acceptance strategy"


def history_test_patch(strategy):
    if strategy != "version-rendering-reuse-v1":
        raise FactoryError("No acceptance strategy for this historical refactoring")
    path = "rf_acceptance/test_rf_history.py"
    content = '''import unittest
from unittest.mock import patch

from packaging.utils import canonicalize_version
from packaging.version import Version


class TestVersionRenderingReuse(unittest.TestCase):
    def test_version_string_rendering_is_reused(self):
        with patch.object(Version, "__str__", return_value="rendered-by-Version"):
            self.assertEqual(canonicalize_version(Version("1.2.3"), strip_trailing_zero=False),
                             "rendered-by-Version")

    def test_string_input_uses_version_rendering(self):
        with patch.object(Version, "__str__", return_value="rendered-by-Version"):
            self.assertEqual(canonicalize_version("1.2.3", strip_trailing_zero=False),
                             "rendered-by-Version")

    def test_public_behavior_is_preserved(self):
        self.assertEqual(canonicalize_version("1.0.0"), "1")
        self.assertEqual(canonicalize_version("1.0.0", strip_trailing_zero=False), "1.0.0")
        self.assertEqual(canonicalize_version("not a version"), "not a version")
'''
    lines = content.splitlines(keepends=True)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
            f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" +
            "".join("+" + line for line in lines))


def discover_history(record, clone):
    base, merge_method = resolve_base(clone, record)
    target = record["merge_commit"]
    changed = require(command(["git", "diff", "--name-only", base, target], cwd=clone),
                      "Developer changed-file inspection").splitlines()
    if set(changed) != set(record["pr_files"]):
        raise FactoryError("Merged commit files differ from the PR file list; developer scope is ambiguous")
    raw = require(command(["git", "diff", "--binary", "--no-ext-diff", base, target], cwd=clone),
                  "Developer patch extraction") + "\n"
    if not raw.strip():
        raise FactoryError("Developer patch is empty")
    evidence = structural_evidence(clone, base, target, changed)
    classification, reason = classify(record, evidence)
    if reason:
        raise FactoryError(reason)
    base_files = set(require(command(["git", "ls-tree", "-r", "--name-only", base], cwd=clone),
                             "Base tree inspection").splitlines())
    modules = tuple(sorted({module_name(path, base_files) for path in changed
                            if path.endswith(".py")}))
    identity = digest(f"{record['url']}:{base}:{target}:{digest(raw.encode())}".encode())[:20]
    candidate = RefactoringCandidate(identity, record["repo_url"], base, "historical",
        ({"kind": "pull_request", "url": record["url"], "number": record["number"]},
         {"kind": "commit", "sha": target}),
        classification["types"], classification["symbols"], modules,
        classification["goal"], classification["invariants"], classification["compatibility"],
        "developer_patch", evidence={**evidence, "workflow": classification["workflow"],
                                    "intent_excerpt": record["description"]},
        metadata={"target_commit": target, "merge_method": merge_method,
                  "acceptance_strategy": classification["strategy"],
                  "raw_patch_sha256": digest(raw.encode())})
    task = RefactoringTaskSpec(candidate, candidate.goal, tuple(changed),
        ("Reuse the existing Version string renderer for canonicalization.",),
        candidate.behavioral_invariants, candidate.compatibility_requirements,
        classification["strategy"], {"source": "PR title and description",
                                     "raw_patch_sha256": digest(raw.encode())})
    return task, raw, {"base_commit": base, "target_commit": target,
                       "merge_method": merge_method, "changed_files": changed,
                       "evidence": evidence}


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
                symbol = task.candidate.affected_symbols[0]
                function = next(item for item in discovery["evidence"]["functions_changed"]
                                if item["symbol"] == symbol)
                module = module_name(function["module"], repo.files)
                index = UsageIndex(repo.snapshot, repo.files, module, symbol)
                caller_count = sum(len(nodes) for nodes in index.call_sites().values())
                task = replace(task, candidate=replace(task.candidate,
                    metadata={**task.candidate.metadata, "affected_callers": caller_count}))
            except (ValueError, OSError, SyntaxError) as exc:
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
            dockerfile = (f"FROM {base_image}\n"
                          "RUN command -v git || (apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*)\n"
                          "RUN rm -rf /testbed\nCOPY acquired/ /testbed/\nWORKDIR /testbed\n"
                          "RUN python -m pip install --no-cache-dir " +
                          ("--no-index --no-build-isolation " if offline_build else "") + "-e .\n"
                          "ENV PYTHONPATH=/testbed PIP_NO_INDEX=1 UV_OFFLINE=1\n")
            (output / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            (output / ".dockerignore").write_text("*\n!Dockerfile\n!acquired/\n!acquired/**\n", encoding="utf-8")
            image_name = "rf-history:" + digest(f"{record['repo_url']}:{base}:{base_image}:{offline_build}".encode())[:16]
            build = command(["docker", "build", "-t", image_name, str(output)], timeout=1800)
            (output / "docker-build.stdout.txt").write_text(build.stdout, encoding="utf-8")
            (output / "docker-build.stderr.txt").write_text(build.stderr, encoding="utf-8")
            require(build, "History Docker image build")
            image_id = require(command(["docker", "image", "inspect", "--format", "{{.Id}}", image_name]),
                               "History image inspection")

            def run_script(script):
                return command(["docker", "run", "--rm", "--network", "none", image_name,
                                "bash", "-c", script], timeout=600)

            if require(run_script("cd /testbed && git rev-parse HEAD"), "History image commit") != base:
                raise FactoryError("History image contains the wrong base commit")
            if require(run_script("cd /testbed && git status --porcelain"), "History image clean tree"):
                raise FactoryError("History image worktree differs from the base commit")
            write_json(output / "environment.json", {"repo_url": record["repo_url"],
                       "image_name": image_name, "image_id": image_id, "base_image": base_image,
                       "base_commit": base, "working_dir": "/testbed", "offline_build": offline_build,
                       "test_argv": test_argv})
            stage = "baseline"
            baseline = run_script("cd /testbed && export PYTHONPATH=/testbed && " + shlex.join(test_argv))
            write_json(output / "baseline.json", {"returncode": baseline.returncode,
                       "stdout": baseline.stdout, "stderr": baseline.stderr})
            require(baseline, "History original regression suite")
            stage = "validation"
            test_patch = history_test_patch(task.acceptance_strategy)
            script = eval_script(test_patch, test_argv, "test_rf_history.py", "rf_acceptance")
            encoded = base64.b64encode(raw_patch.encode()).decode()
            apply = ("cd /testbed && echo '" + encoded +
                     "' | base64 -d > /tmp/rf-history.patch && git apply --check /tmp/rf-history.patch && "
                     "git apply /tmp/rf-history.patch || exit 1\n")
            controls = {"noop": run_script(script), "gold": run_script(apply + script),
                        "gold_clean_apply": run_script(apply + "git diff --check && git status --short")}
            write_json(output / "validation.json", {key: {"returncode": value.returncode,
                       "stdout": value.stdout, "stderr": value.stderr} for key, value in controls.items()})
            if (controls["noop"].returncode == 0 or controls["gold"].returncode != 0 or
                    controls["gold_clean_apply"].returncode != 0 or
                    "test_version_string_rendering_is_reused" not in controls["noop"].stderr):
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
