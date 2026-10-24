import json
import shutil
from pathlib import Path
from uuid import uuid4

from rf_factory.config import Config
from rf_factory.pipeline import Pipeline
from rf_factory.verifier import verify


ROOT = Path(__file__).resolve().parents[1]


def test_responsibility_pipeline():
    config = Config.load(ROOT / "examples/module-responsibility.toml")
    scratch = ROOT / ".rf-factory-work" / ("responsibility-test-" + uuid4().hex)
    try:
        output = scratch / "run"
        result = Pipeline().run(config, output)
        assert result["exported"] == result["gold_pass"] == result["noop_fail"] == 1
        row = json.loads((output / "instances.jsonl").read_text(encoding="utf-8"))
        assert row["task_spec"]["refactoring_type"] == "module_responsibility_migration"
        cid = row["task_spec"]["candidate"]["candidate_id"]
        spec = json.loads((output / "verifier" / cid / "spec.json").read_text(encoding="utf-8"))
        gold = output / "gold" / cid / "workspace"
        assert all(verify(gold, spec)["checks"].values())
        service = gold / "app/services/pricing.py"
        service.write_text(service.read_text(encoding="utf-8") + "\n# implementation note\n", encoding="utf-8")
        assert all(verify(gold, spec)["checks"].values())
        assert not all(verify(output / "source", spec)["checks"].values())
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
