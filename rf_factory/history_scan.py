"""Batch, read-only historical discovery with reusable API and Git caches."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .candidate_pool import difficulty_metadata
from .common import FactoryError, digest, write_json
from .history import GITHUB_URL, discover_history, github_json, resolve_base, resolve_pr
from .history_acceptance import choose_strategy
from .history_analysis import classify, structural_evidence
from .real_pipeline import command, require


def read_pr_urls(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        values = data.get("prs") if isinstance(data, dict) else data
    else:
        values = [line.strip() for line in text.splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
    if not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values):
        raise FactoryError("PR input must contain a nonempty list of URLs")
    if len(values) > 100 or len(values) != len(set(values)):
        raise FactoryError("PR input must contain at most 100 distinct URLs")
    for value in values:
        match = GITHUB_URL.fullmatch(value)
        if not match or match.group(3) != "pull":
            raise FactoryError(f"Invalid GitHub PR URL: {value}")
    return values


class CachedGitHub:
    def __init__(self, cache: Path):
        self.cache = cache / "api"
        self.cache.mkdir(parents=True, exist_ok=True)

    def __call__(self, path):
        destination = self.cache / (digest(path.encode()) + ".json")
        if destination.exists():
            return json.loads(destination.read_text(encoding="utf-8"))
        result = github_json(path)
        write_json(destination, result)
        return result


def cached_clone(cache: Path, record, local_paths):
    repo_url = record["repo_url"]
    destination = cache / "repos" / (record["owner"] + "__" + record["name"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        source = local_paths.get(repo_url, repo_url)
        argv = ["git", "-c", "core.autocrlf=false", "clone", "--no-hardlinks", "--no-checkout"]
        if source == repo_url:
            argv.append("--filter=blob:none")
        require(command([*argv, "--", str(source), str(destination)], timeout=600),
                "Cached history clone")
        require(command(["git", "config", "core.autocrlf", "false"], cwd=destination),
                "Cached history newline configuration")
    if command(["git", "cat-file", "-e", record["merge_commit"] + "^{commit}"],
               cwd=destination).returncode:
        require(command(["git", "fetch", "origin", record["merge_commit"]],
                        cwd=destination, timeout=600), "Historical commit fetch")
    return destination


def scan_history(*, input_path: Path, output: Path, cache: Path, local_paths=None,
                 getter=None):
    urls = read_pr_urls(input_path)
    output = output.resolve()
    if output.exists():
        raise FactoryError(f"Output already exists: {output}")
    cache = cache.resolve()
    if cache == output or cache.is_relative_to(output) or output.is_relative_to(cache):
        raise FactoryError("Cache and report output must be separate")
    output.mkdir(parents=True)
    metadata = getter or CachedGitHub(cache)
    local_paths = local_paths or {}
    candidates, rejections, reports = [], [], []
    for url in urls:
        report = {"pr_url": url, "status": "rejected", "gold_patch_extractable": False,
                  "synthesis_feasible": False}
        try:
            record = resolve_pr(pr_url=url, getter=metadata)
            report.update(repository=record["repo_url"], merged=True,
                          merge_commit=record["merge_commit"], pr_title=record["title"])
            clone = cached_clone(cache, record, local_paths)
            base, method = resolve_base(clone, record)
            target = record["merge_commit"]
            report.update(base_commit=base, target_commit=target, merge_method=method)
            changed = require(command(["git", "diff", "--name-only", base, target],
                                      cwd=clone), "History scan changed files").splitlines()
            report["changed_files"] = changed
            if set(changed) != set(record["pr_files"]):
                raise FactoryError("Merged commit files differ from PR file list")
            raw = require(command(["git", "diff", "--binary", "--no-ext-diff", base,
                                   target], cwd=clone), "History scan developer diff") + "\n"
            report["gold_patch_extractable"] = bool(raw.strip())
            if not raw.strip():
                raise FactoryError("Developer patch is empty")
            evidence = structural_evidence(clone, base, target, changed)
            classification, reason = classify(record, evidence)
            report.update(refactoring_types=list(classification["types"]),
                          atomic_operations=classification["operations"],
                          affected_symbols=list(classification["symbols"]),
                          affected_modules=evidence["affected_modules"],
                          observed_changed_caller_count=len({(item.get("module", item.get("to_module")), caller)
                              for item in classification["operations"]
                              for caller in item.get("callers", ())}),
                          evidence=evidence, review_status=classification["review_status"])
            if reason:
                raise FactoryError(reason)
            strategy = choose_strategy(record, classification, evidence)
            task, patch, discovery = discover_history(record, clone)
            report.update(status="eligible_for_docker", synthesis_feasible=True,
                          candidate_id=task.candidate.candidate_id,
                          acceptance_strategy=strategy["id"],
                          acceptance_coverage=strategy["coverage"],
                          difficulty=difficulty_metadata((task,), patch, changed))
            candidates.append({"pr_url": url, "candidate": asdict(task.candidate),
                               "task_spec": asdict(task), "preflight": report})
        except (FactoryError, OSError, ValueError, SyntaxError, KeyError) as exc:
            report["rejection_reason"] = str(exc)
            rejections.append({"pr_url": url, "reason": str(exc),
                               "stage": "history_preflight"})
        reports.append(report)
    write_json(output / "candidates.json", candidates)
    write_json(output / "rejections.json", rejections)
    write_json(output / "preflight_reports.json", reports)
    summary = {"input_count": len(urls), "eligible_count": len(candidates),
               "rejected_count": len(rejections), "docker_verified_count": 0,
               "cache": str(cache), "reports": "preflight_reports.json",
               "candidates": "candidates.json", "rejections": "rejections.json"}
    write_json(output / "preflight_summary.json", summary)
    return summary
