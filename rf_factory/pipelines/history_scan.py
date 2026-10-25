"""Orchestrate historical batch discovery and report writing."""
from dataclasses import asdict
from pathlib import Path
from ..core.common import FactoryError, write_json
from ..core.process import command, require
from ..discovery.historical.analysis import classify, structural_evidence
from ..discovery.historical.inputs import read_pr_urls
from ..repository.cache import CachedGitHub as _CachedGitHub, cached_clone
from ..repository.github import github_json, resolve_pr
from ..repository.git_history import resolve_base
from ..synthesis.candidate_pool import difficulty_metadata
from ..synthesis.generators.historical import discover_history
from ..validation.historical_acceptance import choose_strategy


class CachedGitHub(_CachedGitHub):
    def __init__(self, cache: Path):
        super().__init__(cache, fetcher=lambda path: github_json(path))


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
