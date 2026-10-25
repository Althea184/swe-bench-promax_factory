"""N-task contracts and local calls needed by historical-plus-synthetic composition."""
import pytest

from rf_factory.discovery.synthetic.api_migration import ApiMigrationDiscovery
from rf_factory.synthesis.generators.api_migration import (ApiMigrationGoldGenerator,
                                                           ApiMigrationSpecBuilder)
from rf_factory.core.common import digest
from rf_factory.core.config import Config, MigrationRecipe
from rf_factory.core.models import Candidate, CompoundTaskSpec, Repository, TaskSpec


def test_four_subtask_dag_rejects_cycle():
    tasks = tuple(TaskSpec(Candidate(str(index), f"module_{index}.py", "helper", 1, 2,
                                     "a" * 64, "real call site", "api-migration-v2"),
                           "api_migration", "Update helper calls.",
                           (f"module_{index}.py",), {}, ("Require named argument.",),
                           ("Preserve behavior.",), (), {}) for index in range(4))
    edges = (("0", "1"), ("0", "2"), ("1", "3"))
    scope = tuple(path for task in tasks for path in task.scope)
    assert len(CompoundTaskSpec(tasks, edges, "Unify related calls.", scope).subtasks) == 4
    with pytest.raises(ValueError, match="Cyclic"):
        CompoundTaskSpec(tasks, (*edges, ("3", "0")), "Unify related calls.", scope)


def test_private_relocated_helper_can_migrate_local_calls(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("from .logic import *\n", encoding="utf-8")
    source = ('__all__ = ["run"]\n\n'
              'def _helper(value, mode="plain"):\n    return value + mode\n\n'
              'def run(value):\n    return _helper(value, "suffix")\n')
    (package / "logic.py").write_text(source, encoding="utf-8")
    files = {"pkg/__init__.py": digest((package / "__init__.py").read_bytes()),
             "pkg/logic.py": digest(source.encode())}
    repo = Repository("local", tmp_path, digest(source.encode()), files)
    recipe = MigrationRecipe("pkg.logic", "_helper", "_helper", "mode",
        "Require the mode by name.", ("Preserve output.",), (), (), auto_discovered=True)
    config = Config(tmp_path, api_migrations=(recipe,), min_caller_modules=1,
                    min_changed_files=1, include_local_calls=True)
    candidates, diagnostics = ApiMigrationDiscovery().discover(repo, config)
    assert len(candidates) == 1 and diagnostics == []
    spec = ApiMigrationSpecBuilder().build(repo, candidates[0], config)
    assert spec.metadata["call_counts"] == {"pkg/logic.py": 1}
    gold = ApiMigrationGoldGenerator().generate(repo, spec)
    assert gold.changes[0].path == "pkg/logic.py"
    assert 'def _helper(value, *, mode="plain")' in gold.changes[0].after
    assert '_helper(value, mode="suffix")' in gold.changes[0].after
