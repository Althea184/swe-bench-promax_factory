"""Historical provenance, lineage, classification and shared candidate contracts."""
import subprocess

import pytest

from rf_factory.candidate_pool import relationship, synthetic_candidate
from rf_factory.history import HistoryPipeline, classify, resolve_base, resolve_pr, discover_history
from rf_factory.history_analysis import structural_evidence
from rf_factory.history_acceptance import choose_strategy
from rf_factory.history_scan import CachedGitHub, read_pr_urls, scan_history
from rf_factory.history_composition import compose_consecutive
from rf_factory.models import Candidate, RefactoringCandidate, RefactoringTaskSpec, TaskSpec


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Factory Test")
    git(root, "config", "user.email", "factory@example.test")
    (root / "code.py").write_text("value = 0\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root, git(root, "rev-parse", "HEAD")


def commit(root, value):
    (root / "code.py").write_text(f"value = {value}\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", f"change {value}")
    return git(root, "rev-parse", "HEAD")


def test_squash_and_rebase_lineage(tmp_path):
    root, base = repo(tmp_path)
    first = commit(root, 1)
    second = commit(root, 2)
    assert resolve_base(root, {"merge_commit": first, "head_sha": "f" * 40,
                               "pr_commits": ["e" * 40, "f" * 40]}) == (base, "squash_merge")
    assert resolve_base(root, {"merge_commit": second, "head_sha": second,
                               "pr_commits": [first, second]}) == (base, "rebase_fast_forward")


def test_merge_commit_lineage(tmp_path):
    root, base = repo(tmp_path)
    original = git(root, "branch", "--show-current")
    git(root, "checkout", "-qb", "refactor")
    head = commit(root, 1)
    git(root, "checkout", "-q", original)
    git(root, "merge", "--no-ff", "-qm", "Merge refactor", "refactor")
    merge = git(root, "rev-parse", "HEAD")
    assert resolve_base(root, {"merge_commit": merge, "head_sha": head,
                               "pr_commits": [head]}) == (base, "merge_commit")


def test_pr_resolution_and_issue_without_unique_pr():
    def getter(path):
        if path.endswith("/pulls/7"):
            return {"merged": True, "merge_commit_sha": "a" * 40, "title": "Refactor parser",
                    "body": "Share parsing logic", "merged_at": "2024-01-01T00:00:00Z",
                    "head": {"sha": "b" * 40}}
        if "/pulls/7/commits" in path:
            return [{"sha": "b" * 40}]
        if "/pulls/7/files" in path:
            return [{"filename": "src/pkg/parser.py"}]
        if "/issues/8/timeline" in path:
            return []
        raise AssertionError(path)
    record = resolve_pr(pr_url="https://github.com/owner/repo/pull/7", getter=getter)
    assert record["merge_commit"] == "a" * 40
    assert record["pr_files"] == ["src/pkg/parser.py"]
    with pytest.raises(Exception, match="no unique cross-referenced PR"):
        resolve_pr(issue_url="https://github.com/owner/repo/issues/8", getter=getter)


def test_issue_link_must_refer_to_the_same_repository():
    def getter(path):
        if "/issues/8/timeline" in path:
            return [{"source": {"issue": {"number": 7, "pull_request": {"url": "pr"},
                    "url": "https://api.github.com/repos/other/repo/issues/7"}}}]
        raise AssertionError(path)
    with pytest.raises(Exception, match="no unique cross-referenced PR"):
        resolve_pr(issue_url="https://github.com/owner/repo/issues/8", getter=getter)


def test_issue_without_linked_pr_is_reported_without_gold(tmp_path):
    def getter(path):
        assert "/issues/8/timeline" in path
        return []
    output = tmp_path / "preflight"
    result = HistoryPipeline().run(output=output,
        issue_url="https://github.com/owner/repo/issues/8", preflight=True, getter=getter)
    assert result["status"] == "no_eligible_pr" and result["candidate_count"] == 0
    assert not (output / "gold.patch").exists()
    assert (output / "rejections.json").exists()


def test_behavior_change_and_unsupported_refactor_are_rejected():
    evidence = {"changed_paths": ["src/pkg/api.py"], "functions_changed": [],
                "classes_added": [], "public_signatures_changed": []}
    assert "behavior change" in classify({"title": "Fix parser", "description": ""}, evidence)[1]
    assert "structural extraction" in classify({"title": "Refactor parser", "description": ""}, evidence)[1]


def test_source_neutral_relation_requires_real_evidence():
    def candidate(identity, source, base, target, symbol, workflow):
        return RefactoringCandidate(identity, "https://github.com/owner/repo", base, source,
            ({"kind": "pull_request", "number": 1},) if source == "historical" else (),
            ("extraction",), (symbol,), ("pkg.api",), "Share parser logic", ("Preserve behavior",),
            (), "developer_patch" if source == "historical" else "generated_patch",
            evidence={"workflow": workflow}, metadata={"target_commit": target})
    base, middle, end = "a" * 40, "b" * 40, "c" * 40
    first = candidate("first", "historical", base, middle, "parse", "parser")
    second = candidate("second", "historical", middle, end, "render", "parser")
    relation, reason = relationship(first, second)
    assert reason is None and relation["kind"] == "consecutive_history"
    unrelated = candidate("third", "historical", middle, end, "render", "other")
    assert relationship(first, unrelated)[0] is None
    assert "no shared reproducible base" in relationship(first,
        candidate("fourth", "historical", "d" * 40, end, "parse", "parser"))[1]


def test_legacy_synthetic_task_projects_to_shared_schema():
    legacy = Candidate("legacy", "src/pkg/api.py", "render", 2, 4, "a" * 64,
                       "caller", "api-migration-v1")
    spec = TaskSpec(legacy, "api_migration", "Require a named option", ("src/pkg/api.py",),
                    {}, ("Use the named argument",), ("Preserve output",), (),
                    {"strategy": "api-migration-v1"})
    projected = synthetic_candidate(spec, repository="https://github.com/owner/repo",
                                    base_commit="b" * 40,
                                    files={"src/pkg/__init__.py": "", "src/pkg/api.py": ""})
    assert projected.candidate.source_type == "synthetic"
    assert projected.candidate.gold_source == "generated_patch"
    assert projected.candidate.affected_modules == ("pkg.api",)
    assert projected.candidate.candidate_id == legacy.candidate_id


def test_consecutive_history_generates_one_final_diff(tmp_path):
    root, base = repo(tmp_path)
    first_sha = commit(root, 1)
    second_sha = commit(root, 2)

    def task(identity, from_sha, to_sha):
        candidate = RefactoringCandidate(identity, "https://github.com/owner/repo", from_sha,
            "historical", ({"kind": "commit", "sha": to_sha},), ("extraction",),
            ("render",), ("code",), "Reuse rendering", ("Preserve output",), (),
            "developer_patch", evidence={"workflow": "rendering"},
            metadata={"target_commit": to_sha})
        return RefactoringTaskSpec(candidate, candidate.goal, ("code.py",), ("Share code",),
                                   candidate.behavioral_invariants, (), "test-strategy")

    plan = compose_consecutive(task("one", base, first_sha),
                               task("two", first_sha, second_sha), root,
                               tmp_path / "composition")
    assert plan["relation"]["kind"] == "consecutive_history"
    assert len(plan["steps"]) == 2
    assert "-value = 0" in plan["gold_patch"] and "+value = 2" in plan["gold_patch"]
    assert "+value = 1" not in plan["gold_patch"]
    assert plan["exportable"] is False


def test_generic_extraction_decomposition_interface_and_clean_patch(tmp_path):
    root, base = repo(tmp_path)
    (root / "code.py").write_text("def convert(value):\n    return value + 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "original API")
    base = git(root, "rev-parse", "HEAD")
    (root / "code.py").write_text(
        "def _adjust(value):\n    return value + 1\n\n"
        "def convert(value=None):\n    return _adjust(value)\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "extract helper")
    target = git(root, "rev-parse", "HEAD")
    record = {"repo_url": "https://github.com/owner/repo", "owner": "owner", "name": "repo",
              "number": 7, "url": "https://github.com/owner/repo/pull/7",
              "title": "Extract adjust helper", "description": "Extract shared adjustment logic.",
              "merge_commit": target, "head_sha": "f" * 40,
              "pr_commits": ["f" * 40], "pr_files": ["code.py"]}
    task, patch, report = discover_history(record, root)
    assert set(task.candidate.refactoring_types) == {
        "extraction", "decomposition", "interface_evolution"}
    assert {item["kind"] for item in report["classification"]["operations"]} == {
        "extraction", "interface_evolution"}
    strategy = choose_strategy(record, report["classification"], report["evidence"])
    assert strategy["id"] == "static-structural-operations-v2"
    assert len(strategy["coverage"]) == 2
    clean = tmp_path / "clean"
    git(tmp_path, "clone", "-q", str(root), str(clean))
    git(clean, "checkout", "-q", base)
    applied = subprocess.run(["git", "-C", str(clean), "apply", "--check", "-"],
                             input=patch, text=True, capture_output=True)
    assert applied.returncode == 0, applied.stderr


def test_generic_relocation_evidence_and_mixed_rejection(tmp_path):
    root, _ = repo(tmp_path)
    (root / "old.py").write_text("def normalize(value):\n    return value.strip()\n", encoding="utf-8")
    (root / "new.py").write_text("def caller(value):\n    return value\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    base = git(root, "rev-parse", "HEAD")
    (root / "old.py").write_text("", encoding="utf-8")
    (root / "new.py").write_text("def normalize(value):\n    return value.strip()\n\n"
                                 "def caller(value):\n    return normalize(value)\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "move function")
    target = git(root, "rev-parse", "HEAD")
    evidence = structural_evidence(root, base, target, ["old.py", "new.py"])
    classification, reason = classify({"title": "Move normalize", "description": ""}, evidence)
    assert reason is None and classification["types"] == ("relocation",)
    assert classification["operations"][0]["from_module"] == "old.py"
    assert choose_strategy({"title": "Move normalize"}, classification, evidence)["coverage"] == ["relocation"]
    evidence["ancillary_paths"] = ["tests/test_new.py"]
    classification, reason = classify({"title": "Move normalize", "description": ""}, evidence)
    assert classification["review_status"] == "mixed_change"
    assert "mixed non-production" in reason


def test_batch_scan_reports_eligible_and_rejected_without_docker(tmp_path):
    root, _ = repo(tmp_path)
    (root / "code.py").write_text("def convert(value):\n    return value + 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "original")
    (root / "code.py").write_text("def helper(value):\n    return value + 1\n\n"
                                   "def convert(value):\n    return helper(value)\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "extract")
    target = git(root, "rev-parse", "HEAD")
    urls = tmp_path / "prs.json"
    urls.write_text('["https://github.com/owner/repo/pull/7", '
                    '"https://github.com/owner/repo/pull/8"]', encoding="utf-8")
    assert len(read_pr_urls(urls)) == 2

    def getter(path):
        if "/pulls/8" in path and not "/commits" in path and not "/files" in path:
            return {"merged": False}
        if path.endswith("/pulls/7"):
            return {"merged": True, "merge_commit_sha": target, "title": "Extract helper",
                    "body": "Extract helper", "merged_at": "2024-01-01T00:00:00Z",
                    "head": {"sha": "f" * 40}}
        if "/pulls/7/commits" in path:
            return [{"sha": "f" * 40}]
        if "/pulls/7/files" in path:
            return [{"filename": "code.py"}]
        raise AssertionError(path)

    output = tmp_path / "scan"
    summary = scan_history(input_path=urls, output=output, cache=tmp_path / "cache",
                           local_paths={"https://github.com/owner/repo": str(root)}, getter=getter)
    assert summary["eligible_count"] == 1 and summary["rejected_count"] == 1
    assert not (output / "Dockerfile").exists()
    assert "not merged" in (output / "rejections.json").read_text(encoding="utf-8")


def test_github_metadata_cache_reuses_a_response(tmp_path, monkeypatch):
    calls = []
    def fake_github(path):
        calls.append(path)
        return {"path": path}
    monkeypatch.setattr("rf_factory.history_scan.github_json", fake_github)
    cache = CachedGitHub(tmp_path)
    assert cache("/repos/owner/repo/pulls/7") == {"path": "/repos/owner/repo/pulls/7"}
    assert cache("/repos/owner/repo/pulls/7") == {"path": "/repos/owner/repo/pulls/7"}
    assert calls == ["/repos/owner/repo/pulls/7"]
