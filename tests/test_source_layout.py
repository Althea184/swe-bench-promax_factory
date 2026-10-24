"""Real-pipeline discovery contracts for src layouts and relative imports."""
import pytest

from rf_factory.api_migration import ApiMigrationGoldGenerator
from rf_factory.compound import candidates_for
from rf_factory.compound import discovery_preflight
from rf_factory.real_pipeline import discover_recipes, eval_script
from rf_factory.repository import RepositoryPreparer
from rf_factory.usage_analysis import UsageIndex, module_name, module_path


def source_repo(tmp_path, caller=None):
    root = tmp_path / "project"
    package = root / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        "def normalize(value, strict):\n    return value.lower() if strict else value\n\n"
        "def render(value, strict):\n    return normalize(value, strict)\n", encoding="utf-8")
    (package / "caller.py").write_text(caller or
        "from .api import normalize, render\nfrom . import api\n\n"
        "def use():\n    return normalize('A', True) + render('B', False) + api.normalize('C', True)\n",
        encoding="utf-8")
    return RepositoryPreparer().prepare(root, tmp_path / "snapshot")


def test_src_layout_relative_imports_and_preflight(tmp_path):
    repo = source_repo(tmp_path)
    assert module_name("src/pkg/api.py", repo.files) == "pkg.api"
    assert module_path("pkg.api", repo.files) == "src/pkg/api.py"
    recipes, diagnostics, rejected = discover_recipes(repo, include_rejections=True)
    assert not diagnostics
    assert {(item.module, item.symbol) for item in recipes} == {
        ("pkg.api", "normalize"), ("pkg.api", "render")}
    index = UsageIndex(repo.snapshot, repo.files, "pkg.api", "normalize")
    assert len(index.call_sites()["src/pkg/caller.py"]) == 2
    report = discovery_preflight(repo, recipes, ["python", "-m", "pytest"], diagnostics, rejected)
    assert report["candidate_count"] == 2
    assert report["pair_count"] == 1
    assert report["pairs"][0]["relation"]["direct_edges"] == [("normalize", "render")]
    assert report["candidates"][0]["callers"][0]["module"] == "pkg.caller"
    assert report["candidates"][0]["callers"][0]["calls"][0]["line"] > 0
    _, _, accepted = candidates_for(repo, recipes, include_candidates=True)
    normalize = next(spec for recipe, spec in accepted if recipe.symbol == "normalize")
    changes = ApiMigrationGoldGenerator().generate(repo, normalize).changes
    assert {change.path for change in changes} == {"src/pkg/api.py", "src/pkg/caller.py"}
    assert "normalize(value, strict=strict)" in next(change.after for change in changes
                                                        if change.path == "src/pkg/api.py")


def test_untracked_relative_import_is_rejected(tmp_path):
    repo = source_repo(tmp_path, "from .missing import normalize\n\ndef use():\n    return normalize('A', True)\n")
    with pytest.raises(ValueError, match="Untracked relative import"):
        UsageIndex(repo.snapshot, repo.files, "pkg.api", "normalize").call_sites()
    recipes, diagnostics, rejected = discover_recipes(repo, include_rejections=True)
    assert not recipes
    assert any("Untracked relative import" in item["error"] for item in diagnostics)
    assert any("call graph analysis failed" in item["reason"] for item in rejected)


def test_compound_acceptance_can_run_separately_from_pytest_regression():
    script = eval_script("patch", ["python", "-m", "pytest", "-q", "tests"],
                         "test_rf_migration_0.py", "rf_acceptance")
    assert "python -m pytest -q tests && python -m unittest discover -s rf_acceptance " in script
    assert "-p test_rf_migration_0.py" in script
