"""Historical pair lineage, relation, acceptance, and preflight contracts."""
import json
import subprocess
import unittest

import pytest

from rf_factory.history_compound import HistoryCompoundPipeline, historical_relation
from rf_factory.history_acceptance import StaticStructuralOperations
from rf_factory.models import RefactoringCandidate, RefactoringTaskSpec
from rf_factory.common import FactoryError


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root, message):
    git(root, "add", ".")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def sample_history(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Factory Test")
    git(root, "config", "user.email", "factory@example.test")
    (root / "utils.py").write_text(
        "def normalize(value):\n    return value.strip()\n\n"
        "def encode(value):\n    return value.upper()\n", encoding="utf-8")
    (root / "header.py").write_text(
        "def render(value):\n    return value\n", encoding="utf-8")
    (root / "body.py").write_text(
        "def package(value):\n    return value\n", encoding="utf-8")
    base = commit(root, "base")
    (root / "utils.py").write_text(
        "def encode(value):\n    return value.upper()\n", encoding="utf-8")
    (root / "header.py").write_text(
        "def normalize(value):\n    return value.strip()\n\n"
        "def render(value):\n    return normalize(value)\n", encoding="utf-8")
    first = commit(root, "Move normalize to header (#1)")
    (root / "utils.py").write_text("", encoding="utf-8")
    (root / "body.py").write_text(
        "def encode(value):\n    return value.upper()\n\n"
        "def package(value):\n    return encode(value)\n", encoding="utf-8")
    second = commit(root, "Move encode to body (#2)")
    return root, base, first, second


def test_two_real_git_revisions_preflight_without_docker(tmp_path):
    root, base, first, second = sample_history(tmp_path)
    output = tmp_path / "preflight"
    result = HistoryCompoundPipeline().run(output=output, commits=[first, second],
        repo_url="https://github.com/example/project", local_path=root, preflight=True)
    assert result["pair_count"] == 1 and result["base_commit"] == base
    assert result["relation"]["shared_source_modules"] == ["utils.py"]
    specs = json.loads((output / "subtasks.json").read_text(encoding="utf-8"))
    assert len(specs) == 2
    assert all(spec["candidate"]["source_refs"][0]["kind"] == "commit"
               for spec in specs)
    assert all(spec["acceptance_strategy"] == "static-structural-operations-v2"
               for spec in specs)
    assert not (output / "swe-bench-promax.json").exists()


def test_relocation_accepts_preserved_function_name(tmp_path):
    root, _, _, _ = sample_history(tmp_path)
    operation = {"kind": "relocation", "from_module": "utils.py",
                 "to_module": "header.py", "from_symbol": "normalize",
                 "to_symbol": "_normalize", "callers": ["render"]}
    patch = StaticStructuralOperations._test_patch([operation])
    content = "\n".join(line[1:] for line in patch.splitlines()
                        if line.startswith("+") and not line.startswith("+++"))
    namespace = {}
    exec(content, namespace)
    namespace["ROOT"] = root
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        namespace["TestHistoricalStructure"])
    result = unittest.TestResult()
    suite.run(result)
    assert result.wasSuccessful(), result.failures


def test_nonconsecutive_or_unrelated_pair_is_rejected(tmp_path):
    root, _, first, second = sample_history(tmp_path)
    (root / "other.py").write_text("x = 1\n", encoding="utf-8")
    third = commit(root, "unrelated")
    output = tmp_path / "gap"
    result = HistoryCompoundPipeline().run(output=output, commits=[first, third],
        repo_url="https://github.com/example/project", local_path=root, preflight=True)
    assert result["pair_count"] == 0
    assert "not consecutive" in result["error"]
    assert not (output / "gold.patch").exists()


def test_relation_rejects_same_repository_without_structural_link():
    def task(identity, base, target, symbol):
        candidate = RefactoringCandidate(identity, "https://github.com/example/project",
            base, "historical", ({"kind": "commit", "sha": target},),
            ("interface_evolution",), (symbol,), ("code",), "Improve interface",
            ("Preserve behavior",), (), "developer_patch",
            metadata={"target_commit": target})
        return RefactoringTaskSpec(candidate, candidate.goal, ("code.py",),
            ("Update interface",), candidate.behavioral_invariants, (), "static")
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    report = {"classification": {"operations": [{"kind": "interface_evolution",
        "symbol": "left"}]}}
    with pytest.raises(FactoryError, match="No evidenced"):
        historical_relation(task("one", a, b, "left"),
                            task("two", b, c, "right"), report, report)
