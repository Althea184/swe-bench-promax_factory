"""Composition contracts, including two independently failing partial solutions."""
import subprocess
from pathlib import Path

from rf_factory.synthesis.generators.api_migration import ApiMigrationGoldGenerator, render_patch
from rf_factory.synthesis.composition.synthetic import apply_steps, candidates_for
from rf_factory.discovery.synthetic.recipes import discover_recipes
from rf_factory.validation.synthetic_acceptance import test_patch_for as make_test_patch
from rf_factory.repository import RepositoryPreparer


def git(root, *args, data=None):
    result = subprocess.run(["git", *args], cwd=root, input=data, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode()


def sample_repo(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "svc.py").write_text(
        "def encode(item, mode):\n    return item + mode\n\n"
        "def store(item, mode):\n    return encode(item, mode)\n", encoding="utf-8")
    (source / "caller.py").write_text(
        "from svc import encode, store\n\n"
        "def run():\n    return store('a', 'b') + encode('c', 'd')\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_original.py").write_text(
        "import unittest\nfrom caller import run\n\n"
        "class Original(unittest.TestCase):\n"
        "    def test_behavior(self):\n        self.assertEqual(run(), 'abcd')\n", encoding="utf-8")
    git(source, "init", "-q")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "add", "-A")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    return source


def test_related_pair_is_ordered_and_gold_comes_from_final_tree(tmp_path):
    source = sample_repo(tmp_path)
    repo = RepositoryPreparer().prepare(source, tmp_path / "snapshot")
    pairs, rejected = candidates_for(repo, discover_recipes(repo)[0],
                                     ["python", "-m", "unittest", "discover", "-s", "tests"])
    assert len(pairs) == 1 and not rejected
    assert pairs[0][4]["direct_edges"] == [("encode", "store")]
    gold, steps, ordered = apply_steps(repo, source, pairs[0], tmp_path / "attempt")
    assert [recipe.symbol for recipe, _ in ordered] == ["encode", "store"]
    assert gold.count("diff --git a/svc.py") == 1
    assert gold.count("diff --git a/caller.py") == 1
    assert all(record["callers_before"] == record["callers_after"] for record in steps)

    tests = "".join(make_test_patch(spec, f"tests/test_rf_migration_{i}.py")
                    for i, (_, spec) in enumerate(ordered))
    variants = {"noop": None, "gold": gold}
    for i, (_, spec) in enumerate(ordered):
        variants[f"partial_{i}"] = render_patch(ApiMigrationGoldGenerator().generate(repo, spec).changes)
    for label, patch in variants.items():
        checkout = tmp_path / label
        subprocess.run(["git", "clone", "-q", str(source), str(checkout)], check=True)
        if patch:
            git(checkout, "apply", "-", data=patch.encode())
        git(checkout, "apply", "-", data=tests.encode())
        result = subprocess.run(["python", "-m", "unittest", "discover", "-s", "tests"],
                                cwd=checkout, capture_output=True, text=True)
        assert (result.returncode == 0) == (label == "gold"), (label, result.stderr)
        assert "test_behavior" not in result.stderr or "FAIL: test_behavior" not in result.stderr
