"""Focused contracts for automatic discovery and portable ProMax artifacts."""
import json
import json
import subprocess
from pathlib import Path

from rf_factory.api_migration import ApiMigrationDiscovery, ApiMigrationGoldGenerator, ApiMigrationSpecBuilder, render_patch
from rf_factory.config import Config
from rf_factory.real_pipeline import discover_recipes, eval_script, test_patch_for as make_test_patch
from rf_factory.repository import RepositoryPreparer
from rf_factory.publish import publish_run


FIXTURE = Path(__file__).resolve().parents[1] / "rf_factory/fixtures/api_migration_repo"


def test_discovery_derives_recipe_without_configured_api(tmp_path):
    repo = RepositoryPreparer().prepare(FIXTURE, tmp_path / "source")
    recipes, diagnostics = discover_recipes(repo)
    match = [recipe for recipe in recipes if recipe.module == "money" and recipe.symbol == "render_amount"]
    assert match and match[0].parameter == "currency"
    assert not diagnostics


def test_generated_test_patch_rejects_noop_and_accepts_gold(tmp_path):
    repo = RepositoryPreparer().prepare(FIXTURE, tmp_path / "source")
    recipe = next(recipe for recipe in discover_recipes(repo)[0] if recipe.module == "money")
    config = Config(FIXTURE, api_migrations=(recipe,), min_caller_modules=1, min_changed_files=2)
    candidate = ApiMigrationDiscovery().discover(repo, config)[0][0]
    spec = ApiMigrationSpecBuilder().build(repo, candidate, config)
    gold = render_patch(ApiMigrationGoldGenerator().generate(repo, spec).changes)
    tests = make_test_patch(spec)
    for label, apply_gold, expected in (("noop", False, 1), ("gold", True, 0)):
        work = tmp_path / label
        RepositoryPreparer().prepare(FIXTURE, work)
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        if apply_gold:
            result = subprocess.run(["git", "apply", "-"], input=gold.encode(), cwd=work, capture_output=True)
            assert result.returncode == 0, result.stderr.decode(errors="replace")
        result = subprocess.run(["git", "apply", "-"], input=tests.encode(), cwd=work, capture_output=True)
        assert result.returncode == 0, result.stderr
        result = subprocess.run(["python", "-m", "unittest", "discover", "-s", "tests"],
                                cwd=work, capture_output=True, text=True)
        assert result.returncode == expected, result.stderr
    script = eval_script(tests, ["python", "-m", "unittest", "discover", "-s", "tests"])
    assert "OMNIGRIL_EXIT_CODE=$rc" in script
    assert "git apply /tmp/rf-test.patch" in script


def test_real_instance_published_by_id(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "source").mkdir()
    (staging / "source" / "example.py").write_text("pass\n", encoding="utf-8")
    row = {"instance_id": "owner__repo-c_1234567-abcdef01", "repository": {"snapshot": "source"},
           "problem_statement": {"text": "Change the API."}, "environment": {"python": ">=3.11", "dependencies": []}}
    (staging / "instances.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (staging / "swe-bench-promax.json").write_text("[]\n", encoding="utf-8")
    published = publish_run(staging, tmp_path / "runs")
    assert published == [tmp_path / "runs" / row["instance_id"]]
    assert (published[0] / "source" / "example.py").is_file()
    assert (published[0] / "instances.jsonl").read_text(encoding="utf-8").strip() == json.dumps(row)
